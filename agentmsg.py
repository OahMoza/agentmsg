#!/usr/bin/env python3
"""agentmsg — 项目内 agent 信箱（SQLite 单文件，零依赖）

每进一个新项目执行一次 `agentmsg init`，建 <cwd>/.agent-mail/mail.db。
把 `.agent-mail/` 加进 .gitignore（运行时状态不入库）。

约定：
  - 收件人必须是已注册 agent 或 "human"
  - 消息短文本放 body，长内容用 --file 存附件
  - status_log 只保留近 30 天，任何命令都会顺手清理
"""
import argparse, json, os, secrets, shutil, sqlite3, subprocess, sys, time, uuid
from pathlib import Path

STALE_MINUTES = 15  # ponytail: 状态租约固定 15 分钟；要可调再加 --stale
CLAIM_LEASE_MINUTES = 2  # ponytail: 认领租约 2 分钟（paste 最多 ~30s），只兜崩溃，够用
WATCH_LOCK_MINUTES = 10  # ponytail: watcher 锁残留阈值，超时可接管
IDLE_GATE_MS = 1500  # ponytail: 投递前输出静默门（tern skill 纪律：不低于 1500，宁可多等）
TERN = os.environ.get("AGENTMSG_TERN", "tern")  # 测试钩子：可指向 stub
APPROVAL_MARKERS = (  # 启发式安检清单（tern skill 的 capture 判定纪律）；漏报不保证，宁跳过
    "do you want", "allow", "approve", "(y/n)", "[y/n]",
    "yes/no", "❯ 1.", "esc to",
)  # 不含裸 "❯"：omp 空闲提示符就是 ❯，裸匹配会把 omp 投递永久卡死（实测）；
   # 也不含裸 "permission"：claude 常态状态栏 "bypass permissions on" 会误杀。
   # "❯ 1." 是 claude 审批框选项行（❯ 1. Yes）的精确特征。

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT,
  tern_block_id INTEGER,
  herdr_target TEXT,
  transport TEXT NOT NULL DEFAULT 'tern',
  state TEXT,
  state_updated_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  assigned_to TEXT,
  state TEXT NOT NULL DEFAULT 'queued',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  subject TEXT,
  body TEXT NOT NULL DEFAULT '',
  payload_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  delivered_at TEXT,
  claimed_at TEXT,
  claim_token TEXT,
  read_at TEXT,
  done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_inbox ON messages(recipient, status, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, created_at);
