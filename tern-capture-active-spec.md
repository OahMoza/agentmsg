# tern 规格：capture 应返回「当前活动屏」的文本（含备用屏）

> **2026-10-15 修订**：受控实验（python 直写 `1049h` 备用屏 + `2026` 同步更新）证明 **tern 0.8.3 的 capture 已经遵循活动屏语义**——四变体（默认/--html/--ansi/--scrollback）一致可见备用屏内容，退出回主屏无回归。原规格的验收标准（§5）事实上已满足，**无需实现**。
> 真正的异常收窄为：**omp 的 TUI 帧不进入 tern 的文本模型**（见 §8）。本文件保留作为：① 验收基准（回归测试用例可复用）；② omp 帧丢失问题的调查起点。

---

## 1. 现状与问题（实测证据）

| 实验（pane 跑 omp，全屏 TUI 激活备用屏） | 结果 |
|---|---|
| `tern capture <id>` | **0 字节**——只读主屏文本模型 |
| `tern capture <id> --surfaces` | 59 字节——只有 TUI 启动前打在主屏的横幅残影 |
| `tern capture <id> --scrollback --ansi` | 0 字节 |
| `tern ctl tree` | **262KB 活控件树，TUI 全部文字在场**（`span.tk-string` 节点带 text+rect；实测命中 `PONG`/`agentmsg`/提示语等）|

结论：**备用屏的样式化文本网格在渲染模型里已经存在**（`tk-string` 就是它渲染出来的），只是 capture 的读取路径硬编码了主屏。这是暴露问题，不是实现问题。

附带问题：`ctl tree` 虽含全部文本，但它是**私有渲染 DOM**（`tn-stage`/`sf-term-clip`/`tk-string` 随版本会变），且无 per-block 的锚点（树里无 block id 属性，矩形归属要猜）——不可作为 API 依赖。

## 2. 目标行为（规格）

**`tern capture <id>` 返回该 block 当前活动屏的文本**：TUI 激活备用屏时返回备用屏内容；否则返回主屏（现状不变）。

### 2.1 各 flag 在活动屏语义下的定义

| flag | 备用屏激活时 | 仅主屏时 |
|---|---|---|
| （默认） | 备用屏可见网格 → 按行纯文本 | 主屏视口（现状）|
| `--scrollback` | 备用屏（它无历史，不掺主屏回滚）| 主屏 + 回滚（现状）|
| `--html` | 同默认内容，保留行结构/样式标记 | 现状 |
| `--ansi` | 从**样式化网格重序列化**为 VT（不要透传原始流）| 现状 |
| `--surfaces` | 保持现语义（surface 分区文本），分区应含活动屏 | 现状 |

### 2.2 默认行为变更的兼容性论证

推荐**直接改默认**而非加 `--alt` 开关：现状下脚本对备用屏应用拿到的是"空"，没有任何消费者能依赖"空"；返回活动屏是严格更有用的超集。若仍要保守，退一步方案是新加 `--active` 并默认开（等价于改默认，留个关闭口子）。`--ansi` 从网格重序列化而非透传的理由：TUI 局部重绘的原始流不可读（光标移动/差分转义），重序列化结果稳定、可 diff、可断言。

## 3. 实现提示（基于外部观测，供定位）

- 渲染侧已维护每 block 的样式化文本网格（`tk-string` 节点的来源），1049 切换时模型里就是"另一块 buffer"。capture 路径改为读 `block 当前活动 buffer` 而非固定主屏——预计是**数据源选择**一处改动，而非新解析。
- 备用屏切换的 VT 变体需都归一：`1049`（save cursor + switch + clear）、`47`/`1047`（仅 switch）、`1048`（仅 cursor）。多数现代 TUI 用 1049。
- 边界情形清单：
  - 程序退出（`keep_open` pane）→ 已切回主屏，capture 应回到 shell 内容；
  - 备用屏中途 resize → 按当前网格快照，不需特殊处理；
  - 从未进过备用屏的 block → 行为与今天完全一致（回归基线）；
  - headless `tern serve` + `ctl` 路径应与窗口路径同源（同一文本模型）。

## 4. 可选增强（小而值）

1. `tern ls --json` 的 block 记录加 `"alt": true|false`（当前哪块 buffer 激活）——消费者可据此决定 capture 语义与等待策略。
2. `tern capture <id> --json` 信封输出：`{"block": id, "screen": "alt"|"main", "cols": C, "rows": R, "text": "…"}`——自描述，方便脚本断言。

