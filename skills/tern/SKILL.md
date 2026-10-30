---
name: tern
description: 用 tern CLI 编排终端：在 Tern 终端里开新 pane、并行跑命令、启动/驱动其他 coding agent（Claude Code、Codex 等）、等待完成、读取输出、监控事件。当用户提到 tern、开个 pane、分屏、后台终端、让另一个 agent 干活、并行 agent、盯任务输出，或要求操控 Tern 里的 pane 时使用。前提：本机装有 Tern 且至少一个窗口在运行。
---

# Tern 编排（tern CLI）

Tern 是多路复用终端：session → tab → block 三层，block 即 pane。`tern` CLI 通过 socket 与 daemon 通信，**不需要在 Tern pane 里运行**。

## 开始前

1. `tern ls --json` — 列出 sessions/tabs/blocks。失败 = 没有窗口在跑，停下告知用户。
2. **block id 只从 JSON 输出里读**（大整数，如 `25769803867`）。标题、程序名也能当 selector 但会撞名，脚本一律用 id。
3. `tern whoami` 在 pane 里返回身份链，在 pane 外返回本机身份（如 `OahMoa@OAHMOA (local)`）——它**不报错**，别拿它当"是否在 Tern 里"的判据，判据是 `tern ls --json`。在 pane 外运行时，命令作用于「第一个窗口」——控制前必须用 `tern ls --json` 确认那确实是用户想让你操作的窗口，有多个窗口时先问。

## 开一个干活 pane

```bash
tern split --json <block-id> right --keep-open -- claude   # 返回 {"session":…,"tab":…,"block":…}，取 .block
tern focus <原block-id>    # split 会把焦点给新 pane，开完立刻还给用户
```

- `--keep-open`：程序退出后 block 不自动关。agent 块必加。
- `--cwd DIR` 指定目录；默认沿用调用方的。不建新 session/tab，除非用户要求。
- 方向：宽 pane 用 `right`，窄/高 pane 用 `down`，避免连切出窄列。
- 普通命令不需要 agent 壳：`tern run <id> "just test"`（原子发送命令+回车）。

## 驱动一个 coding agent

Tern 没有 agent 生命周期状态机，**你就是状态机**，固定三步节奏：

```bash
tern send <id> paste "Review the diff, report only actionable findings."  # paste 只粘贴不回车，多行安全
tern send <id> keys enter                                                 # 第二步：回车提交
tern wait <id> --until idle=2000 --timeout 600 || true                    # 静默 2s 判稳定；退出码 124=超时
tern capture <id>                                                         # 必查！据此判定状态
```

capture 后判定：

- 出现**审批/提问框**（"Do you want"、`❯` 选项行、"allow"、"approve"、行首 `?` 等）：**不要发任何按键**，把界面内容报给用户决定。
- 出现完整回答且回到空闲提示符：完成，读结果交付。
- 仍在转圈/输出半截：回到 `wait` 继续等，直到 `--timeout`。

规矩：

- **每次 send 之前过最小安检门**：先 `tern wait --until idle=MS` 过静默门，再 `tern capture` 检查已知审批文案；命中则暂停并交由用户处理。静默门和词表都是启发式，可能漏掉静默工作或未知审批，不能证明投递安全。
- `wait --until idle=MS` 是输出静默判定，不是 agent 回合语义；MS 不低于 1500，宁可多等。
- 超时 ≠ 没送达。先 capture 再决定，别盲发第二遍。
- 需要按键（esc、ctrl+c）用 `tern send <id> keys esc`，逻辑键名。
- 给 agent 起名没有专门机制，在对话里自己记录 `名字 → block id` 的映射并沿用。

## 读输出

```bash
tern capture <id>                  # 当前视口
tern capture <id> --scrollback     # 带回滚——长回答用这个
tern capture <id> --ansi           # 颜色/样式本身是证据时
tern process <id> --json           # 查这个块里跑的进程
```

**注意**：capture 空 ≠ pane 死了。2026-10-16 的交叉实验显示 tern 0.8.3 丢失部分 omp 帧，0.8.4 修复了所测场景；旧 daemon 遗留 session 仍可能为空。排查时检查版本与 `tern process <id> --json` 的 `foreground`；静默门只反映输出静默，接入信箱的任务结果走信箱。

**omp 渲染形态也是可见性变量**（2026-10-21，tern 0.9.5 实测）：原生交互形态下帧不可见，TUI 形态可见。空屏时检查版本、渲染形态和进程，不据此判断任务完成或盲发第二遍。

**capture 使用纪律**：读屏用于超时、疑似卡住或检查审批；接入 agentmsg 的任务，内容与完成回执走信箱，判活走 `tern process`，监控使用 `tern events`。未接信箱的直接任务按需 capture 读结果。静默不证明 Agent 空闲。机制见 `docs/observation-discipline.md`。

## 监控

`tern events` 持续输出 JSON 行直到中断。后台任务多时用它订阅，别轮询 capture。

