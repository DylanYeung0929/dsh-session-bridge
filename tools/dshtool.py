#!/usr/bin/env python3
"""dshtool —— 从任意会话读取/检索 DeepSeek Harness 的全部对话。"""
import fcntl, json, os, signal, sys, glob, subprocess, time

try:  # behave like a normal CLI when piped into `head`
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

HOME = os.path.expanduser("~/.dsh")
CACHE = os.path.join(HOME, "storages/session_projcache/sessions")
SESS = os.path.join(HOME, "sessions")
RECENT = 300  # seconds

def lock_held(path):
    """True when a live DSH client holds this session's flock.

    DSH keeps an exclusive flock on `session.lock` for every session that is
    open in a client window —— the only offline "is anyone using this" signal.
    Probe with a *shared* lock so a running agent's exclusive lock is never
    disturbed; contention simply means "held".
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)

def sessions():
    out = []
    for f in glob.glob(os.path.join(CACHE, "*.json")):
        try:
            d = json.load(open(f))["record"]
        except Exception:
            continue
        sid = os.path.basename(f)[:-5]
        rows = d.get("rows", {})
        title = (rows.get("title") or {}).get("val") or "(未命名)"
        cwd = d.get("identity", {}).get("cwd", "")
        usage = ((rows.get("tokenUsage") or {}).get("val") or {}).get("totals", {})
        created = d.get("identity", {}).get("createdAt", 0)
        tr = transcript(sid)
        mtime = os.path.getmtime(tr) if tr else 0
        size = os.path.getsize(tr) if tr else 0
        held = bool(tr) and lock_held(os.path.join(os.path.dirname(tr), "session.lock"))
        out.append(dict(id=sid, title=title, cwd=cwd, usage=usage, created=created,
                        mtime=mtime, size=size, open=held,
                        active=held and (time.time() - mtime) < RECENT, transcript=tr))
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out

def transcript(sid):
    hits = glob.glob(os.path.join(SESS, "*", sid, "session.v4.jsonl.zstd"))
    return hits[0] if hits else None

class AmbiguousSession(Exception):
    """More than one session matches the needle."""

    def __init__(self, key, candidates):
        self.key = key
        self.candidates = candidates
        super().__init__("%d sessions match %r" % (len(candidates), key))

def resolve(key, ss=None):
    """Resolve by exact id -> unique id prefix -> exact title -> unique title.

    An ambiguous needle raises AmbiguousSession instead of silently picking the
    first hit: reading the wrong transcript is worse than failing.
    """
    ss = sessions() if ss is None else ss
    exact = [r for r in ss if r["id"] == key]
    if exact:
        return exact[0]
    by_id = [r for r in ss if key in r["id"]]
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        raise AmbiguousSession(key, by_id)
    low = key.lower()
    exact_title = [r for r in ss if (r["title"] or "").lower() == low]
    if len(exact_title) == 1:
        return exact_title[0]
    if len(exact_title) > 1:
        raise AmbiguousSession(key, exact_title)
    by_title = [r for r in ss if low in (r["title"] or "").lower()]
    if len(by_title) == 1:
        return by_title[0]
    if len(by_title) > 1:
        raise AmbiguousSession(key, by_title)
    return None

def iter_events(tr):
    with subprocess.Popen(["zstd", "-dc", tr], stdout=subprocess.PIPE, text=True) as p:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue

def msg_content(data):
    """Message payload differs per role: user/message keeps the text in
    `data.content`, assistant/message wraps it in `data.message.content`.
    Handle both so neither side silently disappears."""
    msg = data.get("message")
    if isinstance(msg, dict) and msg.get("content") is not None:
        return msg["content"]
    return data.get("content")

def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "image":
                    parts.append("[图片]")
    return "\n".join(p for p in parts if p)

def show(key, last=0, full=False, as_json=False):
    try:
        r = resolve(key)
    except AmbiguousSession as exc:
        print("会话名不唯一，请用完整 id 或更长的前缀：")
        for row in exc.candidates:
            print("  %-38s %s" % (row["id"], row["title"]))
        return 2
    if not r:
        print("找不到会话:", key); return 1
    if as_json:
        msgs = []
        for ev in iter_events(r["transcript"]):
            t = ev.get("type")
            if t in ("user/message", "assistant/message"):
                txt = text_of(msg_content(ev.get("data", {})))
                if txt.strip():
                    msgs.append({"role": "user" if t.startswith("user") else "assistant",
                                 "text": txt.strip()})
        tail = msgs[-last:] if last else msgs
        print(json.dumps({"sessionId": r["id"], "title": r["title"], "cwd": r["cwd"],
                          "messages": tail}, ensure_ascii=False, indent=2))
        return 0
    print("### %s  [%s]\n### cwd=%s\n" % (r["title"], r["id"], r["cwd"]))
    lines = []
    for ev in iter_events(r["transcript"]):
        t = ev.get("type"); data = ev.get("data", {})
        if t == "user/message":
            txt = text_of(msg_content(data))
            if txt.strip():
                lines.append(("USER", txt.strip()))
        elif t == "assistant/message":
            txt = text_of(msg_content(data))
            if txt.strip():
                lines.append(("ASSISTANT", txt.strip()))
        elif t == "tool/call" and full:
            lines.append(("TOOL", "%s(%s)" % (data.get("name"), str(data.get("arguments"))[:120])))
    tail = lines[-last:] if last else lines
    for role, txt in tail:
        if not full and len(txt) > 1200:
            txt = txt[:1200] + "…"
        print("【%s】%s\n" % (role, txt))
    return 0

def cmd_list(argv):
    ss = sessions()
    print("%-14s %-26s %-22s %8s %6s %s" % ("ID", "标题", "目录", "大小", "turn", "状态"))
    for r in ss:
        u = r["usage"]
        print("%-14s %-26s %-22s %7.1fM %6s %s" % (
            r["id"].replace("session-", "")[:12],
            (r["title"] or "")[:24],
            os.path.basename(r["cwd"])[:20],
            r["size"] / 1e6,
            ((r["usage"] or {}).get("last") or {}).get("turn", "-"),
            "● 活跃" if r["active"] else "◐ 打开" if r["open"] else time.strftime("%m-%d %H:%M", time.localtime(r["mtime"]))))
    return 0

def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print("""用法:
  dshtool list                       列出所有会话
  dshtool read <id前缀|标题> [N]     读最后 N 条消息（默认全部）
  dshtool read <key> --full          含工具调用
""")
        return 0
    if a[0] == "list":
        return cmd_list(a[1:])
    if a[0] == "read":
        if len(a) < 2:
            print("用法: dshtool read <id前缀|标题> [N] [--full] [--json]", file=sys.stderr); return 2
        key = a[1]
        last = 0; full = False; as_json = False
        for x in a[2:]:
            if x == "--full": full = True
            elif x == "--json": as_json = True
            else:
                try: last = int(x)
                except: pass
        return show(key, last, full, as_json)
    print("未知命令", a[0]); return 1

if __name__ == "__main__":
    sys.exit(main())
