#!/usr/bin/env python3
"""Telegram bridge for Claude Code — single-file, std-lib only.

One script handles everything: outbound (send/file/photo), inbound routing
(pump/claim under an flock so multiple sessions share Telegram's single
getUpdates consumer), the always-on background listener, and the Claude Code
hooks (Stop / UserPromptSubmit / Notification / SessionStart) loaded from
hooks/hooks.json.

State lives in a stable dir (default ~/.claude/telegram, override with
$TG_STATE_DIR) — never inside the plugin, so reinstalls/updates don't touch your
token or message offset.

Usage:
  tg.py setup                     detect + save chat_id and user_id
  tg.py send "text" | send -      send a message (stdin with '-'); auto-splits >4096
  tg.py file <path> [caption]     send a document
  tg.py photo <path> [caption]    send an image
  tg.py recv [timeout]            one receive cycle (lock→pump→claim); exit 3 if none
  tg.py listen [maxsecs]          block (cheap) until a message for this session; print+exit
  tg.py ask "text" [budget]       send, then wait inline for the reply (loops recv)
  tg.py drain                     reset offset + clear inbox
  tg.py away on|off|active|clear|list [dir]
  tg.py hook stop|userprompt|notification|sessionstart|sessionend   (reads hook JSON on stdin)

Env: TG_KEY / TG_SESSION_ID / TG_SESSION_LEASE bind managed workers to one
session generation; TG_CWD / TG_LABEL set the outbound label.
Exit codes: 0 ok (message printed), 2 config missing/invalid, 3 timeout/no-message
(relaunch once), 4 a listener for this session is already running (do nothing),
5 the owning session ended (do not relaunch), 1 other.
"""
import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

STATE_DIR = os.environ.get("TG_STATE_DIR") or os.path.expanduser("~/.claude/telegram")
CONFIG = os.path.join(STATE_DIR, "config.json")
STATE = os.path.join(STATE_DIR, "state")
SENT = os.path.join(STATE_DIR, "sent.map")
INBOX = os.path.join(STATE_DIR, "inbox.jsonl")
LOCKF = os.path.join(STATE_DIR, "lock")
AWAYD = os.path.join(STATE_DIR, "away.d")
IDLED = os.path.join(STATE_DIR, "idle.d")
SESSIOND = os.path.join(STATE_DIR, "sessions.d")
INBOX_TTL = 3600     # drop unclaimed messages after 1h
SENT_MAX = 500
SESSION_STATE_VERSION = 1
SELF = os.path.abspath(__file__)


def die(msg, code=1):
    sys.stderr.write("tg: %s\n" % msg)
    sys.exit(code)


def ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def load_cfg():
    if not os.path.exists(CONFIG):
        die("no config.json in %s — run setup" % STATE_DIR, 2)
    with open(CONFIG) as f:
        c = json.load(f)
    tok = c.get("token")
    if not tok or tok == "PUT_YOUR_BOT_TOKEN_HERE":
        die("token not set in %s" % CONFIG, 2)
    return c


def save_cfg(c):
    ensure_dir()
    with open(CONFIG, "w") as f:
        json.dump(c, f, indent=2)
    os.chmod(CONFIG, 0o600)


