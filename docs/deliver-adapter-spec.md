# agentmsg 投递适配器通用接口规范（deliver_one_<tool>）

> 目标：把 agentmsg 的可靠信箱接到任何"能向 agent 所在终端提交输入、且能查询其状态"的工具上。
> 参考实现：`deliver_one_tern`（agentmsg.py）、`deliver_one_herdr`（agentmsg.py + agentmsg_herdr.py）。
> 核心原则：**消息真相在 SQLite；投递通道可丢弃；通知 ≠ 执行。**

---

## 1. 适配器在架构中的位置

```
watcher 循环 (cmd_watch, 每 interval 秒)
  └─ deliver_one(conn, name)            # 分发层，transport 无关，固定不改
       ├─ 重读 agents 表绑定             # 每次调用重读（fencing C：重绑立即生效）
       ├─ transport == '<tool>'
       │    └─ deliver_one_<tool>(conn, name, target)   ← 你要实现的就这一个函数
       └─ 其他 transport → 各自适配器
```

分发层 `deliver_one` 已负责：重读绑定、调用适配器、日志。适配器不自己查 agents 表拿绑定，绑定由参数 `target` 传入。

---

## 2. 函数签名

```python
def deliver_one_<tool>(conn, name, target):
    """
    conn    : sqlite3.Connection，agentmsg 数据库（WAL 模式）
    name    : str，收件 agent 名（agents.name）
    target  : str | None，agents 表中该 agent 的绑定标识
              （如 herdr_target / tern_block_id / 其他工具的 agent 或 pane 句柄）
    返回值  : 以下状态字符串之一（见 §4）
    """
```

禁止在适配器内缓存 target —— 每次调用由分发层传入最新值。

---

## 3. 必须复用的公共设施（在 agentmsg.py 中，不许重写第二份）

| 函数 | 作用 | 适配器内的用法 |
|---|---|---|
| `claim_one(conn, name)` | 原子认领一条 pending（BEGIN IMMEDIATE + 条件 UPDATE + claim_token） | 返回 `None`→empty、`True`→busy、dict→已认领 |
| `finalize(conn, mid, token)` | fencing：token 仍持租约才落 delivered | 提交成功后必须调用 |
| `revert(conn, mid, token)` | 退回 pending（失败/不安全/跳过时） | 任何"没投出去"的路径必须调用 |
| `notify_text(row)` | 固定格式通知文本：`[agentmsg] 新消息 msg <id>。\n取信: python agentmsg.py read <id>` | **只投这个**，不拼接 sender、subject、kind 或正文等不可信字段 |

适配器的 try 块只包含"查状态 + 提交"两步，其余全交给分发层语义。

---

## 4. 返回值契约

| 返回 | 语义 | 消息状态 | 适配器侧动作 |
|---|---|---|---|
| `"unbound"` | target 为空/无效 | 不动 | 直接 return，不认领 |
| `"empty"` | 无 pending 消息 | pending（本来就没有） | 不认领（claim_one 返回 None 时） |
| `"busy"` | 认领被别的 watcher 抢走 | claimed（别人的） | return busy，绝不 revert 别人的租约 |
| `"skipped"` | 目标不安全/不 ready，暂缓 | **pending**（已 revert） | revert + stderr 说明原因 |
| `"failed"` | 提交异常或结果不明 | **pending**（已 revert） | revert + stderr 警告 |
| `"delivered"` | 工具已接受提交 | delivered（finalize 成功） | finalize 成功后 return |
| `"busy"`（finalize 失败） | 租约在提交期间被回收 | 未知（别人的事） | return busy，**不动**，等新一轮 |

关键不变量：除 `"delivered"` 和两种 `"busy"` 外，适配器退出时消息必须回到 pending。

---

## 5. 实现骨架（模板）

