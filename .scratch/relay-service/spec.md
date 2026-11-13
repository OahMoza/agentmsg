# Agent Message Relay Service 与 Runtime 投递策略（初稿）

Status: draft
Date: 2026-11-13
Scope: 架构讨论稿；尚非实现契约，不改变 agentmsg v2.1 现有行为。

## 1. 背景与目标

当前 agentmsg 以 SQLite 作为共享消息账本，通过 watcher 检测 pending 消息，并借助 Tern / Herdr 等 transport 向目标 Agent 所在终端投递固定通知。现有实现已经具备消息持久化、原子认领、租约、claim-token fencing、失败回退和消息 ID 去重纪律。

本提案不推翻现有信箱，也不引入第二套消息队列。目标是在其上增加一个本地常驻的 **Agent Message Relay Service**，统一负责：

1. 监听 SQLite 中新增的消息；
2. 根据收件人注册信息解析目标 Runtime / Session；
3. 根据消息声明的投递策略和 Runtime 能力决定何时、如何投递；
4. 统一调度、串行化、重试、恢复和记录投递结果；
5. 让发送方只需向消息表写入消息，不必直接唤醒或调用接收方。

核心原则：

> Agent 负责生产和消费消息；SQLite 保存事实；Relay Service 负责确定性调度；Runtime Adapter 负责执行对应 Harness 的会话操作。消息路由与投递策略不交给 AI 判断。

## 2. 术语边界：Session、Turn、Execution

这几个概念不能混用：

- **Session**：Agent 的持久会话上下文。通常跨多个用户回合存在。
- **Turn**：Session 中一次正在进行的模型交互/执行回合。
- **Execution / WorkUnit**：业务层的一项工作。它可能跨越多个 Turn，也可能启动后台进程或子 Agent。

本提案中的“真正中断”默认指 **中断当前 Turn**，而不是销毁整个 Session 或关闭 Agent 进程。中断后默认保留原 Session，并向同一个 Session 提交新消息。

如果未来需要“销毁整个 Session，再创建新 Session”，应作为另一项高风险 Runtime 操作单独讨论，不属于本稿的默认中断语义。

同样，Turn 完成不代表 Execution / WorkUnit 已完成；agentmsg 的消息投递不能代替 ANWG Core 的工作包验收。

## 3. 四种投递策略

消息类型（kind）与投递策略（delivery_policy）是两个独立维度。kind 描述消息语义，例如 request / reply / notify；delivery_policy 描述 Relay 应如何将消息交给目标 Runtime。

### 3.1 ABORT_AND_SUBMIT：中断当前 Turn，再提交新消息

语义：

1. 目标 Session 正在执行时，Relay 请求 Runtime 中断当前 Turn；
2. 等待 Runtime 给出明确的中断结果；
3. 确认当前 Turn 已中断后，向同一个 Session 提交新消息，开启新的 Turn；
4. 保留 Session 上下文，除非用户明确选择另一个会话生命周期操作。

约束：

- 这是破坏当前执行连续性的高影响操作，必须由发送方显式声明或由接收方明确授权的策略触发。
- 不能把发送 Ctrl+C / Esc 等按键等同于中断成功。
- 中断结果未知时不得盲目重复提交；消息保持可恢复状态并记录结果不确定。
- 不支持中断或无法确认目标 Session 时，不得静默降级为强制终止进程、销毁 Session 或向终端缓冲区粘贴新输入。
- 中断模型 Turn 不保证已经启动的 Shell、后台进程或外部副作用被撤销。

### 3.2 STEER：向当前执行追加引导指令

语义：

- 当前 Turn 保持运行；
- Relay 调用 Runtime 的原生 steering / follow-current-turn 输入能力，将新指令加入当前执行；
- Runtime 在其支持的时机处理该指令：可能是实时处理，也可能在当前模型生成、工具调用或工具批次边界处理。

约束：

- steer accepted 只表示 Runtime 接受了指令，不表示模型已经读取或执行。
- Steering 的实际生效时机由 Harness、Provider 和当前工具执行状态决定，不能统一承诺为“立即打断当前工具”。
- 只有 Runtime 明确声明并验证支持 steering，才允许使用此策略。
- 若目标不处于可 steering 的活动 Turn，按明确配置处理；不得由 Relay 自行猜测。

### 3.3 FOLLOW_UP：当前 Turn 结束后排队执行

语义：

- 不干扰当前 Turn；
- 将消息作为下一回合输入排队；
- 收到可靠的 Turn 完成事件后，Relay 提交下一回合；
- 如果目标当前空闲，则可以立即作为新 Turn 提交。

实现优先级：

1. 优先使用 Runtime 原生的 follow-up / queue 能力；
2. 如果 Runtime 没有原生队列，则由 Relay 将消息持久化为 pending，在可靠的 turn_completed 事件后再提交。

