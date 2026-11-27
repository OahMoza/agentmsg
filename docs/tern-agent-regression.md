# Tern × Agent 投递协议回归用例集

> 目的：固化「主控通过 tern 向各 coding agent 的 pane 投递消息」这条链路的协议行为，
> 让「消息停留在对话框 / agent 毫无反应」这类问题可回归、可定位。
> 首次执行：2026-11-27（上海），执行人：grok（Oahmo 会话）。
> 环境：Windows 11、ConPTY、Tern 0.9.35 (ccfae03)。

## 1. 被测对象与版本

| Agent | 版本 | 启动方式 | 会话落盘 |
|---|---|---|---|
| omp | 19.2.1 | `omp` | `~/.omp/agent/sessions/<cwd 编码>/*.jsonl` |
| grok | 1.0.92 | `grok` | `~/.grok/sessions/<cwd 编码>/<sid>/chat_history.jsonl` |
| claude | 2.1.332 | `claude --permission-mode bypassPermissions` | `~/.claude/projects/<cwd 编码>/*.jsonl` |
| opencode | 1.19.6 | `opencode` | `~/.local/share/opencode/storage/` |
| pi | 1.6.0 | `pi` | `~/.pi/agent/` |

## 2. 协议语义（本用例集的判定基础）

- `tern send <block> paste TEXT` **只把文字放进输入框，不提交**。
- `tern send <block> keys enter` 才触发提交；`tern run <block> COMMAND` 是「打字+回车」的原子等价物。
- 因此所有「正向」用例必须由 paste + enter 两步构成；只 paste 的负向用例用于确认协议语义，不是故障。

## 3. 用例定义

| 编号 | 名称 | 步骤 | 预期 |
|---|---|---|---|
| TC-01 | 就绪与可见性 | 开 pane → `tern wait --until idle=3000`；记录 `tern capture` 长度 | wait 通过；记录各 agent 捕获是否可见 |
| TC-02 | 仅粘贴（负向） | `paste "<TAG> 请只回复OK"`，等 4s | 无任何新会话记录：文字停留在 composer，agent 未收到 |
| TC-03 | 粘贴+回车（正向） | 承 TC-02 的文本 → `keys enter` → `wait --until idle=3000` | 出现 assistant 回复 |
| TC-04 | 多行通知+回车 | `paste "<TAG> 验证多行\n第二行：请只回复OK"` → `keys enter` | 两行完整提交并回复（对齐 agentmsg `notify_text` 的两行格式） |
| TC-05 | 残留叠加（观察） | 连续两次 paste 中间不按 enter | 记录 composer 是否累积拼接 |
| TC-06 | watcher 端到端 | `agentmsg send` + `watch --once`，查 `messages.status` | delivered/read；静默门窗口足够宽（见 PR #1） |

## 4. 实测结果（2026-11-27）

| Agent | TC-01 可见性 | TC-02 仅粘贴 | TC-03 粘贴+enter | TC-04 多行+enter |
|---|---|---|---|---|
| claude 2.1.332 | 可见 | 停留未提交 ✅ | 回复 OK ✅ | 两行提交，回复 OK ✅ |
| opencode 1.19.6 | 可见 | 停留未提交 ✅ | Thought 3.7s → OK ✅ | 两行提交，Thought 1.4s → OK ✅ |
| pi 1.6.0 | 可见 | 停留未提交 ✅ | 回复 OK ✅ | 两行提交，回复 OK ✅ |
| grok 1.0.92 | 可见 | 停留未提交 ✅ | 回复 ✅ | 两行提交，Worked 7.7s ✅ |
| omp 19.2.1 | **不可见（0 字节）** | 停留未提交 ✅ | 回复 ✅ | 两行提交，回复 ✅ |

结论：五种 agent 的提交协议一致——**Enter 是唯一提交动作，多行安全**。差异集中在观测性与残留行为（下节）。

## 5. 差异与风险清单

1. **omp 捕获不可见**：原生交互模式下 `tern capture` 恒为 0 字节（见 `../omp-capture-issue.md`；TUI 形态可见）。影响：watcher 的审批词表安检（`pane_unsafe`）对 omp 空转，无法拦截审批框——投递纪律只能靠 `tern wait --until idle` 静默门。
2. **grok 残留叠加**：第一次 paste 未提交时，第二次 paste 会追加到同一 composer（TC-05 实测拼接成一条消息）。若编排方重试投递，agent 会收到两条指令粘连的文本。建议：重发前先 `capture` 确认 composer 为空，或先 `keys esc` 清空。
3. **watcher 静默门**：`deliver_one` 原用 `--timeout 3`，刚活跃过的 pane 易被误判「疑似忙」而 skip，消息长期留 pending（本次排查的根因，已改 10s，见 PR #1）。
4. **绑定漂移**：`register --block` 的 block id 随 pane 关闭失效；新 pane 必须重新注册。
5. **取信指引依赖 cwd**：`notify_text` 让 agent 执行裸 `python agentmsg.py read <id>`，脚本须在 agent 的 cwd 或 PATH 中，否则取信失败（本次实测 omp 自行找到了正确路径）。

## 6. 复现命令片段

```bash
# 开 pane（cwd 统一 C:\Users\oahmo\work）
tern split --json <host> down --keep-open --cwd C:\Users\oahmo\work -- claude --permission-mode bypassPermissions
tern wait <block> --until idle=3000 --timeout 90

# TC-02 负向
tern send <block> paste '<TAG> 请只回复OK'
# 断言：会话落盘无 <TAG>

# TC-03 正向
tern send <block> keys enter
tern wait <block> --until idle=3000 --timeout 150

# TC-04 多行
note=$'<TAG> 验证多行\n第二行：请只回复OK'   # Windows 下用 pwsh: "line1`nline2"
tern send <block> paste "$note"; tern send <block> keys enter

# 收尾
tern close <block>
```

## 7. 相关

- 静默门修复：PR #1（`fix/quiet-gate-timeout`）
- omp 捕获问题档案：`../omp-capture-issue.md`
- 观测纪律：`../../skills/tern/SKILL.md`