## 5. 验收标准

1. pane 跑 omp/vim：`tern capture <id>` 非空且含可见 TUI 文案（如 omp 输入框/状态栏字样）。
2. TUI 退出后 capture 回到 shell 内容。
3. 备用屏激活时 `--scrollback` 不泄漏主屏历史。
4. text/html/ansi 三种变体内容一致（同一网格的两种序列化）。
5. 纯 shell pane：输出与 0.8.3 现状逐字节等价（回归）。
6. `tern serve`（headless）+ `ctl` 与窗口行为一致。

## 6. agentmsg 侧对接（已就绪，无需等 tern）

`agentmsg.py` 的投递安检现为两层：**输出静默门**（`tern wait --until idle`，走 PTY 输出流，与渲染屏无关，忙时注入已闭环）+ **capture 审批文案检查**（对内联渲染的 claude 有效，真机实证命中 `allow`/`esc to`）。omp 实测自动放行、无审批框，运营风险低。

## 7. 附：为什么不用 `ctl tree` 兜底

私有 DOM（结构随版本漂移）+ 无 per-block 锚点（block id 仅作为被裁剪的标题文本出现过一次）+ 整窗树需自行做矩形/标题匹配——三重脆弱。capture 是官方 API，改它是把私有结构收敛成契约。

## 8. 遗留异常：omp 的帧为何不可见（待源码级调查，2026-10-15）

**现象**：omp pane（`-- powershell -NoLogo -c omp` 启动，node v18.11.0）
- `tern capture` **0 字节**——聚焦/失焦均如此；
- `--surfaces` 可见其横幅行（且随会话滚动更新：`Turn on /advisor …`）→ omp **确实在往终端写**；
- `ctl tree` 的 tk-string 里只有 pane 标题，无 TUI 正文；
- omp 功能完全正常（收通知、执行命令、回信都成功）。

**已排除**（受控实验，复现脚本均为直写 VT 的 python）：
1. ~~备用屏盲~~：python 直写 `1049h` → capture 可见（52 字节，四变体一致）；
2. ~~同步更新丢帧~~：`2026h` 包裹的帧 → capture 可见；
3. ~~焦点相关~~：聚焦/失焦 capture 同为 0；
4. ~~shell 包装层~~：omp 经 powershell 包装启动（与横幅可见矛盾不大，但非主因）。

**下一步（需 tern/omp 源码）**：
- tern 侧：抓取该 block 的输出流（`tern events` 或调试日志），确认 omp 的渲染字节是否到达解析器、模型里是否留存——区分"解析丢弃"与"渲染保留失败"；
- omp 侧：确认其 TUI 用的绘制通道（是否绕过 ConPTY 的 VT 路径，如 Win32 Console API 直写、或检测到非常规终端后切换了静默模式）。
- 对照组可复用本规格 §5 的 python 用例（`altscreen_test.py`/`sync_test.py` 思路），一分钟复现差异。

**2026-10-16 收窄**：同机发现**可完全捕获的 omp 实例**（Default session、`shown: true`、pwsh 内交互启动：idle 提示符、干活帧、备用屏内容全部可见）；0 字节实例全部位于 `shown: false` 的 session（经 `omp --resume` 启动）。「omp 帧不可见」收窄为两个候选：**隐藏 session/tab 不进 capture 合成路径**（首要嫌疑）或 **`--resume` 启动路径**。最小实验：同一 omp 二进制在 shown/hidden × resume/交互 交叉复测。

**2026-10-16 定案（交叉实验完成，假设作废）**：四格矩阵在 `tern 0.8.4 (6b4535f)` 下全部可见——shown+交互 (10KB)、hidden+交互（`tern new session` 后台 session，1.3KB）、shown+`--resume`（复刻原 B 的启动命令与同一 jsonl，6.5KB 带历史回放）；唯一 0 字节的是 **0.8.3 daemon 遗留 session** 里的实例。shown/`--resume` 是混杂变量（跟着升级代次走），不是原因。根因：**0.8.3 的 capture 路径丢 omp 帧，0.8.4 已修**。§1–§7 的备用屏规格在 0.8.4 下已实测满足（omp 帧可读），本文件留作 0.8.3 回归参考。
