#!/usr/bin/env python3
"""dshctl.py — control every DSH dialog from the command line.

Backends
--------
app (default)  Talk to the *running DeepSeek Harness desktop app* over its local
               HTTP RPC server — the same one the GUI uses. No extra process,
               every agent preset available, messages appear live in the GUI.
               Auth: the GUI's own `dsh-auth-*` cookie, read from Chromium's
               cookie store (~/Library/Application Support/@deepseek-ai/dsh-desktop/Cookies).
helper         Fallback: a throw-away `dsh --profile web` instance on a private
               port, used only when the desktop app is closed. It shares
               ~/.dsh/sessions but can only resume sessions whose agent preset
               exists in the web profile (e.g. `computer-use` sessions fail).

Wire protocol (from app.asar > dsh-client-connection / dsh-api-*):
    POST /api/<namespace>/<method>
    {"type":"client-request","rpcId":"<uuid>","method":"<namespace>/<method>",
     "payload":{"args":{"<parameter-name>":{...}}}}
  -> {"type":"server-response","rpcId":"...","result":{"ok":true,"value":...}}
     or {"result":{"ok":false,"error":{"code":...,"message":...}}}

Argument names differ per endpoint: `session/list` -> {"_request":{}},
`session/prompt` -> {"request":{...}}.

session/prompt body: {requestId, sessionId, content:[{type:"text",text}],
                      mode:"queue"|"steer", clientTimeZone?}

Commands
--------
  dshctl.py status                        backend + session overview
  dshctl.py list [--full]                 enumerate sessions
  dshctl.py prompt <session> <text> [--steer] [--wait SECS]
  dshctl.py chat <session>                interactive chat, with a menu after each turn
  dshctl.py focus <session> | focus -     remember / leave the session we are chatting with
  dshctl.py where                         show that remembered session
  dshctl.py tail <session> [N] [--full]   read the transcript from local files
  dshctl.py watch <session> [--lines N]   follow it live over the gateway stream
  dshctl.py doctor                        what this machine's discovery can see
  dshctl.py raw <endpoint> '<json args>'  call any endpoint directly
  dshctl.py start|stop [--port N] [--key KEY]   manage the helper fallback
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

try:  # behave like a normal CLI when piped into `head`
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

def _load_appenv():
    """Local sibling module: cross-platform paths, cookie store, CLI lookup."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import appenv
    return appenv


_ENV = _load_appenv()
APP_DIR = _ENV.app_data_dir()
COOKIE_DB = _ENV.cookie_db_path()
DSH = _ENV.dsh_cli_candidates()[0]
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".cache", "dshctl.json")
LOG = os.path.join(HOME, ".cache", "dshctl.log")
DEFAULT_PORT = 51499


# --------------------------------------------------------------------------- #
# backend: live desktop app
# --------------------------------------------------------------------------- #
DSH_HINT = b"dsh"