CREATE TABLE IF NOT EXISTS status_log(
  id INTEGER PRIMARY KEY,
  agent TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_status_agent ON status_log(agent, created_at);
"""

def db_path(args):
    return Path(args.db)

def connect(args):
    p = db_path(args)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL 坑位对策（zylos 文章）：busy_timeout 必设；短连接天然避免 checkpoint 饥饿
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}  # 旧库迁移
    if "state" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN state TEXT")
    if "state_updated_at" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN state_updated_at TEXT")
    if "herdr_target" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN herdr_target TEXT")
    if "transport" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN transport TEXT NOT NULL DEFAULT 'tern'")
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "delivered_at" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN delivered_at TEXT")
    if "claimed_at" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN claimed_at TEXT")
    if "claim_token" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN claim_token TEXT")
    prune(conn, p)  # 懒清理，不设 cron
    recover_claims(conn)  # 崩溃滞留的认领过期回收（任何命令顺手做）
    return conn

def recover_claims(conn):
    """A：认领超租约（watcher 崩在 paste 前）→ 回到 pending 重投。"""
    conn.execute(
        "UPDATE messages SET status='pending', claimed_at=NULL, claim_token=NULL "
        "WHERE status='claimed' AND claimed_at < datetime('now', ?)",
        (f"-{CLAIM_LEASE_MINUTES} minutes",))
    conn.commit()

def prune(conn, root):
    conn.execute("DELETE FROM status_log WHERE created_at < datetime('now','-30 days')")
    old = conn.execute(
        "SELECT id, payload_path FROM messages "
        "WHERE status IN ('done','expired') AND created_at < datetime('now','-30 days')").fetchall()
    if old:
        for r in old:
            conn.execute("DELETE FROM messages WHERE id=?", (r["id"],))
        conn.commit()
        for r in old:  # 行删干净后清孤儿附件；只动自己 attachments 目录里的文件
            if r["payload_path"]:
                p = Path(r["payload_path"])
                try:
                    if p.is_file() and p.parent.name == "attachments" and p.parent.parent == root.parent:
                        p.unlink()
                except OSError:
                    pass

def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)

def out(rows, cols, as_json):
    if as_json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    for r in rows:
        print("  ".join(f"{c}={r[c]}" for c in cols if r[c] is not None))

def need_agent(conn, name):
    if name == "human":
        return
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
        known = [r["name"] for r in conn.execute("SELECT name FROM agents")]
        die(f"未注册的收件人 '{name}'，已注册: {known or '（无，先 register）'} 或用 human")

def cmd_wait(a):
    conn = connect(a)
    cond, params = "recipient=? AND id>?", [a.to, a.after_id]
    if a.kind:
        cond += " AND kind=?"
        params.append(a.kind)
    if a.thread:  # D：只等指定 thread 的回信，跨任务/并发批次不串信
        cond += f" AND thread_id IN ({','.join('?' * len(a.thread))})"
        params += a.thread
    deadline = time.time() + a.timeout
    while True:
        rows = conn.execute(f"SELECT * FROM messages WHERE {cond} ORDER BY id", params).fetchall()
        if len(rows) >= a.expect:
            out(rows, ("id", "sender", "thread_id", "kind", "subject", "body"), a.json)
            return
        if time.time() >= deadline:
            print(f"timeout: {len(rows)}/{a.expect} 封到达（exit 124）", file=sys.stderr)
            sys.exit(124)
        time.sleep(2)

def cmd_agents(a):
    conn = connect(a)
    rows = conn.execute(
        "SELECT name,kind,transport,tern_block_id,herdr_target,state,state_updated_at,"
        "(state_updated_at < datetime('now',?)) AS stale "
        "FROM agents ORDER BY name", (f"-{STALE_MINUTES} minutes",)).fetchall()
    out(rows, ("name", "kind", "transport", "tern_block_id", "herdr_target", "state", "state_updated_at", "stale"), a.json)

def cmd_sweep(a):
    conn = connect(a)
    # C：working 不判死——长任务可能几小时不碰信箱；静默的 working 靠 agents 列表的
    # stale 标记 + tern capture 由主控裁决，不自动枪毙（详见方案 v2.1 §6）
    cur = conn.execute(
        "UPDATE agents SET state='offline' WHERE state IS NOT NULL "
        "AND state NOT IN ('offline','working') "
        "AND state_updated_at < datetime('now',?)", (f"-{STALE_MINUTES} minutes",))
    conn.commit()
    print(f"ok: {cur.rowcount} 个超时 agent 判死置 offline（working 豁免，防误杀长任务）")

def cmd_task_new(a):
    conn = connect(a)
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("INSERT INTO tasks(title,assigned_to) VALUES(?,?)", (a.title, a.assign))
    conn.commit()
    print(f"ok: task {cur.lastrowid}: {a.title}" + (f" → {a.assign}" if a.assign else ""))

def cmd_task_list(a):
    conn = connect(a)
    cond, params = "1=1", []
    if a.state:
        cond, params = "state=?", [a.state]
    rows = conn.execute(f"SELECT id,title,assigned_to,state,created_at,updated_at FROM tasks WHERE {cond} ORDER BY id", params).fetchall()
    out(rows, ("id", "title", "assigned_to", "state", "updated_at"), a.json)

def cmd_task_set(a):
    conn = connect(a)
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE tasks SET state=?, assigned_to=COALESCE(?,assigned_to), updated_at=datetime('now') WHERE id=?",
        (a.state, a.assign, a.id))
    conn.commit()
    print(f"ok: task {a.id} → {a.state}" if cur.rowcount else f"error: task {a.id} 不存在")
    sys.exit(0 if cur.rowcount else 1)

def pane_unsafe(text):
    """B：capture 文本里出现审批/提问特征 → 视为 pane 不安全，暂缓投递。"""
    t = (text or "").lower()
    return any(m in t for m in APPROVAL_MARKERS)

def _tern(block, *rest, timeout=15):
    return subprocess.run([TERN, "send" if rest else "capture", str(block), *rest],
                          check=True, timeout=timeout, capture_output=True, text=True)

def capture(block):
    return _tern(block).stdout

def notify_text(row):
    """B：只注入固定格式通知（id+来源+预览+取信指引），正文绝不进 pane。"""
    prev = (row["subject"] or row["body"] or "").replace("\n", " ")[:100]
    return (f"[agentmsg] 新消息 msg {row['id']} 来自 {row['sender']}（{row['kind']}）：{prev}\n"
            f"取信: agentmsg read {row['id']}")

def claim_one(conn, name):
    """A：pending →(原子认领)→ claimed+租约+token。返回 None=没消息 / True=被抢 / dict=认领成功。"""
    row = conn.execute(
        "SELECT * FROM messages WHERE recipient=? AND status='pending' ORDER BY id LIMIT 1",
        (name,)).fetchone()
    if not row:
        return None
    token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE messages SET status='claimed', claimed_at=datetime('now'), claim_token=? "
        "WHERE id=? AND status='pending'", (token, row["id"]))
    conn.commit()
    if cur.rowcount != 1:
        return True  # 被别的 watcher 抢走
    return {**dict(row), "claim_token": token}

def finalize(conn, mid, token):
    """A：fencing——只有租约仍属于本 token 才能落 delivered。"""
    cur = conn.execute(
        "UPDATE messages SET status='delivered', delivered_at=datetime('now') "
        "WHERE id=? AND status='claimed' AND claim_token=?", (mid, token))
    conn.commit()
    return cur.rowcount == 1

def revert(conn, mid, token):
    cur = conn.execute(
        "UPDATE messages SET status='pending', claimed_at=NULL, claim_token=NULL "
        "WHERE id=? AND status='claimed' AND claim_token=?", (mid, token))
    conn.commit()
    return cur.rowcount == 1

def deliver_one(conn, name):
    """投一封。返回 'empty'|'unbound'|'busy'|'skipped'|'failed'|'delivered'。
    每次调用都重读绑定（C：重绑/换 pane 立即生效，旧 watcher 不再打旧 pane）。"""
    ag = conn.execute("SELECT name,transport,tern_block_id,herdr_target FROM agents WHERE name=?", (name,)).fetchone()
    if ag and ag["transport"] == "herdr":
        return deliver_one_herdr(conn, name, ag["herdr_target"])
    if not ag or not ag["tern_block_id"]:
        return "unbound"
    got = claim_one(conn, name)
    if got is None:
        return "empty"
    if got is True:
        return "busy"
    row = got
    try:
        # 静默门：输出未静默（agent 在干活）→ 不注入。对主屏/备用屏 TUI 都生效
        # （判定走 PTY 输出流，不依赖画面），补 capture 对 alt-screen 失明的洞
        wr = subprocess.run([TERN, "wait", str(ag["tern_block_id"]),
                             "--until", f"idle={IDLE_GATE_MS}", "--timeout", "3"],
                            capture_output=True, text=True, timeout=10)
        if wr.returncode != 0:
            revert(conn, row["id"], row["claim_token"])
            print(f"skip: msg {row['id']} 暂缓——pane 输出未静默（疑似忙），保持 pending", file=sys.stderr)
            return "skipped"
        if pane_unsafe(capture(ag["tern_block_id"])):  # B：先安检再打字（审批框文案）
            revert(conn, row["id"], row["claim_token"])
            print(f"skip: msg {row['id']} 暂缓——pane 疑似在等审批/提问，保持 pending 稍后重试", file=sys.stderr)
            return "skipped"
        _tern(ag["tern_block_id"], "paste", notify_text(row))
        _tern(ag["tern_block_id"], "keys", "enter")
        if finalize(conn, row["id"], row["claim_token"]):
            print(f"delivered: msg {row['id']} → {name} (block {ag['tern_block_id']})")
            return "delivered"
        return "busy"  # 租约被回收：别动，等新一轮
    except Exception as e:
        revert(conn, row["id"], row["claim_token"])
        print(f"warn: msg {row['id']} 投递失败已退回队列: {e}", file=sys.stderr)
        return "failed"


def deliver_one_herdr(conn, name, target):
    """Herdr 投递：沿用相同的消息认领、租约、fencing 和回退机制。
    Herdr agent.prompt 提交输入；delivered 仅意味着 Herdr 接受提交，不代表 Agent 已处理。
    """
    if not target:
        return "unbound"
    got = claim_one(conn, name)
    if got is None:
        return "empty"
    if got is True:
        return "busy"
    row = got
    try:
        from agentmsg_herdr import submit_notification
        if not submit_notification(target, notify_text(row)):
            revert(conn, row["id"], row["claim_token"])
            print(f"skip: msg {row['id']} 暂缓——Herdr Agent 非 idle/done，保持 pending", file=sys.stderr)
            return "skipped"
        if finalize(conn, row["id"], row["claim_token"]):
            print(f"delivered: msg {row['id']} → {name} (herdr target={target})")
            return "delivered"
        return "busy"
    except Exception as e:
        revert(conn, row["id"], row["claim_token"])
        print(f"warn: msg {row['id']} Herdr 投递失败或结果不明，已退回队列: {e}", file=sys.stderr)
        return "failed"

def acquire_lock(lockfile):
    """B：watcher 单例保险（同 pane 并发 paste 会交叠）。O_EXCL 建锁 + pid；
    残留锁（超 WATCH_LOCK_MINUTES）可接管。"""
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        if time.time() - lockfile.stat().st_mtime > WATCH_LOCK_MINUTES * 60:
            lockfile.unlink(missing_ok=True)
            return acquire_lock(lockfile)
        return False

def cmd_watch(a):
    conn = connect(a)
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (a.name,)).fetchone():
        die(f"未注册的 agent: {a.name}")
    lockfile = db_path(a).parent / f"watch-{a.name}.lock"
    if not acquire_lock(lockfile):
        die(f"另一个 watcher 正在投递 {a.name}（{lockfile}）。确认真没有后删除该文件")
    def loop(once):
        while True:
            st = deliver_one(conn, a.name)
            if st == "unbound":
                print(f"warn: {a.name} 未绑定有效 Tern block 或 Herdr target，暂不投递", file=sys.stderr)
            if st == "delivered" or (once and st == "busy"):
                continue
            if once:
                return  # empty/unbound/skipped/failed 都停：留给下一次 cron/常驻实例
            time.sleep(a.interval)
    print(f"watching {a.name}，每 {a.interval}s 查一次，Ctrl-C 退出")
    try:
        loop(a.once)
    except KeyboardInterrupt:
        pass
    finally:
        lockfile.unlink(missing_ok=True)

def cmd_init(a):
    connect(a)
    print(f"ok: {db_path(a)}（WAL 已启用，.agent-mail/ 记得加 .gitignore）")


def cmd_register(a):
    conn = connect(a)
    if a.block is not None and a.herdr_target is not None:
        die("不可同时指定 --block (Tern) 和 --herdr-target (Herdr)")
    old = conn.execute(
        "SELECT kind,tern_block_id,herdr_target,transport,state FROM agents WHERE name=?",
        (a.name,)).fetchone()
    if a.herdr_target is not None:
        transport, block, target = "herdr", None, a.herdr_target
    elif a.block is not None:
        transport, block, target = "tern", a.block, None
    elif old:
        transport, block, target = old["transport"], old["tern_block_id"], old["herdr_target"]
    else:
        transport, block, target = "tern", None, None

    changed = not old or (
        transport, block, target
    ) != (old["transport"], old["tern_block_id"], old["herdr_target"])
    state = "idle" if changed else old["state"] or "idle"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO agents(name,kind,tern_block_id,herdr_target,transport,state,state_updated_at) "
        "VALUES(?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(name) DO UPDATE SET "
        "kind=COALESCE(excluded.kind,agents.kind),"
        "tern_block_id=excluded.tern_block_id,"
        "herdr_target=excluded.herdr_target,"
        "transport=excluded.transport,"
        "state=excluded.state,"
        "state_updated_at=datetime('now')",
        (a.name, a.kind, block, target, transport, state))
    conn.commit()
    print(f"ok: {a.name} (kind={a.kind} transport={transport} target={target if transport == 'herdr' else block})")

def cmd_send(a):
    conn = connect(a)
    need_agent(conn, a.to)
    r = conn.execute(
        "SELECT state, (state_updated_at < datetime('now',?)) AS stale FROM agents WHERE name=?",
        (f"-{STALE_MINUTES} minutes", a.to)).fetchone()
    if r and r["stale"] and r["state"] != "offline":
        print(f"warn: {a.to} 状态 {r['state']} 已超 {STALE_MINUTES} 分钟未更新，可能已死——先 sweep 或等它自证", file=sys.stderr)
    body = a.body if a.body is not None else sys.stdin.read()
    thread = a.thread or f"t{secrets.token_hex(3)}"
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "INSERT INTO messages(sender,recipient,thread_id,kind,subject,body) VALUES(?,?,?,?,?,?)",
        (a.sender, a.to, thread, a.kind, a.subject, body))
    mid = cur.lastrowid
    if a.file:
        src = Path(a.file)
        if not src.is_file():
            die(f"附件不存在: {src}")
        dest = db_path(a).parent / "attachments" / f"{mid}-{src.name}"
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(src, dest)
        conn.execute("UPDATE messages SET payload_path=? WHERE id=?", (str(dest), mid))
    conn.commit()
    print(f"ok: msg {mid} → {a.to} thread={thread}")

def cmd_inbox(a):
    conn = connect(a)
    st = "" if a.all else " AND status='pending'"
    rows = conn.execute(
        f"SELECT id,sender,thread_id,kind,subject,status,created_at FROM messages "
        f"WHERE recipient=?{st} ORDER BY id", (a.agent,)).fetchall()
    out(rows, ("id", "sender", "thread_id", "kind", "subject", "status", "created_at"), a.json)

def cmd_read(a):
    conn = connect(a)
    r = conn.execute("SELECT * FROM messages WHERE id=?", (a.id,)).fetchone()
    if not r:
        die(f"消息不存在: {a.id}")
    conn.execute("UPDATE messages SET status='read', read_at=datetime('now') WHERE id=? AND status='pending'", (a.id,))
    conn.commit()
    if a.json:
        print(json.dumps(dict(r), ensure_ascii=False, indent=2))
        return
    print(f"[{r['id']}] {r['sender']} → {r['recipient']}  thread={r['thread_id']} kind={r['kind']} status={r['status']}")
    if r["subject"]:
        print(f"subject: {r['subject']}")
    print(r["body"])
    if r["payload_path"]:
        print(f"[附件] {r['payload_path']}")

def cmd_reply(a):
    conn = connect(a)
    r = conn.execute("SELECT sender,recipient,thread_id FROM messages WHERE id=?", (a.id,)).fetchone()
    if not r:
        die(f"消息不存在: {a.id}")
    sys.argv = ["agentmsg", "send", r["recipient"], r["sender"], a.body, "--thread", r["thread_id"], "--kind", "reply"]
    args2 = build().parse_args(sys.argv[1:])
    args2.db = a.db
    cmd_send(args2)

def cmd_done(a):
    conn = connect(a)
    cur = conn.execute("UPDATE messages SET status='done', done_at=datetime('now') WHERE id=?", (a.id,))
    conn.commit()
    print(f"ok: {cur.rowcount} 条标记 done" if cur.rowcount else f"error: 消息不存在 {a.id}")
    sys.exit(0 if cur.rowcount else 1)

def cmd_status(a):
    conn = connect(a)
    if not conn.execute("SELECT 1 FROM agents WHERE name=?", (a.agent,)).fetchone():
        die(f"未注册的 agent '{a.agent}'，先 register 再报状态")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO status_log(agent,state,detail) VALUES(?,?,?)", (a.agent, a.state, a.detail))
    conn.execute("UPDATE agents SET state=?, state_updated_at=datetime('now') WHERE name=?", (a.state, a.agent))
    conn.commit()
    print(f"ok: {a.agent} → {a.state}")

def cmd_log(a):
    conn = connect(a)
    rows = conn.execute(
        "SELECT agent,state,detail,created_at FROM status_log WHERE created_at > datetime(?) ORDER BY id DESC",
        (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - a.days * 86400)),)).fetchall()
    if a.agent:
        rows = [r for r in rows if r["agent"] == a.agent]
    out(rows, ("agent", "state", "detail", "created_at"), a.json)

def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(Path.cwd() / ".agent-mail" / "mail.db"))
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("init").set_defaults(fn=cmd_init)
    q = s.add_parser("agents"); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_agents)
    q = s.add_parser("wait"); q.add_argument("to"); q.add_argument("--expect", type=int, default=1)
    q.add_argument("--after-id", type=int, default=0, dest="after_id"); q.add_argument("--kind")
    q.add_argument("--thread", action="append", help="只等这些 thread 的回信（可重复，防跨任务串信）")
    q.add_argument("--timeout", type=int, default=600); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_wait)
    q = s.add_parser("register"); q.add_argument("name"); q.add_argument("--kind"); q.add_argument("--block", type=int)
    q.add_argument("--herdr-target", help="Herdr 中唯一的 Agent 名称或当前 pane ID；切换绑定时清除 Tern block")
    q.set_defaults(fn=cmd_register)
    q = s.add_parser("send"); q.add_argument("sender"); q.add_argument("to"); q.add_argument("body", nargs="?")
    q.add_argument("--kind", default="note"); q.add_argument("--thread"); q.add_argument("--subject"); q.add_argument("--file"); q.set_defaults(fn=cmd_send)
    q = s.add_parser("inbox"); q.add_argument("agent"); q.add_argument("--all", action="store_true"); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_inbox)
    q = s.add_parser("read"); q.add_argument("id", type=int); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_read)
    q = s.add_parser("reply"); q.add_argument("id", type=int); q.add_argument("body"); q.set_defaults(fn=cmd_reply)
    q = s.add_parser("done"); q.add_argument("id", type=int); q.set_defaults(fn=cmd_done)
    q = s.add_parser("status"); q.add_argument("agent"); q.add_argument("state"); q.add_argument("detail", nargs="?"); q.set_defaults(fn=cmd_status)
    q = s.add_parser("log"); q.add_argument("agent", nargs="?"); q.add_argument("--days", type=int, default=30); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_log)
    s.add_parser("sweep").set_defaults(fn=cmd_sweep)
    t = s.add_parser("task"); tt = t.add_subparsers(dest="tcmd", required=True)
    q = tt.add_parser("new"); q.add_argument("title"); q.add_argument("--assign"); q.set_defaults(fn=cmd_task_new)
    q = tt.add_parser("list"); q.add_argument("--state"); q.add_argument("--json", action="store_true"); q.set_defaults(fn=cmd_task_list)
    q = tt.add_parser("set"); q.add_argument("id", type=int); q.add_argument("state"); q.add_argument("--assign"); q.set_defaults(fn=cmd_task_set)
    q = s.add_parser("watch"); q.add_argument("name"); q.add_argument("--interval", type=float, default=5)
    q.add_argument("--once", action="store_true", help="投完当前 pending 即退出（适合 cron）"); q.set_defaults(fn=cmd_watch)
    return p

if __name__ == "__main__":
    args = build().parse_args()
    args.fn(args)