```python
def deliver_one_<tool>(conn, name, target):
    """<工具> 投递：认领/租约/fencing/回退全部沿用公共机制。"""
    if not target:
        return "unbound"
    got = claim_one(conn, name)
    if got is None:
        return "empty"
    if got is True:
        return "busy"
    row = got
    try:
        # ① 安全门：查询目标状态，只有 ready 态才投
        if not <tool>_ready(target):
            revert(conn, row["id"], row["claim_token"])
            print(f"skip: msg {row['id']} 暂缓——<tool> 目标非 ready，保持 pending", file=sys.stderr)
            return "skipped"
        # ② 原子提交：一条命令提交 文本+Enter（工具不支持原子时，两步之间不插逻辑）
        <tool>_submit(target, notify_text(row))
        # ③ fencing 落账
        if finalize(conn, row["id"], row["claim_token"]):
            print(f"delivered: msg {row['id']} → {name} (<tool> target={target})")
            return "delivered"
        return "busy"
    except Exception as e:
        revert(conn, row["id"], row["claim_token"])
        print(f"warn: msg {row['id']} 投递失败或结果不明，已退回队列: {e}", file=sys.stderr)
        return "failed"
```

①②③ 之外不允许有其他副作用。③ 的 finalize 失败路径 return busy 且不 revert（租约已易主）。

---

## 6. 工具侧两个原语（你在工具层要提供的）

### 6.1 `<tool>_ready(target) -> bool`

投递前安全门。语义：**目标当前处于"可以接受一段新输入"的状态**。

| 要求 | 说明 |
|---|---|
| 优先结构化状态 | 用工具的 API/JSON（如 `herdr agent get`），不要靠终端文字猜。实在没有 API 才降级到文本启发式（如 Tern 的 `pane_unsafe` 审批框文案匹配） |
| unknown 即不投 | 状态拿不到、解析不出、工具不可达 → 一律 `False`（fail-closed），宁可暂缓不误投 |
| blocked 不投 | 审批/提问界面绝不注入；保留 pending 由人处理 |
| ready 集合最小化 | 只认明确 ready 的状态（Herdr: `{idle, done}`；Tern 等价物：输出静默 ≥1.5s 且无审批特征） |

### 6.2 `<tool>_submit(target, text) -> None`

把一段文本作为 agent 的输入提交。失败必须抛异常（走 revert）。

| 要求 | 说明 |
|---|---|
| 原子提交 | 文本 + Enter 一次有序提交（Herdr `agent prompt` 即如此）。工具无原子原语时：先 paste 文本再发 Enter，两步间不得插入任何等待/查询 |
| 成功 ≠ 已读 | 返回成功仅代表输入已进入目标终端；Agent 是否处理由 `agentmsg reply` 回执证明。文档/日志措辞必须体现这一点 |
| 环境校验 fail-closed | 若工具要求在受管环境内执行（如 Herdr 的 `HERDR_ENV=1`），在 submit 入口强制校验，不满足抛 RuntimeError，不降级不绕过 |
| 不发按键绕过 | 不发送 esc/ctrl+c/y/回车确认等"替 agent 答审批"的按键；审批由实际授权人处理 |

---

## 7. 集成清单（接入新工具时的核对表）

1. [ ] `agents` 表支持：`transport='<tool>'` + 新增绑定列（参照 herdr_target 的 ALTER TABLE 迁移写法，agentmsg.py `connect()`）
2. [ ] `register --<tool>-target` 写入绑定；与 `--block`/`--herdr-target` 互斥校验
3. [ ] `deliver_one()` 分发处加一个 `elif ag["transport"] == "<tool>"` 分支
4. [ ] 实现 `deliver_one_<tool>`（§5 骨架），原语 `_<tool>_ready` / `_<tool>_submit`（§6）
5. [ ] watcher 提示语覆盖新 transport（"未绑定有效 block/target，暂不投递"）
6. [ ] 单测：状态解析（各状态→ready 判定）、fail-closed（无状态/坏 JSON→不投）、认领-回退（skipped/failed 后消息回 pending）
7. [ ] Skill 文档：绑定写法、ready 集合、去重要求（按消息 ID 去重，重复通知不是执行授权）、绑定失效后的重绑流程（不复用旧句柄）

---

## 8. 已知边界（所有 transport 共有，适配器文档必须携带）

- **超时/失败 ≠ 未送达**：提交超时后消息可能已进入目标终端。重投前先 `inbox`/`read` 核对，收件方按消息 ID 去重。
- **投递成功 ≠ 已处理**：delivered 仅表示通知进入终端；执行与回执以 `reply`/`done` 为准。
- **绑定易碎**：agent 重启/换 pane 后旧 target 失效，需重新查询并 register 重绑；适配器每次调用重读绑定就是为此兜底。
- **状态检查是辅助安全网**，不构成 OS 级权限隔离；不能替代审批机制。