## agentmsg 接入（把新 pane 注册进信箱）

split 拿到 block id 后，**编排方顺手一行注册**（别让 agent 自探测——Windows pid 链不可靠）：

```bash
tern split --json <宿主id> right --keep-open --cwd <项目目录> -- claude --permission-mode bypassPermissions
tern run <新block-id> "python agentmsg.py register <名字> --kind claude --block <新block-id>"   # 未提交就补一次 keys enter
```

- **claude 建议 `--permission-mode bypassPermissions`**（实测全协议闭环：read→reply→done；默认 auto mode 的分类器不可用会卡权限门）。
- 安检词表已适配：`bypass permissions on` 是模式指示不是审批框，不会误跳过。
- **信箱按项目隔离**（`<cwd>/.agent-mail/mail.db`）：对别的项目里的 agent 操作 agentmsg 必须 `--cwd` 到那个项目（或 `--db` 指过去）。**期待回信前先在该项目信箱注册自己的发件人名**：`agentmsg register <名字> --kind agent`——`reply` 的收件人必须已注册，否则对方回信被拒（agent 得自己补 register 发件人才投得出去）。
- **已存在的活 pane 也能接入**，不必 split 新开：`agentmsg register <名字> --kind omp --block <现存id>`（注册仍由编排方做，别让 agent 自探测）。
- `reply` 正文是**位置参数**：`agentmsg reply <id> "正文"`，没有 `--body`；长产物用 `--file`。

任务正文里**永远不要写死 msg id**（发送时 id 还没定）；agent 按通知里的 id 自己取信（`python agentmsg.py read <通知里的id>`）。通知只包含消息 ID 和固定取信指引，不注入原消息正文。

**单向通知（UDP 语义）**：`send` 本身就是数据报——不需要反馈就别建回执层。`agentmsg send <我> <对方> "…" --kind notify` 发完即走：不 `wait`、不查回执，通知话术写明"读完即可，无需回复"。接收方 `read` 后不回信，可 `done <id>` 清 pending（唯一的可选 ACK）。广播 = 循环多次 `send`。协议语义详见方案 §5.4。

**派活话术（四要素，无脚本名；方案 §5.6）**：①做什么 ②完成后怎么交（回信到本消息 / 标记 done / 直发第三方） ③**谁消费后续回信** ④水位线与线程约定——全用自然语言写进信箱正文；`python agentmsg.py`、CLI、block id 一律不进话术，动词映射（回信→reply、查收→read、转交→tern send）由 agent 凭 skill 语境完成。pane 通知固定一行：`有新消息：#<id>，请查收信箱，按信里说的做`。
**注册名用岗位代号**（角色即标签，方案 §3）：`dev-1`、`qa-1`…；`name` 记岗位，`kind` 记程序，pane 位置与 harness 查 `agents` 注册表。

**结果回收走信箱，不走 capture**（即使 TUI 形态下 capture 可见——信箱是几行结构化记录，capture 是整帧推理文本，进的是编排方上下文）：任务里要求它 `agentmsg reply <id> --file 产物路径`，完成后 `agentmsg wait/inbox` 收取；capture 降级为按需排障工具（见「读输出」的 capture 使用纪律）。

### omp 投递限制（实测，勿踩）

- **omp 静默门的边界**：输出静默不能证明 Agent 不在工作；屏幕可能不可读，审批文案检查也可能漏报。最小门不等于安全证明。
- **watch 可能反复 skip**：静默门未通过时消息保持 pending，等待状态变化后重跑。手动投递同样先过最小门：`tern wait --until idle` + capture 查已知审批文案；这仍是启发式，有不确定的界面就上报用户，不代按。

## 收尾

- 只 `tern close <你开的id>`；**从不动用户的 block**（close 关不掉或需要强杀才 `tern kill`，且仅限自己开的）。
- agent 确认完成后关掉自己开的块；用户可能想看现场，就留着并说明。
- 不跑 `tern remote`/`tern register`/账号相关命令，除非用户明确要求。

## 已知边界

无 blocked/done 状态、无 kind 识别、无 agent 命名注册、无 `--skill` 自取入口——这些场景靠上面的「capture 判定约定」与人的判断兜底；凡涉及审批框等不确定界面，一律上报用户，不代按。

## 黄金集（实测可用序列，直接抄）

> 2026-10-16 在 tern 0.8.4 + omp/claude 上逐条跑通。信箱命令一律先 `--cwd` 到目标项目（信箱按项目隔离）。

### G1 给另一个 omp 发消息并收回执（全链路）