注意：FOLLOW_UP 等待的是当前 Turn 完成，不等同于等待整个 Execution / WorkUnit 完成。

### 3.4 MAILBOX_ONLY：仅写信箱

语义：

- 消息持久化到 SQLite；
- Relay 不向 Runtime 主动提交通知或正文；
- 接收 Agent 在主动查收信箱时消费。

适用：低优先级消息、人工控制消息、Runtime 状态不可信或不具备安全投递能力的场景。

### 3.5 默认策略与降级纪律

建议默认策略为 FOLLOW_UP。发送方可以显式指定策略；若未指定，则读取接收 Agent 的默认策略。

策略不支持时不得静默改变语义：

| 请求策略 | Runtime 不支持 / 状态未知时的建议行为 |
|---|---|
| ABORT_AND_SUBMIT | 保持 pending，报告 unsupported / unknown；不自动杀进程 |
| STEER | 保持 pending，或仅在发送方明确允许时降级为 FOLLOW_UP |
| FOLLOW_UP | 使用 Relay 自身的持久化队列，等待可靠 Turn 完成事件；缺少事件时保守暂缓 |
| MAILBOX_ONLY | 正常保留在信箱，不主动投递 |

任何降级都应显式记录实际采用的策略和原因。

## 4. 本地 Relay Service 职责

建议采用单进程、本地常驻服务，复用当前项目 SQLite 数据库。初步拆分如下：

1. **Message Observer**：发现新增或待重试消息。
2. **Message Router**：根据 agents 注册表解析 recipient、Runtime、Session 和目标绑定。
3. **Policy Engine**：结合 delivery_policy、Runtime 能力和会话状态决定可执行动作。
4. **Delivery Scheduler**：执行认领、租约、每 Session 串行化、重试、超时和恢复。
5. **Runtime Adapters**：调用 OMP、Claude Code、Codex、OpenCode、Pi 等 Runtime 的原生接口；Tern / Herdr 继续作为终端/工作区 transport 或适配层。

逻辑结构：

~~~text
Agent A / Agent B / Controller
          |
          | send（只写 SQLite）
          v
   SQLite Message Ledger
          |
          v
   Agent Message Relay Service
     |        |         |
 Observer   Router   Policy Engine
                       |
                Delivery Scheduler
                       |
                Runtime Adapter
          +------------+-------------+
          |            |             |
         OMP        Codex / CLI    Tern / Herdr
          |            |             |
          +------------+-------------+
                       |
                 Target Session
                       |
                 read / reply / done
                       |
                       v
                 SQLite Ledger
~~~

### 4.1 消息监听

SQLite 仍然是唯一事实来源。监听机制只负责唤醒调度，不是消息事实本身。

可先采用：

- 对同一个长期连接读取 PRAGMA data_version，观察其他连接提交事务后的变化；
- 变化后扫描可投递消息；
- 定期执行完整 pending / expired-claim 扫描，作为漏唤醒和重启恢复兜底。

data_version 必须在同一个连接上比较前后值；它不是跨连接共享的全局事件序号，也不能替代数据库扫描。轮询间隔、扫描周期和批量大小需要通过本机负载测试确定，不在本稿锁定数值。

不建议当前阶段为了消息唤醒引入 Redis、RabbitMQ、NATS 等外部基础设施。

### 4.2 调度约束

- 每个目标 Session 同一时刻最多执行一个会改变会话输入/状态的 Relay 操作。
- 不同 Session 可以并行调度。
- 消息默认按入库顺序处理；优先级与跨消息合并暂不作为第一阶段必需能力。
- 服务重启后从数据库重建调度，不依赖内存队列恢复。
- 继续复用现有 claim lease / claim-token fencing / revert 机制，避免另建一套认领真相。

## 5. Runtime 生命周期与能力协议

AFTER_TURN / FOLLOW_UP 的正确性依赖 Runtime 对 Turn 生命周期的可观察性。终端静默、屏幕文本或 Agent 自报状态不能被直接当作可靠的 Turn 完成证明。

建议定义统一事件：

| 事件 | 含义 |
|---|---|
| session_started | Runtime Session 已建立 |
| session_ended | Session 生命周期结束 |
| turn_started | 新 Turn 开始 |
| turn_completed | 当前 Turn 自然完成 |
| turn_interrupted | 当前 Turn 已被中断 |
| turn_blocked | 当前 Turn 被审批或人工输入阻塞 |
| runtime_state_unknown | 状态无法可靠判断 |

事件至少携带：

- event_id：事件唯一 ID，用于去重；
- agent_name、session_id、turn_id；
- event_type、event_time；
- source / runtime_name / runtime_version；
- 可选的 correlation_id 与诊断信息。