def _port_alive(port: int) -> bool:
    """True only when the port answers the way DSH itself does.

    A bare HTTPError proves nothing: proxies and other local servers answer
    whatever they like on `/` (e.g. 404 "Not found"). DSH's unauthenticated
    root answers 401 with a "dsh web authentication required" body.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as res:
            return res.status < 400
    except urllib.error.HTTPError as err:
        try:
            body = err.read(400).lower()
        except Exception:
            body = b""
        return err.code in (401, 403) and DSH_HINT in body
    except Exception:
        return False


def app_cookie() -> tuple[str, str, int]:
    """Newest GUI cookie whose authority points at a live local port."""
    for name, value, port in _ENV.read_auth_cookies(COOKIE_DB):
        if _port_alive(port):
            return name, value, port
    raise RuntimeError("no live dsh-auth cookie (is the app running?)")


def dsh_cli() -> str:
    """The bundled `dsh` binary (or a PATH one) for the helper backend."""
    for candidate in _ENV.dsh_cli_candidates():
        if os.path.exists(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit("dshctl: cannot find the dsh CLI; install the desktop app "
                     "or pass --port/--key to reuse a running helper")


def cmd_doctor(_args) -> None:
    """Print what this machine offers, and why discovery may fail."""
    rows = {
        "platform": _ENV.platform_id(),
        "app data": APP_DIR,
        "cookie db": COOKIE_DB,
        "cookie db exists": os.path.exists(COOKIE_DB),
        "dsh CLI": next((c for c in _ENV.dsh_cli_candidates()
                         if os.path.exists(c) or shutil.which(c)), None),
        "helper state": STATE if os.path.exists(STATE) else None,
    }
    cookies = _ENV.read_auth_cookies(COOKIE_DB)
    rows["auth cookies"] = [{"name": n, "port": p} for n, _v, p in cookies]
    candidates = [p for _n, _v, p in cookies] + _ENV.default_ports()
    rows["live ports"] = sorted({p for p in candidates if _port_alive(p)})
    print(json.dumps(rows, ensure_ascii=False, indent=2))


class AppClient:
    backend = "app"

    def __init__(self):
        self.name, self.value, self.port = app_cookie()
        self.base = f"http://127.0.0.1:{self.port}"

    def call(self, endpoint: str, args: dict, timeout: int = 120):
        body = json.dumps({"type": "client-request", "rpcId": str(uuid.uuid4()),
                           "method": endpoint, "payload": {"args": args}}).encode()
        req = urllib.request.Request(
            f"{self.base}/api/{endpoint}", data=body,
            headers={"content-type": "application/json",
                     "Cookie": f"{self.name}={self.value}"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            env = json.loads(res.read().decode())
        result = env.get("result", {})
        if not result.get("ok"):
            raise RuntimeError(json.dumps(result.get("error"), ensure_ascii=False))
        return result.get("value")


# --------------------------------------------------------------------------- #
# backend: throw-away helper instance
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(data, fh)
    os.chmod(STATE, 0o600)


def _read_token(port: int, since: int = 0) -> str | None:
    try:
        with open(LOG, errors="ignore") as fh:
            fh.seek(since)
            text = fh.read()
    except FileNotFoundError:
        return None
    hits = re.findall(rf"http://127\.0\.0\.1:{port}/\?token=([A-Za-z0-9_-]+)", text)
    return hits[-1] if hits else None


def helper_start(port: int = DEFAULT_PORT, key: str | None = None) -> dict:
    state = _load_state()
    if state.pop("key", None) is not None:
        _save_state(state)  # scrub keys persisted by <=0.1.0, they never leave memory now
    if state.get("token") and state.get("port") == port and _port_alive(port):
        return state
    if _port_alive(port):
        raise SystemExit(f"dshctl: :{port} already in use; pick another --port")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    since = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    env = dict(os.environ)
    key = key or env.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("dshctl: need DEEPSEEK_API_KEY (env or --key) for the helper; "
                         "it is passed through the child env and never written to disk")
    env["DEEPSEEK_API_KEY"] = key
    with open(LOG, "ab") as out:
        proc = subprocess.Popen([dsh_cli(), "--profile", "web", "--port", str(port), "--no-open"],
                                stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                env=env, cwd="/tmp")
    token = None
    for _ in range(60):
        time.sleep(0.5)
        token = _read_token(port, since)
        if token:
            break
        if proc.poll() is not None:
            raise SystemExit(f"dshctl: helper exited, see {LOG}")
    if not token:
        raise SystemExit(f"dshctl: no token in {LOG} within 30s")
    state = {"port": port, "token": token, "pid": proc.pid}
    _save_state(state)
    return state


def helper_stop() -> None:
    state = _load_state()
    pid = state.get("pid")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    try:
        os.remove(STATE)
    except FileNotFoundError:
        pass


class HelperClient:
    backend = "helper"

    def __init__(self, state: dict):
        self.port = state["port"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        try:
            self.opener.open(f"{self.base}/?token={state['token']}", timeout=10)
        except urllib.error.HTTPError:
            pass

    def call(self, endpoint: str, args: dict, timeout: int = 120):
        body = json.dumps({"type": "client-request", "rpcId": str(uuid.uuid4()),
                           "method": endpoint, "payload": {"args": args}}).encode()
        req = urllib.request.Request(f"{self.base}/api/{endpoint}", data=body,
                                     headers={"content-type": "application/json"})
        with self.opener.open(req, timeout=timeout) as res:
            env = json.loads(res.read().decode())
        result = env.get("result", {})
        if not result.get("ok"):
            raise RuntimeError(json.dumps(result.get("error"), ensure_ascii=False))
        return result.get("value")


def client() -> AppClient | HelperClient:
    try:
        return AppClient()
    except Exception as app_err:
        state = _load_state()
        if state.get("token") and _port_alive(state.get("port", 0)):
            return HelperClient(state)
        raise SystemExit(f"dshctl: no backend ({app_err}); start the app or run "
                         f"`dshctl.py start --key <KEY>`")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def sessions(c) -> dict[str, dict]:
    rows = c.call("session/list", {"_request": {}})["items"]
    out = {}
    for row in rows:
        title = ((row.get("projections") or {}).get("values") or {}).get("title") or ""
        out[row["sessionId"]] = {**row, "title": title}
    return out


def resolve(c, needle: str) -> str:
    """Exact id -> unique id substring -> exact title -> unique title substring.

    Never guesses: an ambiguous or unknown needle aborts instead of silently
    sending the message to the wrong conversation.
    """
    rows = sessions(c)
    if needle in rows:
        return needle

    def pick(cands: list[str], what: str) -> str | None:
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise SystemExit(f"dshctl: ambiguous {what}, candidates:\n" + "\n".join(
                f"  {sid}  {rows[sid]['title']}" for sid in cands))
        return None

    hit = pick([sid for sid in rows if needle in sid], "session id")
    if hit:
        return hit
    hit = pick([sid for sid, row in rows.items() if row["title"] == needle], "title")
    if hit:
        return hit
    hit = pick([sid for sid, row in rows.items() if needle in row["title"]], "title")
    if hit:
        return hit
    raise SystemExit(f"dshctl: no session matching {needle!r}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_status(_args) -> None:
    c = client()
    print(f"backend: {c.backend}  port: {getattr(c, 'port', '?')}")
    rows = sessions(c)
    print(f"sessions: {len(rows)}   running: {sum(1 for r in rows.values() if r.get('running'))}")
    for row in sorted(rows.values(), key=lambda r: -int(r.get("updatedAt") or 0))[:15]:
        print(f"  {'●' if row.get('running') else '○'} {row['sessionId'][:22]}  {row['title']}")


def _row_summary(row: dict) -> dict:
    usage = (((row.get("projections") or {}).get("values") or {}).get("tokenUsage") or {})
    return {"sessionId": row["sessionId"], "title": row.get("title") or "",
            "cwd": row.get("cwd"), "running": bool(row.get("running")),
            "updatedAt": row.get("updatedAt"),
            "outputTokens": usage.get("outputTokens")}


def cmd_list(args) -> None:
    c = client()
    rows = sessions(c)
    if getattr(args, "json", False):
        ordered = sorted(rows.values(), key=lambda r: -int(r.get("updatedAt") or 0))
        print(json.dumps(ordered if args.full else [_row_summary(r) for r in ordered],
                         ensure_ascii=False, indent=2))
        return
    for row in sorted(rows.values(), key=lambda r: -int(r.get("updatedAt") or 0)):
        line = f"{'●' if row.get('running') else '○'} {row['sessionId']}  {row['title']}"
        if args.full:
            usage = (((row.get("projections") or {}).get("values") or {})
                     .get("tokenUsage") or {})
            line += f"\n    cwd={row.get('cwd')}  agent={row.get('agentAvailable')}  out={usage.get('outputTokens')}"
        print(line)


def cmd_prompt(args) -> None:
    c = client()
    sid = resolve(c, args.session)
    request = {
        "sessionId": sid,
        "content": [{"type": "text", "text": args.text}],
        "mode": "steer" if args.steer else "queue",
        "requestId": str(uuid.uuid4()),
        "clientTimeZone": "Asia/Shanghai",
    }
    value = c.call("session/prompt", {"request": request}, timeout=120)
    print(f"sent -> {sid}  {json.dumps(value)}")
    if args.wait:
        deadline = time.time() + args.wait
        time.sleep(2)
        while time.time() < deadline:
            if not sessions(c).get(sid, {}).get("running"):
                print("turn finished")
                break
            time.sleep(2)
        else:
            print("still running")
        print(f"read it with: dshctl.py tail {sid}")



def _load_dshtool():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import dshtool  # local sibling module
    return dshtool


def last_messages(sid: str, n: int = 1) -> list[tuple[str, str]]:
    """Last `n` user/assistant messages from the local transcript."""
    try:
        dt = _load_dshtool()
        row = dt.resolve(sid)
        if not row or not row.get("transcript"):
            return []
    except Exception:
        return []
    msgs: list[tuple[str, str]] = []
    for ev in dt.iter_events(row["transcript"]):
        kind = ev.get("type")
        if kind not in ("user/message", "assistant/message"):
            continue
        text = dt.text_of(dt.msg_content(ev.get("data", {}))).strip()
        if text:
            msgs.append(("你" if kind == "user/message" else "它", text))
    return msgs[-n:]


def _wait_turn(c, sid: str, timeout: int = 180) -> bool:
    start = time.time()
    time.sleep(1.5)
    while time.time() - start < timeout:
        if not sessions(c).get(sid, {}).get("running"):
            return True
        time.sleep(2)
    return False


MENU = "1继续 2切换 3上下文 4退出"


def cmd_chat(args) -> None:
    """Interactive cross-session chat with a tiny after-each-turn menu."""
    c = client()
    sid = resolve(c, args.session)
    rows = sessions(c)

    def label(s): return rows.get(s, {}).get("title") or s

    def show_menu():
        print(f"\n{MENU}  ← 直接打字=继续")

    def switch():
        nonlocal sid, rows
        rows = sessions(c)
        ordered = sorted(rows.items(), key=lambda kv: -int(kv[1].get("updatedAt") or 0))[:15]
        for i, (s, row) in enumerate(ordered, 1):
            mark = "●" if row.get("running") else "○"
            print(f"  {i:>2}. {mark} {row['title'] or '(无标题)'}  [{s[:16]}…]")
        raw = input("切到第几个（0=取消）> ").strip()
        if not raw.isdigit() or raw == "0":
            print("已取消。")
            return
        idx = int(raw) - 1
        if not 0 <= idx < len(ordered):
            print("编号超范围。")
            return
        sid = ordered[idx][0]
        print(f"✅ 已切换到：{label(sid)}")

    print(f"💬 会话：{label(sid)}\n（/help 看命令；回车=继续；4=退出）")
    while True:
        try:
            line = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break

        if line in ("4", "q", "quit", "exit", "退出", "/quit", "/exit"):
            print("已退出。")
            break
        if line in ("1", "继续", "/continue"):
            line = input("要说什么（直接回车=发「继续」）> ").strip() or "继续"
        elif line == "":
            line = "继续"  # bare Enter means "continue", as documented
        elif line in ("2", "切换", "/switch"):
            switch()
            show_menu()
            continue
        elif line in ("3", "上下文", "/context"):
            n = args.context
            for role, text in last_messages(sid, n):
                print(f"\n【{role}】{text[:600]}")
            show_menu()
            continue
        elif line in ("/help", "help", "?", "帮助"):
            print("直接输入=发消息；1继续 2切换 3上下文 4退出")
            continue

        request = {
            "sessionId": sid,
            "content": [{"type": "text", "text": line}],
            "mode": "steer" if args.steer else "queue",
            "requestId": str(uuid.uuid4()),
            "clientTimeZone": "Asia/Shanghai",
        }
        try:
            c.call("session/prompt", {"request": request}, timeout=120)
        except Exception as exc:
            print(f"❌ 发送失败：{exc}")
            show_menu()
            continue
        print("⏳ 等它回完这一轮…", end="", flush=True)
        ok = _wait_turn(c, sid, args.timeout)
        print(" 完成" if ok else " 仍在跑")
        msgs = last_messages(sid, 4)
        reply = next((t for role, t in reversed(msgs) if role == "它"), None)
        print(f"\n它 > {reply[:1500] if reply else '(这一轮还没产出文本，可用 3 看上下文)'}")
        show_menu()



CHAT_STATE = os.path.join(HOME, ".cache", "dshctl-chat.json")


def focus_read() -> dict:
    try:
        with open(CHAT_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def focus_write(data: dict) -> None:
    os.makedirs(os.path.dirname(CHAT_STATE), exist_ok=True)
    with open(CHAT_STATE, "w") as fh:
        json.dump(data, fh)
    os.chmod(CHAT_STATE, 0o600)


def cmd_focus(args) -> None:
    """Remember which session the current conversation is with (for IM use)."""
    if args.session in ("-", "none", "clear"):
        focus_write({})
        print("已退出跨会话对话。")
        return
    c = client()
    row = sessions(c)
    sid = resolve(c, args.session)
    try:
        _row = row.get(sid, {})
    except TypeError:  # very old rows without session ids map cleanly anyway
        _row = {}
    title = _row.get("title") or sid
    focus_write({"sessionId": sid, "title": title, "at": int(time.time())})
    print(f"已进入：{title}\n（后续消息会发给它；`dshctl.py focus -` 退出）")


def cmd_where(_args) -> None:
    f = focus_read()
    print(json.dumps(f, ensure_ascii=False) if f else "当前没有进行中的跨会话对话。")


def cmd_tail(args) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "dshtool.py"), "read", args.session, str(args.n)]
    if getattr(args, "json", False):
        cmd.append("--json")
    if getattr(args, "full", False):
        cmd.append("--full")
    # propagate the reader's exit code so callers can tell "ambiguous" from "empty"
    return subprocess.run(cmd, check=False).returncode


ROLE = {"user/message": "你", "assistant/message": "它"}


def _cookie_header(c) -> str:
    """Cookie header value that authenticates the WebSocket upgrade."""
    if getattr(c, "backend", "") == "app":
        return f"{c.name}={c.value}"
    return "; ".join(f"{ck.name}={ck.value}" for ck in c.jar)


def _recent(sid: str, n: int) -> list[tuple[str, str]]:
    """Last n user/assistant messages from the local transcript (offline)."""
    if n <= 0:
        return []
    dt = _load_dshtool()
    tr = dt.transcript(sid)
    if not tr:
        return []
    msgs = []
    for ev in dt.iter_events(tr):
        if ev.get("type") in ROLE:
            text = dt.text_of(dt.msg_content(ev.get("data") or "")).strip()
            if text:
                msgs.append((ev["type"], text))
    return msgs[-n:]


def _on_stream(frame: dict, args, state: dict) -> None:
    """Render `assistant-stream` frames: partial text while the turn runs."""
    kind = frame.get("type")
    if kind == "start":
        if not state["prefix"]:
            sys.stdout.write("\n它 > ")
            sys.stdout.flush()
            state["prefix"] = True
        return
    if kind == "chunk":
        chunk = frame.get("chunk") or {}
        ctype = chunk.get("type")
        if ctype == "text-delta":
            text = chunk.get("text") or ""
            if text:
                if not state["prefix"]:
                    sys.stdout.write("\n它 > ")
                state["prefix"] = True
                state["streamed"] = True
                sys.stdout.write(text)
                sys.stdout.flush()
        elif ctype == "reasoning-delta" and getattr(args, "reasoning", False):
            text = chunk.get("text") or ""
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
        return
    if kind == "end" and (state["prefix"] or state["streamed"]):
        sys.stdout.write("\n")
        sys.stdout.flush()
        state["prefix"] = False
        state["streamed"] = False


def cmd_watch(args) -> int:
    """Follow one session live over the gateway `session/follow` stream.

    The gateway multiplexes every stream over one WebSocket at
    `/api/remote.mux`; the cookie that authenticates HTTP also authenticates
    the upgrade. Frames are `{type:open|item|end|cancel|error}`.
    """
    c = client()
    rows = sessions(c)
    sid = resolve(c, args.session)
    row = rows.get(sid, {})
    title = row.get("title") or sid

    # Subagent sessions are addressable only through their durable parent.
    if row.get("origin") == "subagent" and row.get("parentSessionId"):
        mode = (((row.get("projections") or {}).get("values") or {})
                .get("subagent") or {}).get("mode") or "unknown"
        address = {"kind": "subagent", "parentSessionId": row["parentSessionId"],
                   "childSessionId": sid, "mode": mode}
    else:
        address = {"kind": "session", "sessionId": sid}
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import ws  # local sibling module: stdlib WebSocket client

    try:
        sock = ws.connect(f"ws://127.0.0.1:{c.port}/api/remote.mux",
                          headers={"Cookie": _cookie_header(c)}, timeout=10)
    except Exception as exc:
        raise SystemExit(f"dshctl: cannot follow {title!r}: {exc}")

    # session/follow takes one `request` object: the opening snapshot is capped
    # to one message (we print the local tail ourselves) and events must carry
    # seq greater than the snapshot cursor.
    request = {"address": address, "maxMessages": 1}
    if not args.no_stream:
        request["assistantStream"] = True
    ask = {"request": request}
    stream_id = uuid.uuid4().hex
    sock.send_text(json.dumps({"type": "open", "streamId": stream_id,
                               "endpoint": "session/follow",
                               "payload": {"args": ask}}))

    if not args.quiet:
        print(f"👀 跟随 {title}（Ctrl-C 退出）", flush=True)
        for role, text in _recent(sid, args.lines):
            print(f"\n{ROLE[role]} > {text}", flush=True)

    state = {"streamed": False, "prefix": False, "cursor": 0}
    try:
        while True:
            try:
                raw = sock.recv_text()
            except ws.ConnectionClosed:
                if not args.quiet:
                    print("\n(连接已断开)", flush=True)
                return 0
            if args.json:
                print(raw, flush=True)
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if frame.get("type") == "error":
                err = frame.get("error") or {}
                raise SystemExit(f"dshctl: follow failed: {err.get('code')} {err.get('message')}")
            if frame.get("type") != "item":
                continue
            value = frame.get("value") or {}
            kind = value.get("type")
            if kind == "snapshot":
                state["cursor"] = int(value.get("cursor") or 0)
                continue
            if kind == "assistant-stream":
                _on_stream(value.get("frame") or {}, args, state)
            elif kind == "event":
                ev = value.get("event") or {}
                role = ROLE.get(ev.get("type"))
                if not role or int(ev.get("seq") or 0) <= state.get("cursor", 0):
                    continue
                if state["streamed"]:  # already printed by the live stream
                    state["streamed"] = False
                    state["prefix"] = False
                    continue
                data = ev.get("data") or {}
                text = _load_dshtool().text_of(_load_dshtool().msg_content(data))
                print(f"\n{role} > {text}", flush=True)
    except KeyboardInterrupt:
        if not args.quiet:
            print("\n👋 已停止跟随", flush=True)
        return 0
    finally:
        try:
            sock.send_text(json.dumps({"type": "cancel", "streamId": stream_id}))
        except Exception:
            pass
        sock.close()


def cmd_raw(args) -> None:
    print(json.dumps(client().call(args.endpoint, json.loads(args.args)),
                     ensure_ascii=False, indent=2))


def cmd_start(args) -> None:
    state = helper_start(args.port, args.key)
    print(f"helper on :{state['port']} pid={state['pid']}")


def cmd_stop(_args) -> None:
    helper_stop()
    print("helper stopped")


def main() -> None:
    ap = argparse.ArgumentParser(description="control DSH sessions over the local RPC")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--key")
    p.set_defaults(func=cmd_start)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="diagnose app/cookie/CLI discovery").set_defaults(func=cmd_doctor)

    p = sub.add_parser("list")
    p.add_argument("--full", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("prompt")
    p.add_argument("session", help="session-<uuid> or a title substring")
    p.add_argument("text")
    p.add_argument("--steer", action="store_true", help="inject into the running turn")
    p.add_argument("--wait", type=int, default=0, metavar="SECS")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("chat")
    p.add_argument("session", help="session-<uuid> or a title substring")
    p.add_argument("--steer", action="store_true")
    p.add_argument("--timeout", type=int, default=180, help="max seconds to wait per turn")
    p.add_argument("--context", type=int, default=6, help="how many messages option 3 shows")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("focus", help="remember the session you are chatting with")
    p.add_argument("session", help="title / session-<uuid> / '-' to clear")
    p.set_defaults(func=cmd_focus)

    sub.add_parser("where").set_defaults(func=cmd_where)

    p = sub.add_parser("tail")
    p.add_argument("session")
    p.add_argument("n", nargs="?", type=int, default=12)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--full", action="store_true", help="include tool calls")
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("watch", help="follow a session live (new messages appear as they happen)")
    p.add_argument("session", help="session-<uuid> or a title substring")
    p.add_argument("--lines", type=int, default=5, metavar="N",
                   help="show the last N messages before following (0 = none)")
    p.add_argument("--no-stream", action="store_true",
                   help="do not follow partial assistant text")
    p.add_argument("--reasoning", action="store_true", help="also print reasoning deltas")
    p.add_argument("--json", action="store_true", help="print raw stream frames")
    p.add_argument("--quiet", action="store_true", help="no banner, no history")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("raw")
    p.add_argument("endpoint")
    p.add_argument("args", nargs="?", default="{}")
    p.set_defaults(func=cmd_raw)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
