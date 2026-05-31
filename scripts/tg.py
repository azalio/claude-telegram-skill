#!/usr/bin/env python3
"""Telegram bridge for Claude Code — single-file, standard library only.

One script handles everything: outbound (send/file/photo), inbound routing
(pump/claim under an flock so multiple sessions share Telegram's single
getUpdates consumer), the always-on background listener, and the Claude Code
hooks (Stop / UserPromptSubmit / Notification / SessionStart).

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
  tg.py hook stop|userprompt|notification|sessionstart   (reads hook JSON on stdin)

Env: TG_CWD / TG_LABEL set the per-session routing key and the outbound label.
Exit codes: 0 ok, 2 config missing/invalid, 3 timeout/no-message, 1 other.
"""
import sys, os, json, time, fcntl, subprocess, io, contextlib, urllib.parse, urllib.request

STATE_DIR = os.environ.get("TG_STATE_DIR") or os.path.expanduser("~/.claude/telegram")
CONFIG = os.path.join(STATE_DIR, "config.json")
STATE = os.path.join(STATE_DIR, "state")
SENT = os.path.join(STATE_DIR, "sent.map")
INBOX = os.path.join(STATE_DIR, "inbox.jsonl")
LOCKF = os.path.join(STATE_DIR, "lock")
AWAYD = os.path.join(STATE_DIR, "away.d")
IDLED = os.path.join(STATE_DIR, "idle.d")
BEATD = os.path.join(STATE_DIR, "beat.d")
INBOX_TTL = 3600     # drop unclaimed messages after 1h
HEARTBEAT_TTL = 120  # a session counts as "listening" if it touched within this
SENT_MAX = 500
AMBIGUOUS = "__ambiguous__"  # held msg that can't be addressed to one session
SELF = os.path.abspath(__file__)
RETURN_PHRASES = ("вернул", "в терминал", "i'm back", "im back", "back to terminal", "/stop")


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
        f.write(str(o)); f.flush(); os.fsync(f.fileno())


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


def beat(key=None):
    """Mark this session as currently listening. mtime of the marker is the heartbeat."""
    key = key or session_key()
    try:
        os.makedirs(BEATD, exist_ok=True)
        open(os.path.join(BEATD, key), "w").write(str(int(time.time())))
    except OSError:
        pass


def live_sessions():
    """Set of session keys whose heartbeat is fresh (within HEARTBEAT_TTL)."""
    live = set()
    if not os.path.isdir(BEATD):
        return live
    now = time.time()
    for fn in os.listdir(BEATD):
        try:
            if now - os.path.getmtime(os.path.join(BEATD, fn)) <= HEARTBEAT_TTL:
                live.add(fn)
        except OSError:
            pass
    return live


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
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


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


def record_mids(mids):
    mids = [str(m) for m in mids if m]
    if not mids:
        return
    with Lock():
        lines = open(SENT).read().splitlines() if os.path.exists(SENT) else []
        lines += ["%s\t%s" % (m, session_key()) for m in mids]
        with open(SENT, "w") as f:
            f.write("\n".join(lines[-SENT_MAX:]) + "\n"); f.flush(); os.fsync(f.fileno())


def cmd_send(arg):
    if chat_id() is None:
        die("chat_id not set — run: tg.py setup", 2)
    text = sys.stdin.read() if arg == "-" else (arg or "")
    if not text:
        die('usage: tg.py send "text"   (or: ... | tg.py send -)')
    text = label_prefix() + text
    rt = get_reply_target(session_key())  # thread onto the user's last message to us
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
        f.flush(); os.fsync(f.fileno())
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
        rt = msg.get("reply_to_message")
        live = live_sessions()
        nudge = False
        if rt:
            # Explicit reply: route to the owning session. If the replied-to message
            # rotated out of sent.map we can't tell which session it was — hold it
            # as ambiguous rather than guess (never deliver to the wrong session).
            owner = sm.get(str(rt.get("message_id")))
            if owner:
                to = owner
            else:
                to, nudge = AMBIGUOUS, len(live) >= 2
        elif len(live) == 1:
            # Exactly one session is listening — a plain message is unambiguous.
            to = next(iter(live))
        else:
            # Zero or several listening — a plain message can't be addressed to one.
            # Hold it; if several are live, nudge the user to reply to a session.
            to, nudge = AMBIGUOUS, len(live) >= 2
        if nudge:
            try:
                _send_chunk("🤔 Несколько сессий слушают — непонятно, кому это. "
                            "Ответь реплаем на сообщение нужной сессии.",
                            reply_to=msg.get("message_id"))
            except Exception:
                pass
        items.append({"to": to, "text": text, "ts": now, "uid": uid_,
                      "mid": msg.get("message_id")})
        seen.add(uid_)
    # No broadcast: a message is only ever delivered to its addressed session (or
    # claimed from the ambiguous hold by a sole live session). An unclaimed message
    # simply expires — it is never reassigned to a different session.
    kept = [it for it in items if now - it.get("ts", now) <= INBOX_TTL]
    write_inbox(kept)          # durable inbox FIRST ...
    set_offset(last + 1)       # ... then advance the Telegram offset


