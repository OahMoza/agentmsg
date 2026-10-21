# Issue Report: omp TUI frames cannot be captured programmatically when running inside a third-party terminal (Tern)

> Report date: 2026-10-16　|　Severity: medium (functionality intact; integration/observability broken)
> Versions: tern 0.8.3 (e2b8656); omp CLI (distributed with Tern: `%LOCALAPPDATA%\Tern\omp\bin\omp.exe`; startup banner shows node v18.11.0) — *please fill in the omp version number*
> Environment: Windows 10.0.26200 x64, ConPTY

## Summary

omp works perfectly inside Tern (a third-party terminal): it receives keyboard input, executes commands, and holds multi-turn sessions without any problem. However, **its TUI frames never reach the terminal's text model**, so every programmatic text capture of the pane is empty:

- `tern capture <id>` → **0 bytes** (identical when focused or unfocused);
- `tern capture <id> --surfaces` → only omp's **startup banner lines** (which keep updating during the session, e.g. `Turn on /advisor …`) — proving omp **is** continuously writing output to the terminal;
- `tern ctl tree` (the rendered element tree) contains only the pane title for this pane, no TUI body text;
- Meanwhile the UI is fully visible on screen for a human user — i.e. **what the screen shows and what a program can read diverge completely**.

## What we ruled out (controlled experiments)

All control experiments ran in the same environment, using a plain python program writing raw VT sequences directly:

| Hypothesis | Experiment | Result |
|---|---|---|
| Alternate screen causes invisibility | python writes `CSI ?1049h` + text | capture **sees it** (all four variants: default/--html/--ansi/--scrollback consistent) |
| Synchronized output (CSI 2026) drops frames | frame wrapped in `CSI ?2026h … CSI ?2026l` | capture **sees it** |
| Focus-related | capture while focused vs unfocused | identical (omp empty either way) |
| Capture-variant issue | 4 variants × omp pane | all 0 bytes |

In other words: **the standard VT path (including alternate screen and synchronized updates) works correctly on the terminal side** — yet omp's frames do not land in the terminal's text model.

## Inference and requests

Inference: under a third-party terminal, omp's TUI rendering does not go (entirely) through the standard VT text output path. Likely directions:

1. omp detects a non-mainstream terminal / capability set and switches to a degraded or silent rendering mode;
2. output bypasses ConPTY's VT conversion (e.g. direct Win32 Console API writes);
3. output is diff-based redrawing relying on state the terminal does not retain (least likely — other TUIs are visible in the same environment).

**Requests to the team:**

1. A description of omp's rendering/output path when running under ConPTY / third-party terminals;
2. Whether there is a "non-mainstream terminal detected → degraded/silent mode" logic, and if so, the corresponding switch;
3. If the current rendering path is by design, please provide an **official programmatic way to read the current UI content** (e.g. an omp-native dump/screenshot/accessibility subcommand).

## Reproduction steps (~1 minute)

1. `tern split -- <host-pane-id> right --keep-open -- omp` (or launch omp as a child process in any third-party terminal);
2. wait until omp's UI is fully rendered;
3. `tern capture <new-pane-id>` → 0 bytes, while the UI is visible on screen;
4. control group, same environment: `python -c "import sys,time;sys.stdout.write('\x1b[?1049hALT-TEST\x1b[?2026h\x1b[?2026l');sys.stdout.flush();time.sleep(30)"` → capture returns `ALT-TEST`.

## Impact

- Third-party terminal integrations (screen reading, state detection, approval-dialog recognition, accessibility) are unavailable for omp;
- Automated tests / recording tools cannot assert omp's UI state;
- omp's own functionality and stability are unaffected.

---

## 2026-10-16 RESOLVED: root cause was tern 0.8.3, fixed in 0.8.4

The interim "counterexample" hypothesis (hidden session / `--resume`) recorded earlier today is **superseded**. Cross experiment, same machine, one day, `tern --version` = 0.8.4 (6b4535f):

| Matrix cell | Instance | tern serving it | `tern capture` |
|---|---|---|---|
| shown + interactive | Default tab omp | 0.8.4 | visible (10 KB) |
| hidden + interactive | background session (`tern new session`, `shown:false`) with omp | 0.8.4 | visible (1.3 KB) |
| shown + `--resume` | split pane with B's exact launch (`powershell -NoLogo -c omp --resume <jsonl>`), same session file | 0.8.4 | visible (6.5 KB, history replay) |
| hidden + `--resume` | original smoke-session omp ("B") | **0.8.3 daemon** | 0 bytes |

Only the instance served by the **0.8.3 daemon** was invisible. `shown` state and `--resume` were confounds — they tracked which upgrade generation the session belonged to — not causes. The 0.8.3-era controlled experiments above (python `1049h`/`2026` writes visible under 0.8.3) remain valid: the 0.8.3 defect was specific to how omp's frames entered the text model, and this report's original findings describe it accurately. No omp-side change needed; close against tern 0.8.4.

---

## 2026-10-21 addendum (tern 0.9.5): omp rendering mode is a second visibility switch

Same machine, tern 0.9.5 (2863c37). Two omp panes (`tern split … omp.exe`) while omp ran in its **native interactive (non-TUI) form** reproduced the headline symptom exactly: `tern capture` returned only the idle shell prompt line — omp frames absent from the text model — while omp itself worked (mailbox read → reply all succeeded). After omp was restored to its **TUI form**, `tern capture --ansi` on the same panes returned the full frame: welcome screen, conversation transcript, tool calls, and the agent's entire reasoning.

Implications:

1. The 2026-10-16 closure (root cause = tern 0.8.3, fixed in 0.8.4) is necessary but not sufficient: **omp's own rendering mode gates frame visibility independently of tern version**. A 0-byte capture on a current tern is not proof of a tern regression — check omp's rendering form first.
2. Capture discipline: the TUI-mode frame includes the agent's full reasoning, so every capture ingests that text into the orchestrator's context. Capture is an exception-diagnosis tool (stuck/timeout), not a monitoring loop; content verification stays on the mailbox, completion on the output-silence gate. The generalized mechanism and tool mapping (tern/herdr) live in `docs/observation-discipline.md`.