事件需要持久化或以可恢复方式消费。重复、乱序、过期事件不能让 Agent 状态倒退；具体序列规则留待协议设计阶段确定。

建议 Adapter 暴露能力声明，而不是由 Relay 根据 Harness 名称猜测：

- supports_abort_turn
- supports_steer
- supports_follow_up_queue
- supports_turn_events
- supports_session_identity
- supports_idempotency_key
- supports_delivery_ack

能力声明必须与实际版本、启动方式、Provider 和运行模式绑定；能力未经验证时视为 unknown。

## 6. Harness 能力初步盘点（待版本锁定与实机验证）

以下是研究线索，不是兼容性承诺。不同版本、Provider、启动方式和 TUI / Server / SDK 入口可能有差异。

| Harness | 中断当前 Turn | 执行中 Steering | Follow-up / Queue | 初步集成方向 |
|---|---|---|---|---|
| OMP / Oh My Pi | 有 abort 类原语 | 有 steer 类原语；生效边界受 Provider / 工具执行影响 | 有 follow-up 类原语 | 优先验证 RPC / Runtime 原生接口 |
| Claude Code | Esc / Ctrl+C 等交互中断 | Send Now 在不同执行状态下可能表现为混合语义；不能简单等同纯 steering | 普通输入排队；另有旁路问答能力 | 先验证 CLI/TUI 的外部可控性和版本行为 |
| Codex | App Server 有中断操作 | 交互界面有当前回合追加输入路径，具体边界待版本验证 | App Server 可开启新的 UserTurn；队列行为待验证 | 优先 App Server 协议 |
| OpenCode | 有 Session interrupt | 有 steer 语义 | 有 queue 语义 | 优先 Server API |
| Pi | SDK / RPC 有 abort 类原语 | 有 steer 类原语 | 有 follow-up 类原语 | 优先 SDK / RPC |
| Tern / Herdr | 终端/工作区层面的输入和状态能力，不等同于 Runtime 原生 Turn 控制 | 不能仅凭 paste / prompt 宣称具备安全 steering | 可用于固定通知或已验证的输入提交 | 保留为 transport / workspace adapter，不作为所有 Harness 的统一语义层 |

必须进一步验证：

1. 精确的 Harness 版本和 API 名称；
2. 中断是否保留 Session，以及是否确实停止当前 Turn；
3. Steering 是实时注入、工具边界注入还是下一模型调用前注入；
4. 当前工具调用、后台进程和 Subagent 在中断时的行为；
5. 外部进程能否可靠调用该能力并获得结果；
6. 接受确认是否可与“模型已读取”区分；
7. 重复提交是否支持 message_id / idempotency key 去重。

## 7. Runtime Adapter 初步接口

建议接口表达语义，而不是映射按键：

~~~python
class AgentRuntimeAdapter:
    def capabilities(self, session_id): ...
    def get_state(self, session_id): ...
    def abort_turn(self, session_id, operation_id): ...
    def steer(self, session_id, message, message_id): ...
    def enqueue_follow_up(self, session_id, message, message_id): ...
    def notify_mailbox(self, session_id, message_id): ...
    def subscribe_events(self, session_id): ...
~~~

返回结果需要区分：

- accepted：Runtime 接受了操作；
- applied：Runtime 确认操作已应用到对应 Turn / Session；
- unknown：调用超时或连接中断，无法判断是否已应用；
- rejected：Runtime 明确拒绝；
- unsupported：能力不存在。

不能把 accepted 当作 Agent 已读取消息。消息的 read / reply / done 仍然由 agentmsg 信箱协议证明。

对于 Tern / Herdr 这类终端通知通道，继续采用固定、可信的取信通知，不把用户可控消息正文拼进终端命令或提示符。对于 Runtime 原生 steer / follow-up API，消息正文进入模型上下文是该策略的预期行为，但必须经过发送者身份、目标权限和消息信任边界检查。

## 8. 数据模型演进方向（暂不实施）

尽量兼容现有 messages、agents、tasks、status_log 和消息状态机。

候选增量：

### messages

- delivery_policy：abort_and_submit / steer / follow_up / mailbox_only
- attempt_count
- next_attempt_at
- last_error
- 可选 fallback_policy：仅允许发送方显式指定

kind 继续表达 request / reply / notify 等消息类型，不与 delivery_policy 合并。

### agents

- default_delivery_policy
- runtime_name / runtime_version
- session_id
- runtime_capabilities（或单独能力表）
- last_runtime_event_at

### 新增候选表

- agent_runtime_events：记录生命周期事件和状态来源；
- delivery_attempts：记录每次投递操作、operation_id、实际策略、结果及错误。

