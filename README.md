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
