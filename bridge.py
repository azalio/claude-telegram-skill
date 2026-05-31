#!/usr/bin/env python3
"""Routing layer for the Telegram bridge.

Telegram's getUpdates has a single, destructive offset — you can't read a message
meant for another session and leave it. So whoever holds the lock PUMPS all new
updates into a shared local inbox, tagging each with the session it's for (matched
via reply_to_message against the sent-message map). Each session then CLAIMS only
its own messages.

Robustness (all state ops run under one exclusive flock so concurrent sessions
never corrupt the shared files):
  * inbox is written + fsync'd BEFORE the Telegram offset is advanced, so a crash
    causes at most duplicates (recoverable), never lost messages;
  * updates are de-duplicated by update_id, so crash-replays are harmless;
  * a message routed to a session that never claims it is downgraded to broadcast
    after ROUTED_TTL (a dead session shouldn't black-hole replies), and dropped
    after INBOX_TTL.

Commands:
  bridge.py recv <key> <timeout>   pump + claim in one locked op; print reply / exit 3
  bridge.py pump <timeout>         pump only (debug)
  bridge.py claim <key>            claim only (debug); exit 3 if nothing
  bridge.py record <key> <mid>...  remember message_id(s) sent by <key>
  bridge.py reset                  consume pending updates and clear the inbox
"""
import sys, os, json, time, fcntl, urllib.parse, urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(DIR, "config.json")
STATE = os.path.join(DIR, ".state")
SENT = os.path.join(DIR, "sent.map")
INBOX = os.path.join(DIR, "inbox.jsonl")
LOCKF = os.path.join(DIR, ".lock")
INBOX_TTL = 3600    # drop unclaimed messages after 1h
ROUTED_TTL = 600    # after 10min, assume the target session is gone -> broadcast
SENT_MAX = 500      # cap sent-map size


def cfg():
    with open(CONFIG) as f:
        c = json.load(f)
    return c["token"], c.get("chat_id"), c.get("user_id")


def api(method, params, timeout):
    token = cfg()[0]
    url = "https://api.telegram.org/bot%s/%s?%s" % (
        token, method, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def get_offset():
    try:
        return int(open(STATE).read().strip())
    except Exception:
        return 0


def set_offset(o):
    with open(STATE, "w") as f:
        f.write(str(o))
        f.flush()
        os.fsync(f.fileno())


def load_sentmap():
    m = {}
    if os.path.exists(SENT):
        for line in open(SENT):
            p = line.rstrip("\n").split("\t")
            if len(p) == 2:
                m[p[0]] = p[1]
    return m


def read_inbox():
    items = []
    if os.path.exists(INBOX):
        for line in open(INBOX):
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    return items


def write_inbox(items):
    tmp = INBOX + ".tmp"
    with open(tmp, "w") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, INBOX)


class Lock:
    """Exclusive flock; auto-released on close, even if the process dies."""
    def __enter__(self):
        self.f = open(LOCKF, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _pump(timeout):
    """Assumes the lock is held."""
    _, chat, user_id = cfg()
    offset = get_offset()
    try:
        resp = api("getUpdates", {"offset": offset, "timeout": timeout}, timeout + 10)
    except Exception:
        return  # transient network/API issue; caller retries later
    results = resp.get("result", [])
    sm = load_sentmap()
    items = read_inbox()
    seen = {it.get("uid") for it in items}
    now = int(time.time())
    last = offset - 1
    for u in results:
        uid = u["update_id"]
        last = max(last, uid)
        if uid in seen:
            continue
        msg = u.get("message")
        if not msg:
            continue
        if chat is not None and msg.get("chat", {}).get("id") != chat:
            continue
        # Only accept messages from the allowed user (the bot may be public).
        if user_id is not None and msg.get("from", {}).get("id") != user_id:
            continue
        text = msg.get("text")
        if not text:
            continue
        to = "*"
        rt = msg.get("reply_to_message")
        if rt:
            to = sm.get(str(rt.get("message_id")), "*")
        items.append({"to": to, "text": text, "ts": now, "uid": uid})
        seen.add(uid)
    # prune expired; downgrade long-unclaimed messages to broadcast
    kept = []
    for it in items:
        age = now - it.get("ts", now)
        if age > INBOX_TTL:
            continue
        if age > ROUTED_TTL:
            it["to"] = "*"
        kept.append(it)
    write_inbox(kept)        # durable inbox FIRST...
    set_offset(last + 1)     # ...THEN advance the Telegram offset


def _claim(key):
    items = read_inbox()
    mine = [it for it in items if it.get("to") in (key, "*")]
    rest = [it for it in items if it.get("to") not in (key, "*")]
    if not mine:
        return None
    write_inbox(rest)
    return "\n".join(it["text"] for it in mine)


def cmd_recv(key, timeout):
    with Lock():
        _pump(timeout)
        out = _claim(key)
    if out is None:
        sys.exit(3)
    print(out)


def cmd_pump(timeout):
    with Lock():
        _pump(timeout)


def cmd_claim(key):
    with Lock():
        out = _claim(key)
    if out is None:
        sys.exit(3)
    print(out)


def cmd_record(key, mids):
    if not mids:
        return
    with Lock():
        lines = open(SENT).read().splitlines() if os.path.exists(SENT) else []
        lines += ["%s\t%s" % (mid, key) for mid in mids]
        with open(SENT, "w") as f:
            f.write("\n".join(lines[-SENT_MAX:]) + "\n")
            f.flush()
            os.fsync(f.fileno())


def cmd_reset():
    with Lock():
        _pump(0)
        write_inbox([])


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit("usage: bridge.py {recv <key> <t>|pump <t>|claim <key>|record <key> <mid>...|reset}")
    cmd = a[0]
    if cmd == "recv":
        cmd_recv(a[1], int(a[2]) if len(a) > 2 else 5)
    elif cmd == "pump":
        cmd_pump(int(a[1]) if len(a) > 1 else 5)
    elif cmd == "claim":
        cmd_claim(a[1])
    elif cmd == "record":
        cmd_record(a[1], a[2:])
    elif cmd == "reset":
        cmd_reset()
    else:
        sys.exit("unknown command: " + cmd)


if __name__ == "__main__":
    main()
