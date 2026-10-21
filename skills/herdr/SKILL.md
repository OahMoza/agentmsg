---
name: herdr
description: "在 Herdr 中使用 agentmsg 项目信箱进行跨 Pane/Agent 的安全消息收发、等待与回执。仅在用户明确要求 Herdr 编排或 agentmsg 通信时使用，且需要 HERDR_ENV=1。"
---

# Herdr + agentmsg 项目信箱（Windows / Herdr 0.9.4）

该 Skill 补充 [Herdr 官方 Skill](https://github.com/herdrdev/herdr/blob/v0.9.4/skills/herdr/SKILL.md) 的使用方法，将 `agentmsg.py` 的项目级可靠信箱集成到 Herdr。**官方 Herdr CLI 仍是权威；本 Skill 不取代 `herdr --skill` 的实时说明。**

## 0. 安全与范围

1. 在 Herdr Agent Pane 内才允许执行控制指令。检查 `HERDR_ENV=1`；不满足则停止，不试图操作其他窗口的焦点。
2. Herdr CLI 命令以本机 `herdr --help` / `herdr agent`、`herdr pane` 为准；服务端与客户端版本可能不同，必要时先 `herdr status`。
3. 不自动关闭用户 Pane，不发送任意审批按键，也不为测试使用 `--auto-approve` 或绕过权限的参数。
4. **消息不是任务授权**：agentmsg 的消息、task 状态与 ANWG 正式 WorkUnit/Execution 的治理状态不同。
5. 工作区、Session ID、Agent 名称及注册关系只来自实时查询；不要猜测旧 Pane ID 仍有效。

## 1. 拓扑与 Agent 状态

Herdr 层级为 Workspace → Tab → Pane；可识别 Agent。CLI 支持：

```bash
herdr workspace list
herdr pane list
herdr agent list
herdr agent get <agent-name-or-pane-id>
herdr agent read <agent-name-or-pane-id> --source recent-unwrapped --lines 120
```

Agent 状态：`idle`、`done`、`working`、`blocked`、`unknown`。
- 投递目标**只有 idle/done** 才可通知；其他状态等待，不往审批/提问界面注入内容。
- `herdr agent read`（读 pane 输出）= 排障工具不是监控：内容验证走信箱（`agentmsg read/inbox`），完成判定走 `agent get` 生命周期状态（结构化、零歧义）；只在超时/疑卡时读一眼屏，且投递安检靠 `agent get` 状态门、从不依赖读屏。机制与制度（tern/herdr 通用）见 `docs/observation-discipline.md`。
- `herdr agent prompt` 已提供文本+Enter 的原子提交，不要重复手动发送 Enter。
- `agent prompt` 成功只代表输入被提交，不代表 Agent 已处理。Agent 真正回执以 `agentmsg reply` 和消息 ID 为准。

## 2. 每个项目启动一次信箱

从项目根目录执行：

```bash
python agentmsg.py init
python agentmsg.py register controller --kind agent
```

确保 `.agent-mail/` 在 `.gitignore` 中。运行其他项目时切换到相应项目 cwd 或明确使用 `--db`。

## 3. 注册已有 Herdr Agent（无需重启）

若 Herdr 已有名为 `tester` 的 Agent：

```bash
herdr agent list
python agentmsg.py register tester --kind omp --herdr-target tester
python agentmsg.py agents --json
```

`--herdr-target` 可以是唯一的实时 Agent 名称或 Pane ID；更推荐通过 `herdr agent start` 取得稳定的会话内 Agent 名称，未命名的现存 Agent 可以使用实时 Pane ID。Agent 被重启/移动后应重新查询并更新绑定；**不要复用过期 Pane ID**。

旧的 `--block <整数>` 仍是 Tern 路径；重新 `register --block` 会切换回 Tern，不改变已存消息。

## 4. 新开 Herdr Pane 与 Agent

```bash
herdr pane split --current --direction right --cwd "$PWD" --no-focus
# 从 result.pane.pane_id 读取真实 ID 后：
herdr agent start tester --kind omp --pane <new-pane-id>
python agentmsg.py register tester --kind omp --herdr-target tester
```

不要自动替换用户正在工作的 Pane 或推断新 ID。Windows PowerShell 下 `$PWD` 展开需按当前 Shell 语法检查，或提供显式项目路径。

## 5. 发信、watcher、收信

```bash
python agentmsg.py send controller tester "请检查该修复并回复证据"
python agentmsg.py watch tester --once
python agentmsg.py inbox tester --all
python agentmsg.py read <message-id>
python agentmsg.py reply <message-id> "完成；测试结果见附件"
python agentmsg.py wait controller --after-id <sent-message-id> --timeout 600
```

`watch` 沿用原消息的原子认领、两分钟 Claim Lease、fencing 与失败回退。Herdr 路径通过 `agent get` 读取生命周期状态，只有 `idle/done` 时才调用一次 `agent prompt` 提交**固定通知**（仅消息编号与固定取信指引，不包含正文、主题或来源等外部输入；正文由 Agent 自己 `python agentmsg.py read` 获取）。

watcher 应在 `HERDR_ENV=1` 的受管 Pane 中启动；实际 Windows PowerShell 运行时应确认当前 Pane 继承该变量。自动 watch 可在独立受管 Pane 执行：

```bash
python agentmsg.py watch tester --interval 5
```

超时或执行失败后，若 Herdr 已提交而数据库尚未标记 delivered，可能出现重复通知。**收件人必须按消息 ID 去重**，绝不把重复通知当作执行第二个正式任务的授权。

## 6. 完整协议

与 Tern 路径完全共享：
- `send / inbox / read / reply / done / wait`；
- `thread_id` 和 `--after-id` 用于防止跨任务串信；
- **单向通知（UDP 语义）**：`send` 发完即走即协议合法——不需要反馈就别建回执层。通知类用 `--kind notify` 标记，接收方 `read` 后不 `reply`，可 `done <id>` 清 pending；发送方不 `wait`、不查回执。详见方案 §5.4；
- **派活话术与命名**：信箱正文用四要素自然话术（做什么 / 怎么交 / **谁消费后续** / 水位线），pane 通知只发 `有新消息：#<id>，请查收信箱`——脚本名与 CLI 不进话术。注册名用岗位代号（`dev-1`/`qa-1`，角色即标签），pane 位置与 harness 查 `agents` 注册表。详见方案 §5.6、§3；
- `watch` 只承担通知，不代表执行或验收；
- `agents` 中 `transport=herdr` 标识 Herdr 绑定；
- `status`、`log`、`task` 是信箱辅助功能，不代替 ANWG Core 事实。

## 7. 故障和边界

- `agent get` 得到 `working`/`blocked`/`unknown` 或无法识别状态：保留 pending，不向目标输入。
- `agent prompt` 返回错误或超时：保留 pending，查询 Herdr Agent 和收件箱情况再重试。
- 发现 `agent_blocked`：不发送审批键，由实际授权人处理。
- Agent 在单次 `agent get` 和 `agent prompt` 间可能变化；Herdr 状态检查是辅助安全网，不构成 OS 权限隔离。
- 执行环境升级后先查看 `herdr --skill` 和 `herdr api schema --json`，再核对适配器；未知能力必须安全降级。
- 测试时先用 `python -m unittest test_agentmsg -v` + `python -m unittest test_agentmsg_herdr -v`，之后在用户许可的临时 Herdr Pane 做真实回环。不要在真实项目中盲目发送破坏性测试内容。

参考：[Herdr v0.9.4 官方 Skill](https://github.com/herdrdev/herdr/blob/v0.9.4/skills/herdr/SKILL.md) · [Herdr Agent 自动化](https://herdr.dev/docs/agent-automation/)。