是否需要新增表、字段和索引，必须在 schema 设计与迁移评审后决定，不在本初稿中锁定。

## 9. 可靠性、安全与状态语义

继续坚持现有原则：

- SQLite 是唯一事实来源；服务内存状态可丢弃、可重建。
- 使用至少一次投递 + message_id 去重 + 幂等处理；不宣称端到端 exactly-once。
- Runtime 调用超时不等于操作未发生；结果 unknown 时不能盲目重复中断或 steering。
- delivered 只能代表通知或 Runtime 输入被接受；read 代表 Agent 已读取；reply 是回执；done 是消息处理完成。
- 消息完成不等于 ANWG WorkUnit.ACCEPTED。
- working=false、终端静默、屏幕提示符和 Agent 自报 idle 都不能单独证明 Turn 已完成。
- blocked、unknown、目标 Session 不匹配或状态过期时，对破坏性操作 fail-closed。
- Relay 不代替人处理审批，不发送 Esc / Ctrl+C / y 等按键来绕过权限确认。
- 不允许一个 Session 上的并发投递操作相互交错。

## 10. 验收场景（初步）

1. 多个 Agent 只向 SQLite 写消息，单个 Relay Service 可以发现并路由新消息。
2. Relay Service 重启后能恢复 pending 和过期 claimed 消息。
3. 同一 Session 的输入操作严格串行，不会交错。
4. ABORT_AND_SUBMIT 必须先确认当前 Turn 已中断，再提交新消息；中断不确定时不重复提交。
5. STEER 不结束当前 Turn；测试能区分 Runtime 接受、实际注入和 Agent read。
6. FOLLOW_UP 不干扰当前 Turn，Turn 完成后按顺序处理。
7. MAILBOX_ONLY 不触发 Runtime 输入。
8. 不支持某策略、状态未知、Session 失效或 Runtime 断线时，消息不丢失且错误可追踪。
9. 重复事件、重复通知、服务崩溃和投递超时不会导致无审计的重复执行。
10. 至少对 OMP 完成一个真实 Runtime 端到端验证；其他 Harness 按能力矩阵逐个验证，不用 mock 结果替代兼容性结论。

## 11. 建议实施顺序

- **S0 — 能力核验**：固定 OMP、Claude Code、Codex、OpenCode、Pi 的版本和启动模式，逐项验证 abort / steer / follow-up / lifecycle event。
- **S1 — Relay Service 骨架**：单进程常驻、消息监听、pending 扫描、统一路由和恢复；先不改变现有投递语义。
- **S2 — FOLLOW_UP**：优先完成最安全的排队投递与 Turn 完成事件闭环。
- **S3 — STEER**：先实现并验证 OMP 原生适配器，再扩展其他 Harness。
- **S4 — ABORT_AND_SUBMIT**：在确认中断语义、结果确认和幂等边界后实现。
- **S5 — MAILBOX_ONLY 与治理完善**：策略配置、审计、可观测性、压力和故障注入测试。

## 12. 尚待讨论的问题

1. 默认策略是否统一采用 FOLLOW_UP，还是由每个 Agent 注册时配置默认策略？
2. ABORT_AND_SUBMIT 是否只中断 Turn 并保留 Session？本稿暂按此定义。
3. STEER 的正文是否直接进入 Runtime 当前上下文，是否需要消息类型或权限级别限制？
4. Runtime 缺少可靠 turn_completed 事件时，FOLLOW_UP 是一直等待、退化为 MAILBOX_ONLY，还是允许经过授权的状态启发式？
5. Relay Service 是每个项目数据库一个实例，还是支持单实例管理多个项目数据库？
6. 是否需要与 ANWG Core 对接 Execution / WorkUnit 完成事件？本稿暂将其排除在 agentmsg 的核心职责之外。

## 13. 非目标

- 不重写现有 SQLite 信箱和 v2.1 watcher；
- 不在本阶段实现多机消息代理或引入外部消息队列；
- 不让 LLM 负责消息路由、状态判断或投递策略选择；
- 不把终端按键模拟包装成通用 Runtime 中断 / Steering API；
- 不承诺 exactly-once；
- 不把 Turn 完成等同于业务任务完成；
- 不在能力未验证前宣称所有 Harness 都支持所有策略。

## 14. 参考入口

- OMP / Oh My Pi：<https://github.com/can1357/oh-my-pi>
- Claude Code Interactive Mode：<https://docs.anthropic.com/en/docs/claude-code/interactive-mode>
- Codex App Server：<https://developers.openai.com/codex/app-server/>
- OpenCode Server / API：<https://opencode.ai/docs/server/>

以上入口用于后续版本核验。具体 API、行为边界和兼容性结论必须记录 Harness 版本与实测证据。