def _claim(key):
    items = read_inbox()
    # A session claims messages addressed to it. An ambiguous hold (no session could
    # be determined) is claimable only when this is the sole live session, so the
    # message isn't lost — but a message addressed to another session is never taken.
    sole = live_sessions() == {key}

    def mine_pred(it):
        t = it.get("to")
        return t == key or (sole and t == AMBIGUOUS)

    mine = [it for it in items if mine_pred(it)]
    rest = [it for it in items if not mine_pred(it)]
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
        beat()
        _pump(timeout)
        out = _claim(session_key())
    if out is None:
        sys.exit(3)
    print(out)


def cmd_listen(maxsecs):
    if chat_id() is None:
        die("chat_id not set — run: tg.py setup", 2)
    start = time.time()
    while time.time() - start < maxsecs:
        with Lock():
            beat()
            _pump(5)
            out = _claim(session_key())
        if out is not None:
            print(out)
            return
        time.sleep(2 + (int(time.time()) % 4))
    sys.exit(3)


def cmd_ask(text, budget):
    if not text:
        die('usage: tg.py ask "text"')
    cmd_send(text)
    start = time.time()
    while time.time() - start < budget:
        with Lock():
            beat()
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
        open(mp, "w").write(me); print("away on: %s" % me)
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
                print(open(os.path.join(AWAYD, fn)).read())
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
    sid = "".join(ch if ch.isalnum() else "_" for ch in (inp.get("session_id", "") or ""))
    # In away mode the listener handles replies; otherwise arm the idle auto-mirror.
    if away_active(cwd):
        return
    try:
        secs = int(load_cfg().get("idle_mirror_secs", 600))
    except Exception:
        secs = 600
    if secs > 0 and sid:
        os.makedirs(IDLED, exist_ok=True)
        with open(os.path.join(IDLED, "msg-" + sid), "w") as f:
            f.write(last)
        env = dict(os.environ, TG_CWD=cwd)
        subprocess.Popen([sys.executable, SELF, "_idlewatch", sid, str(int(time.time())), cwd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def hook_userprompt(inp):
    cwd = inp.get("cwd", "")
    sid = "".join(ch if ch.isalnum() else "_" for ch in (inp.get("session_id", "") or ""))
    if sid:
        os.makedirs(IDLED, exist_ok=True)
        open(os.path.join(IDLED, "prompt-" + sid), "w").write(str(int(time.time())))
    if cwd:
        try:
            os.remove(marker_path(cwd))
        except OSError:
            pass


def hook_notification(inp):
    cwd = inp.get("cwd", "")
    if not away_active(cwd):
        return
    msg = inp.get("message") or "Claude ждёт твоего ввода."
    os.environ["TG_CWD"] = cwd
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


def hook_sessionstart(inp):
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
    sid = (inp.get("session_id", "") or "")[:8]
    label = os.path.basename(cwd) or cwd
    os.environ["TG_CWD"] = cwd
    msg = ("🟢 *Сессия на связи*: `%s`\nsid `%s` · `%s`\n"
           "Слушаю Telegram. _Ответь реплаем на это сообщение, чтобы писать именно этой сессии._"
           % (label, sid or "?", cwd))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_send(msg)
    except Exception:
        pass
    # Pin TG_CWD in the commands so the listener's routing key matches the startup
    # announcement's key even if you cd elsewhere — replies to this session's
    # message then reliably come back to THIS session.
    pre = "TG_CWD='%s' " % cwd
    ctx = (
        "Telegram always-listen is ON for this session. Early on your first turn, start "
        "listening and keep it up for the whole session:\n"
        "1. Launch the listener as a background task (Bash tool, run_in_background: true):\n"
        "     %s%s listen\n"
        "2. When that task completes it printed a Telegram message routed to this session. "
        "Read its output, act on it, and reply with:\n"
        "     %s%s send 'your reply'   (single quotes — avoid backticks)\n"
        "3. Then IMMEDIATELY relaunch the listener (step 1). If it exited with no output, just relaunch.\n"
        "Cheap: the background task uses no model tokens while waiting; you wake only on a message. "
        "The user targets a session by replying (Telegram reply-to) to its message. Stop only if asked "
        "to stop listening. Never block the terminal." % (pre, tg, pre, tg)
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def cmd_idlewatch(sid, armed, cwd):
    promptf = os.path.join(IDLED, "prompt-" + sid)
    msgf = os.path.join(IDLED, "msg-" + sid)
    try:
        secs = int(load_cfg().get("idle_mirror_secs", 600))
    except Exception:
        secs = 600
    if secs <= 0:
        return

    def returned():
        try:
            with open(promptf) as f:
                return int(f.read().strip()) >= armed
        except Exception:
            return False

    waited = 0
    while waited < secs:
        if returned() or away_active(cwd):
            return
        time.sleep(15); waited += 15
    if returned():
        return
    msg = ""
    if os.path.exists(msgf):
        with open(msgf) as f:
            msg = f.read()
    os.environ["TG_CWD"] = cwd
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
                   "notification": hook_notification, "sessionstart": hook_sessionstart}.get(ev)
        if handler:
            handler(_read_hook_input())
    elif cmd == "_idlewatch":
        cmd_idlewatch(a[1], int(a[2]), a[3])
    else:
        die("unknown command: %s" % cmd)


if __name__ == "__main__":
    main()
