# agentmsg

项目内多 agent 信箱：SQLite 单文件、零依赖（纯 Python 3 标准库），让多个 coding agent 互发消息、互相等待，**不经过主控会话转手**——日常协调 0 主控 token。

## 快速开始

```bash
# 每个项目初始化一次，建 ./.agent-mail/mail.db（记得把 .agent-mail/ 加进 .gitignore）
python agentmsg.py init

python agentmsg.py register tester --kind=agent   # 注册参与者（或 human）
python agentmsg.py send dev tester "跑一下回归" 
python agentmsg.py wait dev --timeout 600         # 阻塞等回信，不烧 token
python agentmsg.py inbox tester                   # 查收件箱
python agentmsg.py reply 42 "全绿"                # 按 id 回信
python agentmsg.py done 42
```

agent 侧常驻 watcher，把新消息投进各自终端/面板：

```bash
python agentmsg.py watch tester          # 轮询 ./.agent-mail，5s 间隔
python agentmsg.py watch tester --once   # 投完当前 pending 即退出，适合 cron
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `init` | 建库（`<cwd>/.agent-mail/mail.db`） |
| `register <name>` | 注册 agent / human |
| `send <from> <to> [body]` | 发消息（长内容 `--file` 存附件，`--thread` 关联线程） |
| `wait <me> [--thread …]` | 阻塞等回信（跨 thread 栅栏防串信，`--timeout` 兜底） |
| `inbox` / `read` / `reply` / `done` | 收件箱 / 读 / 回 / 完结 |
| `task new/list/set` | 轻量任务板 |
| `status` / `log` | 状态上报与查询（status_log 只留近 30 天） |
| `sweep` | 清理过期附件/租约 |
| `watch <me>` | 常驻投递循环（Tern pane 投递，失败自动回退 pending） |

## 可靠性设计（v2.1）

- **原子认领 + 租约**：watcher 认领消息带 2 分钟租约与 `claim_token`，多 watcher 并发不重复投递。
- **fencing 防复活**：只有租约仍归属本 token 才能落 delivered；被回收重认领后，旧持有者的 finalize 是 no-op。
- **失败回退**：投递失败（含 pane 已死）→ 带回 pending + warn；`--once` 模式停手而非无限重试。

## 测试

```bash
python -m unittest test_agentmsg -v
```

全部 mock、不碰真实 pane。覆盖并发领取、崩溃恢复、重复投递防护、旧库迁移等。

## 安装 skill（另一台电脑）

用 [skills CLI](https://github.com/vercel-labs/skills) 直接从本仓库装（需要 Node.js）：

```bash
npx skills add OahMoza/agentmsg        # 交互式选择 agent
npx skills add OahMoza/agentmsg -g -y  # 装到用户级，免确认
```

前置条件：`tern` skill 需本机装有 Tern 且窗口在运行；`agentmsg.py` 本身是零依赖单文件，直接 clone 或复制即可用。

## 设计文档

见 [agentmsg-方案.md](agentmsg-方案.md)。

## Herdr 适配（可与 Tern 共存）

`skills/herdr/SKILL.md` 是面向 **Herdr 0.9.4** 的 agentmsg 扩展 Skill；对应的 transport 在 `agentmsg_herdr.py`。消息 DB/Claim/Thread/Wait 仍由 `agentmsg.py` 统一维护，不重建队列。

在 **Herdr 托管的 Agent Pane**（`HERDR_ENV=1`）内操作：

```bash
herdr agent list
python agentmsg.py init
python agentmsg.py register controller --kind agent
python agentmsg.py register tester --kind omp --herdr-target tester
python agentmsg.py send controller tester "请检查并回复"
python agentmsg.py watch tester --once
python agentmsg.py inbox controller --all
```

Herdr 路径只向 `idle/done` Agent 投递固定消息通知（`herdr agent get` + `herdr agent prompt`）；`working/blocked/unknown` 不投递。Herdr 成功返回意味着输入已被提交，**不是 Agent 已读取/完成**。可由 Agent `agentmsg read <id>` 后 `agentmsg reply <id> ...`，用 `wait` 按消息 ID/thread 栅栏收回执。未做真实 Herdr/Windows 端到端验证前，请先按 Skill 说明运行隔离冒烟测试。

旧 `register --block <int>` 和 Tern watcher 默认行为保持兼容；用 `--herdr-target <agent-name-or-pane-id>` 给同一收件人切换至 Herdr，随时再用 `--block` 切回。

## 消息已读与通知安全（2026-10-20）

- `read` 会将 `pending` 或 `delivered` 更新为 `read`，保留首次 `read_at`；`claimed` 仍受 watcher 认领保护。
- Tern 和 Herdr 共用的终端通知现在**只含可信消息 ID 与固定取信指引**，不再插入消息正文、主题、发件人或内容预览。
- `delivered` 只代表投递动作被终端接受，不代表已读；`read` 不代表任务完成，`done` 不代表 ANWG 的 WorkUnit.ACCEPTED。
- Herdr 的 `agent get` 返回 JSON 无效或状态未知时 fail-closed。若提交超时仍可能已投递，收件方必须以消息 ID 去重，不能重复执行正式任务。
- 如 Agent 当前工作目录不是仓库根目录，须确保可以执行 `python agentmsg.py read <id>`，且各 Agent 使用相同的 `.agent-mail/mail.db`；后续可增加显式脚本路径与 DB 路径传递。