def api(method, params=None, timeout=30, files=None):
    tok = load_cfg()["token"]
    url = "https://api.telegram.org/bot%s/%s" % (tok, method)
    if files:  # multipart/form-data
        boundary = "----tgbridge%d" % int(time.time() * 1000)
        body = bytearray()
        for k, v in (params or {}).items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, k, v)).encode()
        for field, path in files.items():
            fn = os.path.basename(path)
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                     "Content-Type: application/octet-stream\r\n\r\n" % (boundary, field, fn)).encode()
            with open(path, "rb") as fh:
                body += fh.read()
            body += b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        req = urllib.request.Request(url, data=bytes(body),
                                     headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    else:
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------- small state helpers ----------
def get_offset():
    try:
        with open(STATE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def set_offset(o):
    ensure_dir()
    with open(STATE, "w") as f:
        f.write(str(o))
        f.flush()
        os.fsync(f.fileno())


def session_key():
    raw = os.environ.get("TG_KEY") or os.environ.get("TG_CWD") or os.getcwd()
    return "".join(ch if ch.isalnum() else "_" for ch in raw)


def label_prefix():
    """Session name as a bold header on its own line above the message body."""
    lab = os.environ.get("TG_LABEL", "")
    if not lab and os.environ.get("TG_CWD"):
        lab = os.path.basename(os.environ["TG_CWD"])
    lab = lab.strip("[] ")  # tolerate a previously-bracketed TG_LABEL
    return ("*%s*\n" % lab) if lab else ""


def marker_path(d):
    raw = d or os.getcwd()
    return os.path.join(AWAYD, "".join(ch if ch.isalnum() else "_" for ch in raw))


def clean_session_id(raw):
    return "".join(ch if ch.isalnum() else "_" for ch in (raw or ""))


def session_state_path(sid):
    return os.path.join(SESSIOND, clean_session_id(sid) + ".json")


def read_session_state(sid):
    if not sid:
        return {}
    try:
        with open(session_state_path(sid)) as f:
            state = json.load(f)
        if state.get("schema_version") != SESSION_STATE_VERSION:
            return {}
        if state.get("session_id") != clean_session_id(sid):
            return {}
        return state
    except Exception:
        return {}


def write_session_state(state):
    os.makedirs(SESSIOND, exist_ok=True)
    path = session_state_path(state["session_id"])
    tmp = path + ".tmp-" + str(os.getpid())
    with open(tmp, "w") as f:
        json.dump(state, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def start_session_lease(sid):
    sid = clean_session_id(sid)
    if not sid:
        return ""
    lease_id = "%d-%d" % (time.time_ns(), os.getpid())
    write_session_state({
        "schema_version": SESSION_STATE_VERSION,
        "session_id": sid,
        "lease_id": lease_id,
        "status": "active",
        "started_at": int(time.time()),
    })
    return lease_id


def end_session_lease(sid):
    sid = clean_session_id(sid)
    state = read_session_state(sid)
    if not state:
        return
    state["status"] = "ended"
    state["ended_at"] = int(time.time())
    write_session_state(state)


def active_session_lease_id(sid):
    state = read_session_state(sid)
    if state.get("status") != "active":
        return ""
    return state.get("lease_id", "")


def session_lease_active(sid, lease_id):
    # Manual CLI usage has no session lease and keeps its existing timeout-based
    # behavior. SessionStart-managed workers always provide both fields.
    if not sid or not lease_id:
        return True
    return active_session_lease_id(sid) == lease_id


def shell_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def command_env_prefix(cwd="", sid="", lease_id=""):
    values = (("TG_CWD", cwd), ("TG_KEY", sid), ("TG_SESSION_ID", sid),
              ("TG_SESSION_LEASE", lease_id))
    return " ".join("%s=%s" % (key, shell_quote(value))
                    for key, value in values if value)


def reply_target_path(key):
    return os.path.join(STATE_DIR, "reply." + key)


def set_reply_target(key, mid):
    """Remember the message_id of the user's latest message to this session so the
    next outbound send() threads onto it (Telegram reply). Persists until a newer
    inbound message overwrites it."""
    if not mid:
        return
    ensure_dir()
    try:
        with open(reply_target_path(key), "w") as f:
            f.write(str(mid))
    except OSError:
        pass


def get_reply_target(key):
    try:
        with open(reply_target_path(key)) as f:
            return int(f.read().strip())
    except Exception:
        return None


class Lock:
    def __enter__(self):
        ensure_dir()
        self.f = open(LOCKF, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ---------- outbound ----------
def chat_id():
    return load_cfg().get("chat_id")


def _send_chunk(text, reply_to=None):
    base = {"chat_id": chat_id(), "text": text}
    if reply_to:
        base["reply_to_message_id"] = reply_to
        base["allow_sending_without_reply"] = True  # don't error if it was deleted
    try:
        r = api("sendMessage", dict(base, parse_mode="Markdown"))
    except Exception:
        r = api("sendMessage", base)
    return (r.get("result") or {}).get("message_id")


def _append_sent(mids, key):
    """Append message_id -> session_key rows to sent.map. Assumes the lock is held.
    Recording EVERY outbound id (replies, nudges, notifications) keeps the map
    hole-free, so any reply to one of our messages is always attributable."""
    mids = [str(m) for m in mids if m]
    if not mids:
        return
    if os.path.exists(SENT):
        with open(SENT) as f:
            lines = f.read().splitlines()
    else:
        lines = []
    lines += ["%s\t%s" % (m, key) for m in mids]
    with open(SENT, "w") as f:
        f.write("\n".join(lines[-SENT_MAX:]) + "\n")
        f.flush()
        os.fsync(f.fileno())


def record_mids(mids):
    with Lock():
        _append_sent(mids, session_key())


def cmd_send(arg, thread=True):
    if chat_id() is None:
        die("chat_id not set — run: tg.py setup", 2)
    text = sys.stdin.read() if arg == "-" else (arg or "")
    if not text:
        die('usage: tg.py send "text"   (or: ... | tg.py send -)')
    text = label_prefix() + text
    # Thread onto the user's last message to us — but only if asked. A standalone
    # message (e.g. the SessionStart announcement) must NOT reply, since there may
    # be no prior message, or only a stale id from an earlier session.
    rt = get_reply_target(session_key()) if thread else None
    mids = []
    for i in range(0, len(text), 4000):
        mid = _send_chunk(text[i:i + 4000], reply_to=rt)
        rt = None  # only the first chunk threads; the rest are plain continuations
        if mid:
            mids.append(mid)
    record_mids(mids)
    print("sent")


def cmd_media(method, field, path, caption):
    if not path or not os.path.isfile(path):
        die("no such file: %s" % path)
    if chat_id() is None:
        die("chat_id not set — run: tg.py setup", 2)
    r = api(method, {"chat_id": chat_id(), "caption": label_prefix() + (caption or "")},
            files={field: path})
    record_mids([(r.get("result") or {}).get("message_id")])
    print("sent")


# ---------- inbound routing ----------
def load_sentmap():
    m = {}
    if os.path.exists(SENT):
        with open(SENT) as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) == 2:
                    m[p[0]] = p[1]
    return m


def read_inbox():
    items = []
    if os.path.exists(INBOX):
        with open(INBOX) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass
    return items


def write_inbox(items):
    ensure_dir()
    tmp = INBOX + ".tmp"
    with open(tmp, "w") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, INBOX)


def _pump(timeout):
    """Assumes lock held. Fetch updates, route to inbox, then advance offset."""
    c = load_cfg()
    chat, uid = c.get("chat_id"), c.get("user_id")
    offset = get_offset()
    try:
        resp = api("getUpdates", {"offset": offset, "timeout": timeout}, timeout=timeout + 10)
    except Exception:
        return
    results = resp.get("result", [])
    sm = load_sentmap()
    items = read_inbox()
    seen = {it.get("uid") for it in items}
    now = int(time.time())
    last = offset - 1
    for u in results:
        uid_ = u["update_id"]
        last = max(last, uid_)
        if uid_ in seen:
            continue
        msg = u.get("message")
        if not msg:
            continue
        if chat is not None and msg.get("chat", {}).get("id") != chat:
            continue
        if uid is not None and msg.get("from", {}).get("id") != uid:
            continue
        text = msg.get("text")
        if not text:
            continue
        # Reply-id is the ONLY routing signal. A message is delivered iff it is a
        # reply to one of our sent messages that we can attribute to a session (via
        # sent.map). No reply, or a reply we can't attribute -> drop it (with a
        # one-line nudge) rather than guess which session it belongs to.
        rt = msg.get("reply_to_message")
        owner = sm.get(str(rt.get("message_id"))) if rt else None
        if not owner:
            try:
                nudge_mid = _send_chunk(
                    "🤔 Не понял, какой сессии это адресовано. "
                    "Ответь реплаем на сообщение нужной сессии.",
                    reply_to=msg.get("message_id"))
                # Record the nudge under a non-session sentinel so sent.map stays
                # hole-free, yet a reply to the nudge itself resolves to no session
                # and is dropped again (rather than silently misrouted).
                if nudge_mid:
                    _append_sent([nudge_mid], "__nudge__")
            except Exception:
                pass
            seen.add(uid_)
            continue  # dropped: never enters the inbox
        items.append({"to": owner, "text": text, "ts": now, "uid": uid_,
                      "mid": msg.get("message_id")})
        seen.add(uid_)
    # A message is only ever delivered to the session it is addressed to. Unclaimed
    # messages expire at INBOX_TTL; they are never reassigned to a different session.
    kept = [it for it in items if now - it.get("ts", now) <= INBOX_TTL]
    write_inbox(kept)          # durable inbox FIRST ...
    set_offset(last + 1)       # ... then advance the Telegram offset


def _claim(key):
    items = read_inbox()
    mine = [it for it in items if it.get("to") == key]
    rest = [it for it in items if it.get("to") != key]
    if not mine:
        return None
    write_inbox(rest)
    in_mids = [it.get("mid") for it in mine if it.get("mid")]
    if in_mids:
        set_reply_target(key, max(in_mids))  # next send() threads onto the latest
    return "\n".join(it["text"] for it in mine)


def cmd_recv(timeout):
    if chat_id() is None:
        die("chat_id not set — run: tg.py setup", 2)
    with Lock():
        _pump(timeout)
        out = _claim(session_key())
    if out is None:
        sys.exit(3)
    print(out)


def cmd_listen(maxsecs):
    sid = clean_session_id(os.environ.get("TG_SESSION_ID", ""))
    lease_id = os.environ.get("TG_SESSION_LEASE", "")
    if not session_lease_active(sid, lease_id):
        sys.exit(5)
    if chat_id() is None:
        die("chat_id not set — run: tg.py setup", 2)
    key = session_key()
    # Singleton per session: hold an exclusive non-blocking lock for this session
    # key. If another listener for the same session is already running, exit at once
    # so listeners can't pile up (the lock auto-releases when this process exits).
    ensure_dir()
    generation = ("." + clean_session_id(lease_id)) if lease_id else ""
    singleton = open(os.path.join(STATE_DIR, "listen." + key + generation + ".lock"), "w")
    try:
        fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # A listener for this session is already running. Exit 4 (NOT 3) so the
        # caller can tell "already listening — do nothing" apart from "timed out
        # with no message — relaunch once".
        singleton.close()
        sys.exit(4)
    try:
        start = time.time()
        while time.time() - start < maxsecs:
            if not session_lease_active(sid, lease_id):
                sys.exit(5)
            # Hold the shared lock only for an instant: _pump(0) returns immediately
            # instead of long-polling for 5s under the lock, so many sessions don't
            # serialize behind a slow poll. Pacing comes from the sleep below.
            with Lock():
                if not session_lease_active(sid, lease_id):
                    sys.exit(5)
                _pump(0)
                if not session_lease_active(sid, lease_id):
                    sys.exit(5)
                out = _claim(key)
            if out is not None:
                # Wrap the message so the agent can't miss that a Telegram reply is
                # REQUIRED before doing anything else — enforces "came from Telegram ->
                # answer in Telegram first" at the point the message is delivered.
                tg = os.path.join(STATE_DIR, "tg")
                cwd = os.environ.get("TG_CWD", "")
                prefix = command_env_prefix(cwd, sid, lease_id)
                send = ((prefix + " ") if prefix else "") + "%s send '...'" % tg
                print("=== TELEGRAM MESSAGE — reply REQUIRED before acting ===\n"
                      + out +
                      "\n=== END. Your FIRST action MUST be to acknowledge in Telegram:\n"
                      "    %s\n"
                      "Only AFTER sending that, act on the message, then relaunch the "
                      "listener. ===" % send)
                return
            time.sleep(1 + (int(time.time()) % 3))
        sys.exit(3)
    finally:
        fcntl.flock(singleton, fcntl.LOCK_UN)
        singleton.close()


def cmd_ask(text, budget):
    if not text:
        die('usage: tg.py ask "text"')
    cmd_send(text)
    start = time.time()
    while time.time() - start < budget:
        with Lock():
            _pump(5)
            out = _claim(session_key())
        if out is not None:
            print(out)
            return
        time.sleep(3)
    sys.exit(3)


def cmd_drain():
    with Lock():
        _pump(0)
        write_inbox([])
    print("drained")


# ---------- setup / away ----------
def cmd_setup():
    resp = api("getUpdates")
    msgs = [u["message"] for u in resp.get("result", []) if u.get("message")]
    if not msgs:
        die("no messages found — send your bot a message first, then rerun setup")
    cid = msgs[-1]["chat"]["id"]
    uid = msgs[-1].get("from", {}).get("id")
    c = load_cfg()
    c["chat_id"] = cid
    if uid:
        c["user_id"] = uid
    save_cfg(c)
    last = max((u["update_id"] for u in resp.get("result", [])), default=0)
    if last:
        set_offset(last + 1)
    print("chat_id set to %s, user_id %s" % (cid, uid))


def cmd_away(action, d):
    me = d or os.getcwd()
    mp = marker_path(me)
    if action == "on":
        os.makedirs(AWAYD, exist_ok=True)
        with open(mp, "w") as f:
            f.write(me)
        print("away on: %s" % me)
    elif action in ("off", "clear"):
        try:
            os.remove(mp)
        except OSError:
            pass
        print("away off: %s" % me)
    elif action == "active":
        sys.exit(0 if os.path.exists(mp) else 1)
    elif action == "list":
        if os.path.isdir(AWAYD):
            for fn in os.listdir(AWAYD):
                with open(os.path.join(AWAYD, fn)) as f:
                    print(f.read())
    else:
        die("usage: tg.py away {on|off|active|clear|list} [dir]")


def away_active(d):
    return os.path.exists(marker_path(d))


# ---------- hooks ----------
def _read_hook_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def hook_stop(inp):
    cwd = inp.get("cwd", "")
    last = inp.get("last_assistant_message", "") or ""
    sid = clean_session_id(inp.get("session_id", ""))
    # In away mode the listener handles replies; otherwise arm the idle auto-mirror.
    if away_active(cwd):
        return
    try:
        secs = int(load_cfg().get("idle_mirror_secs", 600))
    except Exception:
        secs = 600
    lease_id = active_session_lease_id(sid)
    if secs > 0 and sid and lease_id:
        os.makedirs(IDLED, exist_ok=True)
        with open(os.path.join(IDLED, "msg-" + sid), "w") as f:
            f.write(last)
        env = dict(os.environ, TG_CWD=cwd, TG_KEY=sid, TG_SESSION_ID=sid,
                   TG_SESSION_LEASE=lease_id)
        subprocess.Popen([sys.executable, SELF, "_idlewatch", sid, str(int(time.time())), cwd,
                          lease_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def hook_userprompt(inp):
    cwd = inp.get("cwd", "")
    sid = clean_session_id(inp.get("session_id", ""))
    if sid:
        os.makedirs(IDLED, exist_ok=True)
        with open(os.path.join(IDLED, "prompt-" + sid), "w") as f:
            f.write(str(int(time.time())))
    if cwd:
        try:
            os.remove(marker_path(cwd))
        except OSError:
            pass


def notification_message(inp):
    """Build a human notification line from Claude's Notification hook payload."""
    return inp.get("message") or "Агент ждёт твоего ввода."


def hook_notification(inp):
    cwd = inp.get("cwd", "")
    if not away_active(cwd):
        return
    msg = notification_message(inp)
    os.environ["TG_CWD"] = cwd
    sid = clean_session_id(inp.get("session_id", ""))
    if sid:
        os.environ["TG_KEY"] = sid
        os.environ["TG_SESSION_ID"] = sid
        lease_id = active_session_lease_id(sid)
        if not lease_id:
            return
        os.environ["TG_SESSION_LEASE"] = lease_id
    try:
        cmd_send("🔔 " + msg)
    except SystemExit:
        pass
    except Exception:
        pass


def ensure_launcher():
    """Write a stable wrapper at STATE_DIR/tg so the script is reachable by a fixed
    path regardless of where the plugin is installed."""
    ensure_dir()
    launcher = os.path.join(STATE_DIR, "tg")
    body = '#!/usr/bin/env bash\nexec python3 "%s" "$@"\n' % SELF
    try:
        cur = ""
        if os.path.exists(launcher):
            with open(launcher) as f:
                cur = f.read()
        if cur != body:
            with open(launcher, "w") as f:
                f.write(body)
            os.chmod(launcher, 0o755)
    except OSError:
        pass
    return launcher


def always_listen_text(tg, cwd, sid="", lease_id=""):
    """The always-listen instruction block, injected as SessionStart context for
    Claude Code — the one agent with a true non-blocking background task that wakes
    the agent, so it runs the `tg listen` poll loop."""
    prefix = command_env_prefix(cwd, sid, lease_id)
    pre = (prefix + " ") if prefix else ""
    send = "%s%s send 'your reply'" % (pre, tg)
    listen = "%s%s listen" % (pre, tg)
    return (
        "Telegram always-listen is ON for this session. Early on your first turn, start "
        "listening and keep it up for the whole session:\n"
        "1. Launch the listener as a background task (Bash tool, run_in_background: true):\n"
        "     %s\n"
        "2. When that task completes it printed a Telegram message routed to this session. "
        "Read its output. Your FIRST action MUST be to reply in Telegram to acknowledge "
        "you received it (this is REQUIRED — do not act, plan, or answer only in the "
        "terminal first):\n"
        "     %s   (single quotes — avoid backticks)\n"
        "Then act on the message; if it takes a while, send progress/results to Telegram too.\n"
        "3. Then relaunch the listener (step 1) — keep EXACTLY ONE running for this session.\n"
        "Handle the listener's exit code (do NOT just relaunch on every wake):\n"
        "  - exit 0: it printed a message -> do step 2, then relaunch once.\n"
        "  - exit 3: timed out, no message -> relaunch once (quietly, no investigation).\n"
        "  - exit 4: a listener for this session is already running -> do NOTHING, do not relaunch.\n"
        "  - exit 5: the session ended or was resumed elsewhere -> do NOTHING, do not relaunch.\n"
        "Cheap: the listener uses no model tokens while waiting; you wake only on a message. "
        "The user targets a session by replying (Telegram reply-to) to its message; a message that "
        "isn't a reply we can attribute is dropped, never guessed. Stop only if asked to stop "
        "listening. Never block the terminal." % (listen, send)
    )


def hook_sessionstart(inp):
    sid = clean_session_id(inp.get("session_id", ""))
    lease_id = start_session_lease(sid)
    try:
        c = load_cfg()
    except SystemExit:
        return
    tg = ensure_launcher()
    if not c.get("chat_id") or not c.get("always_listen", False):
        return
    # Announce this session to Telegram ("I'm session X, now listening"). This
    # message is also the reply-anchor for addressing this session. Suppress the
    # command's stdout so it doesn't corrupt the hook's JSON output.
    cwd = inp.get("cwd", "") or os.getcwd()
    label = os.path.basename(cwd) or cwd
    os.environ["TG_CWD"] = cwd
    if sid:
        os.environ["TG_KEY"] = sid
        os.environ["TG_SESSION_ID"] = sid
    if lease_id:
        os.environ["TG_SESSION_LEASE"] = lease_id
    msg = ("🟢 *Сессия на связи*: `%s`\nsid `%s` · `%s`\n"
           "Слушаю Telegram. _Ответь реплаем на это сообщение, чтобы писать именно этой сессии._"
           % (label, sid[:8] or "?", cwd))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_send(msg, thread=False)  # standalone announcement — never a reply
    except Exception:
        pass
    # Pin the session key and lease in every command. TG_CWD remains the display
    # label, while replies route by session_id even when multiple sessions share it.
    ctx = always_listen_text(tg, cwd, sid, lease_id)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def hook_sessionend(inp):
    sid = clean_session_id(inp.get("session_id", ""))
    if not sid:
        return
    end_session_lease(sid)
    for prefix in ("msg-", "prompt-"):
        try:
            os.remove(os.path.join(IDLED, prefix + sid))
        except OSError:
            pass


def cmd_idlewatch(sid, armed, cwd, lease_id=""):
    promptf = os.path.join(IDLED, "prompt-" + sid)
    msgf = os.path.join(IDLED, "msg-" + sid)
    try:
        secs = int(load_cfg().get("idle_mirror_secs", 600))
    except Exception:
        secs = 600
    if secs <= 0:
        return
    if not session_lease_active(sid, lease_id):
        return

    def returned():
        try:
            with open(promptf) as f:
                return int(f.read().strip()) >= armed
        except Exception:
            return False

    waited = 0
    while waited < secs:
        if (not session_lease_active(sid, lease_id)
                or returned() or away_active(cwd)):
            return
        time.sleep(15)
        waited += 15
    if not session_lease_active(sid, lease_id) or returned():
        return
    msg = ""
    if os.path.exists(msgf):
        with open(msgf) as f:
            msg = f.read()
    os.environ["TG_CWD"] = cwd
    os.environ["TG_KEY"] = sid
    os.environ["TG_SESSION_ID"] = sid
    os.environ["TG_SESSION_LEASE"] = lease_id
    try:
        cmd_send("💤 %d мин без ответа:\n\n%s" % (secs // 60, msg or "Жду твоего ответа."))
    except Exception:
        pass
    try:
        os.remove(msgf)
    except OSError:
        pass


# ---------- dispatch ----------
def main():
    a = sys.argv[1:]
    if not a:
        die("usage: tg.py {setup|send|file|photo|recv|listen|ask|drain|away|hook}")
    cmd = a[0]
    if cmd == "setup":
        cmd_setup()
    elif cmd == "send":
        cmd_send(a[1] if len(a) > 1 else "")
    elif cmd == "file":
        cmd_media("sendDocument", "document", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else "")
    elif cmd == "photo":
        cmd_media("sendPhoto", "photo", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else "")
    elif cmd == "recv":
        cmd_recv(int(a[1]) if len(a) > 1 else 5)
    elif cmd == "listen":
        cmd_listen(int(a[1]) if len(a) > 1 else 21600)
    elif cmd == "ask":
        cmd_ask(a[1] if len(a) > 1 else "", int(a[2]) if len(a) > 2 else 120)
    elif cmd == "drain":
        cmd_drain()
    elif cmd == "away":
        cmd_away(a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else "")
    elif cmd == "hook":
        ev = a[1] if len(a) > 1 else ""
        handler = {"stop": hook_stop, "userprompt": hook_userprompt,
                   "notification": hook_notification, "sessionstart": hook_sessionstart,
                   "sessionend": hook_sessionend}.get(ev)
        if handler:
            handler(_read_hook_input())
    elif cmd == "_idlewatch":
        cmd_idlewatch(a[1], int(a[2]), a[3], a[4] if len(a) > 4 else "")
    else:
        die("unknown command: %s" % cmd)


if __name__ == "__main__":
    main()
