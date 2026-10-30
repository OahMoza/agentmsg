# 02：验证 Tern 忙碌信号的适用范围

**Blocked by:** 无。

**What to build:** 维护者能够依据可复现的隔离观察，判断 `working` 能否作为特定 Agent 和状态下的额外暂缓条件，避免把未知字段语义接入 watcher。

**Status:** done

- [x] 在自建 pane 中记录版本、目标匹配方法和可取得的运行端信息。
- [x] 调查状态类型及可观察范围，记录无法确认的字段来源和场景。
- [x] 使用独立任务记录确认 omp 任务阶段并记录局限。
- [x] 复现 PowerShell 静默运行时 `working=false`。
- [x] 记录证据不足、不建议接入的结论。
- [x] 未修改生产行为或终端服务，已关闭自建 pane。

## 验证记录

- Tern 客户端 `0.9.8 (f21d9a3)`，隔离 omp `18.14.0`；daemon 版本未取得。目标使用创建响应中的 block ID 与 `inspect --json` 的 `panes[].pane` 精确匹配。
- 隔离 omp 提交计算任务后，`working=true`、`quiet_ms=5538`；独立会话记录确认任务提交并生成最终答案。回答后观察到 `working=false`、`quiet_ms=22397`。此为单次观察，不证明所有工作阶段都可识别。
- PowerShell 执行 30 秒静默等待、完成标记尚未出现时，`working=false`、`quiet_ms=7614`；之后观察到完成标记。`working=false` 不能证明一般进程空闲。
- 保留的退出 pane 报告 `exited=1`、`working=false`；不将该值等同于内部命令的退出码。`working` 在所测结果中是布尔值，来源未查明。
- omp 原生交互形态无法 capture；未验证其他 Agent、其他渲染形态或审批/提问，也未多次覆盖长时间静默工作。结论：证据不足，暂不接入 watcher。
- 复现：在未显示的隔离 session 创建 pane，发送受控任务，以独立会话记录确认阶段，再用 `tern inspect --json` 按 block ID 查询；仅关闭自行创建的 pane。空 session 无 tab，仍保留。
