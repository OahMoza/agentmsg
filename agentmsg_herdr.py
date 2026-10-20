#!/usr/bin/env python3
"""agentmsg 的 Herdr 薄投递适配器（兼容 Herdr 0.9.4）。

所有消息与租约逻辑仍在 agentmsg.py；本文件只解析真实 Agent 状态并投递固定通知。
"""
import json
import os
import subprocess

HERDR = os.environ.get("AGENTMSG_HERDR", "herdr")
READY = {"idle", "done"}


def agent_status(payload):
    """仅从结构化 Herdr Agent 返回值读取状态；不凭终端文字猜测。未知则拒绝投递。"""
    data = json.loads(payload)
    result = data.get("result", {}) if isinstance(data, dict) else {}
    if not isinstance(result, dict):
        return None
    agent = result.get("agent", result)
    if not isinstance(agent, dict):
        return None
    status = agent.get("agent_status", agent.get("status", agent.get("state")))
    return status if isinstance(status, str) else None


def submit_notification(target, message):
    """成功仅代表 Herdr 已接受 Prompt，不代表 Agent 已阅读/完成。

    因 Herdr --skill 建议仅在受管 pane 内控制其 Session，非 HERDR_ENV=1 时 fail-closed。
    """
    if os.environ.get("HERDR_ENV") != "1":
        raise RuntimeError("Herdr watcher 必须从 Herdr 托管的 pane 启动（HERDR_ENV=1）")
    snapshot = subprocess.run(
        [HERDR, "agent", "get", target],
        check=True, capture_output=True, text=True, timeout=15)
    if agent_status(snapshot.stdout) not in READY:
        return False
    # 原子提交 Prompt+Enter；不要 --wait，送达不是完成确认。
    subprocess.run(
        [HERDR, "agent", "prompt", target, message],
        check=True, capture_output=True, text=True, timeout=15)
    return True