```bash
tern ls --json                                     # 找目标 block id（只认 JSON 里的 id）
tern process <id> --json                           # foreground=omp.exe → 活着
tern capture <id>                                  # 排障/检查已知审批文案；提示符本身不证明可投
tern wait <id> --until idle=3000 --timeout 120 || true
python agentmsg.py agents                          # 确认对方已注册；顺带看自己的发件人名在不在
python agentmsg.py register <我的名字> --kind agent  # 不在就注册，否则对方 reply 被拒
python agentmsg.py send <我的名字> <对方agent名> "任务：<做什么>。完成后回信到本消息。<四要素其余部分>"
tern send <id> paste "有新消息：#<通知里的id>，请查收信箱，按信里说的做"
tern send <id> keys enter
tern wait <id> --until idle=3000 --timeout 600 || true
python agentmsg.py read <回信id>                    # 主路径：信箱收回执（结构化、省 token）
tern capture <id> --scrollback                     # 仅怀疑卡住/要肉眼看现场时才看
```

### G2 接入一个现存的活 omp（不 split）

```bash
tern process <id> --json                             # foreground 必须是目标 agent 程序
python agentmsg.py register <名字> --kind omp --block <id>   # 注册由编排方做，别让 agent 自探测
```

### G3 split 新 claude pane + 注册

```bash
tern split --json <宿主id> right --keep-open --cwd <项目> -- claude --permission-mode bypassPermissions
tern run <新id> "python agentmsg.py register <名字> --kind claude --block <新id>"   # 未提交就补 keys enter
tern focus <宿主id>    # 焦点还给用户
```

### G4 capture 为空的排查序列

```bash
tern --version                        # 0.8.3 曾丢 omp 帧（0.8.4 修复所测场景）；旧 daemon 遗留 session 仍会空
# omp 渲染形态是可见性变量：原生交互模式可能 0 字节；先确认 omp 形态与 tern 版本
tern process <id> --json              # 1) 活没活：foreground
tern wait <id> --until idle=3000      # 2) 输出是否静默；静默不证明干完，仅作为停止等待的参考
tern capture <id> --scrollback --ansi # 3) 换变体再试
# 仍空 → 接了信箱的任务走信箱回收；未接信箱的按需读输出/人工确认，别盲发第二遍
```

完成判定以任务自身证据为准：接入信箱的看回执，直接任务按需读输出后由人判断；静默只说明没有输出。

### G5 验证信箱链路是否通（最小回环）

```bash
python agentmsg.py register <我的名字> --kind agent
python agentmsg.py send <我的名字> <对方名> "任务：回一条确认消息证明信箱链路可用。完成后回信到本消息。"
tern send <对方block> paste "有新消息：#<通知里的id>，请查收信箱，按信里说的做"
tern send <对方block> keys enter
tern wait <对方block> --until idle=3000 --timeout 120 || true
python agentmsg.py inbox <我的名字>     # 看到 reply → 链路通
```

### G6 新项目冷启动

```bash
python agentmsg.py init                          # 建 <cwd>/.agent-mail/mail.db；.agent-mail/ 记得加 .gitignore
python agentmsg.py register <我的名字> --kind agent
# 接下来：G2/G3 接入 agent，G1 派活
```

### G7 收尾清理

```bash
# 只关自己开的块（对话里记 名字→block id 映射）
tern close <id>            # 关不掉才 tern kill <id>，且仅限自己开的
tern ls --json             # 确认块消失；焦点被抢就 tern focus <原block>
```

### G8 长任务等待与防串信

```bash
tern wait <id> --until idle=3000 --timeout 600 || true   # 超时≠没干完，先 capture 再决定
python agentmsg.py wait <我的名字> --after-id <我发的msg id> --thread <thread-id> --timeout 600
# ⚠ 实测：不带 --after-id 会立刻返回线程里的旧信；--thread 只等指定线程，防跨任务串信
```

### G9 多 agent 并行 fan-out（双发双收）

```bash
# 先按 G2/G3 接入两个 agent；确认各自准备接收输入，静默和审批词表均非安全保证
python agentmsg.py send <我> <agentA> "任务A…"     # 不带 --thread → 各自新线程，天然隔离
python agentmsg.py send <我> <agentB> "任务B…"
tern send <blockA> paste "有新消息：#<idA>，请查收信箱，按信里说的做"
tern send <blockA> keys enter
tern send <blockB> paste "有新消息：#<idB>，请查收信箱，按信里说的做"
tern send <blockB> keys enter
python agentmsg.py wait <我> --expect 2 --after-id <最后发出的msg id> --timeout 300   # 双回信齐了才返回
```

### G10 task 派单三件套（task new → 通知 → done）

```bash
# ⚠ 实测：task new --assign 只插行不通知，必须配一条信箱消息把 agent 叫醒
python agentmsg.py task new "任务描述" --assign <agent>
python agentmsg.py send <我> <agent> "分派 task <id>（task list 可查）。完成后把 task 标记 done，并回信本消息一句话结果。"
tern send <block> paste "有新消息：#<msgid>，请查收信箱，按信里说的做"
tern send <block> keys enter
python agentmsg.py wait <我> --after-id <msgid> --timeout 300
python agentmsg.py task list        # state=done + 产物文件在，才算闭环
```
