#!/usr/bin/env python3
"""End-to-end tests for the telegram-bridge plugin. Standard library only, no bot
token, no network — the Telegram API is mocked. Run: python3 tests/test_e2e.py

Covers:
  * plugin structure (plugin.json / marketplace.json / hooks.json / SKILL.md / tg.py)
  * helpers: notification_message, always_listen_text phrasing, the SessionStart
    additionalContext envelope
  * inbound routing in tg.py: reply-to routing, user_id allowlist, update_id dedup,
    lifecycle-bound listeners, strict session claims, outbound message_id recording.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_JSON = os.path.join(REPO, ".claude-plugin", "plugin.json")
MARKET_JSON = os.path.join(REPO, ".claude-plugin", "marketplace.json")
HOOKS_JSON = os.path.join(REPO, "hooks", "hooks.json")
SKILL_MD = os.path.join(REPO, "skills", "telegram", "SKILL.md")
TG_PY = os.path.join(REPO, "scripts", "tg.py")


def load_tg(state_dir):
    """Import scripts/tg.py fresh, pointed at a temp state dir with a fake config."""
    os.environ["TG_STATE_DIR"] = state_dir
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "config.json"), "w") as f:
        json.dump({"token": "TEST:TOKEN", "chat_id": 111, "user_id": 222,
                   "idle_mirror_secs": 600, "always_listen": True}, f)
    spec = importlib.util.spec_from_file_location("tg_mod_%d" % id(state_dir), TG_PY)
    assert spec and spec.loader
    tg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tg)
    return tg


class FakeAPI:
    """Stand-in for tg.api. getUpdates returns a preset list; sendMessage records
    and hands back an incrementing message_id."""
    def __init__(self):
        self.updates = []
        self.sent = []
        self.calls = []
        self._mid = 1000

    def __call__(self, method, params=None, **kwargs):
        self.calls.append(method)
        if method == "getUpdates":
            return {"ok": True, "result": list(self.updates)}
        if method in ("sendMessage", "sendDocument", "sendPhoto"):
            self.sent.append((method, params, kwargs.get("files")))
            self._mid += 1
            return {"ok": True, "result": {"message_id": self._mid}}
        return {"ok": True, "result": {}}


def upd(update_id, text, from_id=222, chat_id=111, reply_to=None):
    msg = {"message_id": 9000 + update_id, "text": text,
           "from": {"id": from_id}, "chat": {"id": chat_id}}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


class StructureTests(unittest.TestCase):
    def test_plugin_json(self):
        with open(PLUGIN_JSON) as f:
            p = json.load(f)
        self.assertEqual(p["name"], "telegram-bridge")
        for k in ("description", "version"):
            self.assertIn(k, p)
        # hooks/hooks.json is auto-loaded by convention — must NOT also be declared
        self.assertNotIn("hooks", p)

    def test_marketplace_json(self):
        with open(MARKET_JSON) as f:
            m = json.load(f)
        with open(PLUGIN_JSON) as f:
            plugin = json.load(f)
        self.assertIn("name", m)
        self.assertIn("owner", m)
        names = [pl["name"] for pl in m["plugins"]]
        self.assertIn("telegram-bridge", names)
        for pl in m["plugins"]:
            self.assertIn("source", pl)
            if pl["name"] == "telegram-bridge":
                self.assertEqual(pl["version"], plugin["version"])

    def test_hooks_json(self):
        with open(HOOKS_JSON) as f:
            h = json.load(f)["hooks"]
        for ev in ("SessionStart", "SessionEnd", "Stop", "UserPromptSubmit", "Notification"):
            self.assertIn(ev, h)
            cmd = h[ev][0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", cmd)
            self.assertIn("scripts/tg.py", cmd)

    def test_skill_frontmatter(self):
        with open(SKILL_MD) as f:
            body = f.read()
        self.assertTrue(body.startswith("---"))
        fm = body.split("---", 2)[1]
        self.assertRegex(fm, r"(?m)^name:\s*telegram\s*$")
        self.assertIn("description:", fm)

    def test_tg_py_parses(self):
        import ast
        with open(TG_PY) as f:
            ast.parse(f.read())


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tg = load_tg(self.tmp)
        self.api = FakeAPI()
        setattr(self.tg, "api", self.api)

    def tearDown(self):
        for key in ("TG_CWD", "TG_KEY", "TG_SESSION_ID", "TG_SESSION_LEASE"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp)

    def _send_as(self, key, text):
        os.environ["TG_KEY"] = key
        os.environ.pop("TG_CWD", None)
        self.tg.cmd_send(text)
        return self.api._mid  # message_id just produced

    def _recv_as(self, key):
        os.environ["TG_KEY"] = key
        os.environ.pop("TG_CWD", None)
        with self.tg.Lock():
            self.tg._pump(0)
            return self.tg._claim(key)

    def test_send_records_message_id(self):
        mid = self._send_as("sessA", "hello")
        with open(os.path.join(self.tmp, "sent.map")) as f:
            sent_map = f.read()
        self.assertIn("%s\tsessA" % mid, sent_map)
        self.assertEqual(self.api.sent[0][0], "sendMessage")

    def test_reply_routes_to_owning_session(self):
        mid = self._send_as("sessA", "question from A")
        self.api.updates = [upd(1, "answer for A", reply_to=mid)]
        # B claims first: must NOT get A's reply
        self.assertIsNone(self._recv_as("sessB"))
        # A gets it
        self.assertEqual(self._recv_as("sessA"), "answer for A")

    def test_user_id_allowlist(self):
        mid = self._send_as("sessA", "q")
        self.api.updates = [upd(2, "stranger", from_id=999, reply_to=mid)]
        self.assertIsNone(self._recv_as("sessA"))

    def test_dedup_by_update_id(self):
        mid = self._send_as("sessA", "q")  # so the reply is attributable
        self.api.updates = [upd(3, "addressed hi", reply_to=mid)]
        with self.tg.Lock():
            self.tg._pump(0)
            self.tg._pump(0)  # same update again (e.g. crash replay)
        items = self.tg.read_inbox()
        self.assertEqual(len([i for i in items if i["uid"] == 3]), 1)

    def test_plain_message_without_reply_is_dropped(self):
        # no guessing: a message with no reply-to is dropped, never delivered.
        self.api.updates = [upd(4, "no reply, to nobody")]
        self.assertIsNone(self._recv_as("whoever"))
        self.assertEqual(self.tg.read_inbox(), [])  # not held anywhere
        # the user gets a one-line nudge to reply to a session
        hint = [s for s in self.api.sent if "реплаем" in (s[1] or {}).get("text", "")]
        self.assertEqual(len(hint), 1)

    def test_reply_to_unknown_message_is_dropped(self):
        # a reply to a message_id not in sent.map can't be attributed -> dropped.
        self.api.updates = [upd(5, "reply to a stranger msg", reply_to=999999)]
        self.assertIsNone(self._recv_as("whoever"))
        self.assertEqual(self.tg.read_inbox(), [])

    def test_send_threads_onto_last_inbound(self):
        # after a session claims a message, its next send() replies onto that message
        self._send_as("sessA", "q from A")
        self.api.updates = [upd(8, "do the thing", reply_to=self.api._mid)]
        self.assertEqual(self._recv_as("sessA"), "do the thing")
        # incoming user message_id is 9000+8 = 9008; the reply must thread onto it
        self.api.sent.clear()
        self._send_as("sessA", "on it")
        params = self.api.sent[-1][1]
        self.assertEqual(params.get("reply_to_message_id"), 9008)
        self.assertTrue(params.get("allow_sending_without_reply"))

    def test_send_without_inbound_is_not_a_reply(self):
        # a fresh session that never claimed anything sends a plain (unthreaded) message
        self._send_as("loneSession", "hello world")
        params = self.api.sent[-1][1]
        self.assertNotIn("reply_to_message_id", params)

    def test_send_thread_false_is_not_a_reply(self):
        # the SessionStart announcement must be a standalone message even when a
        # stale reply target exists, since the replied-to message may be gone.
        self.tg.set_reply_target("sessA", 4242)  # a leftover target from before
        os.environ["TG_KEY"] = "sessA"
        os.environ.pop("TG_CWD", None)
        self.tg.cmd_send("session announcement", thread=False)
        params = self.api.sent[-1][1]
        self.assertNotIn("reply_to_message_id", params)
        # sanity: with thread=True it WOULD have threaded onto 4242
        self.tg.cmd_send("a reply", thread=True)
        self.assertEqual(self.api.sent[-1][1].get("reply_to_message_id"), 4242)

    def test_label_is_bold_header_on_own_line(self):
        os.environ.pop("TG_LABEL", None)
        os.environ["TG_CWD"] = "/Users/x/gitroot/demoproj"
        try:
            self.tg.cmd_send("hello body")
        finally:
            os.environ.pop("TG_CWD", None)
        text = self.api.sent[-1][1]["text"]
        self.assertTrue(text.startswith("*demoproj*\n"))
        self.assertEqual(text, "*demoproj*\nhello body")

    def test_every_outbound_id_recorded_no_holes(self):
        # the root cause of "Делай фичу 2" going astray: sent.map had holes because
        # nudges weren't recorded. Now an unattributed message triggers a nudge whose
        # id IS recorded, so the map has no gaps.
        self.api.updates = [upd(14, "stray, no reply")]
        with self.tg.Lock():
            self.tg._pump(0)
        with open(os.path.join(self.tmp, "sent.map")) as f:
            sent_map = f.read()
        self.assertIn("%s\t__nudge__" % self.api._mid, sent_map)

    def test_reply_to_nudge_is_not_misrouted(self):
        # replying to the bot's nudge resolves to no real session -> not delivered.
        self.api.updates = [upd(15, "stray")]
        with self.tg.Lock():
            self.tg._pump(0)
        nudge_id = self.api._mid
        self.api.updates = [upd(16, "replying to the nudge", reply_to=nudge_id)]
        self.assertIsNone(self._recv_as("sessA"))

    def test_reply_routes_when_many_sessions_exist(self):
        # routing is by reply-id alone; the number of other sessions is irrelevant.
        midA = self._send_as("sessA", "q from A")
        self._send_as("sessB", "q from B")
        self._send_as("sessC", "q from C")
        self.api.updates = [upd(13, "answer A", reply_to=midA)]
        self.assertIsNone(self._recv_as("sessB"))
        self.assertIsNone(self._recv_as("sessC"))
        self.assertEqual(self._recv_as("sessA"), "answer A")

    def test_addressed_message_never_stolen(self):
        # the original bug: a message addressed to a busy session (not currently
        # listening) must NOT be handed to a different live session. It waits.
        self.tg.write_inbox([{"to": "busySession", "text": "for busy only",
                              "ts": int(time.time()), "uid": 7, "mid": 500}])
        # a different, live session pumps + claims: it must not get the message
        self.assertIsNone(self._recv_as("otherSession"))
        # and the message is still there, still addressed to the busy session
        items = self.tg.read_inbox()
        self.assertEqual(items[0]["to"], "busySession")
        # when the intended session finally listens, it claims its own message
        self.assertEqual(self._recv_as("busySession"), "for busy only")

    def test_listen_singleton_exits_4_when_already_running(self):
        # a second listener for the same session must exit 4 (not 3), so the caller
        # can tell "already listening" apart from "timed out, relaunch".
        import fcntl
        key = "sessSingleton"
        os.environ["TG_KEY"] = key
        os.environ.pop("TG_CWD", None)
        lock_path = os.path.join(self.tmp, "listen." + key + ".lock")
        held = open(lock_path, "w")
        fcntl.flock(held, fcntl.LOCK_EX)  # simulate an already-running listener
        code = None
        try:
            try:
                self.tg.cmd_listen(1)
            except SystemExit as exc:
                code = exc.code
                fcntl.flock(held, fcntl.LOCK_UN)
                held.close()
                held = None
                exc.__traceback__ = None
        finally:
            if held is not None:
                fcntl.flock(held, fcntl.LOCK_UN)
                held.close()
        self.assertEqual(code, 4)

    def test_expired_message_dropped_not_reassigned(self):
        # an unclaimed addressed message expires at INBOX_TTL; it is never broadcast.
        old_ts = int(time.time()) - (self.tg.INBOX_TTL + 60)
        self.tg.write_inbox([{"to": "ghost", "text": "stale",
                              "ts": old_ts, "uid": 9, "mid": 600}])
        with self.tg.Lock():
            self.tg._pump(0)  # prune runs inside pump
        self.assertEqual(self.tg.read_inbox(), [])


class HelperTests(unittest.TestCase):
    """The agent helpers: notification_message, always_listen_text phrasing, and
    the SessionStart additionalContext envelope."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tg = load_tg(os.path.join(self.tmp, "state"))
        self.api = FakeAPI()
        setattr(self.tg, "api", self.api)

    def tearDown(self):
        for key in ("TG_CWD", "TG_KEY", "TG_SESSION_ID", "TG_SESSION_LEASE"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp)

    def test_notification_message(self):
        self.assertEqual(self.tg.notification_message({"message": "hi"}), "hi")
        self.assertTrue(self.tg.notification_message({}))   # non-empty fallback

    def test_always_listen_text_phrasing(self):
        # Claude Code runs the `tg listen` loop as a non-blocking background task.
        txt = self.tg.always_listen_text("/x/tg", "/cwd")
        self.assertIn("run_in_background", txt)
        self.assertIn("/x/tg listen", txt)
        self.assertIn("exit 0:", txt)                       # listen-loop exit-code handling
        self.assertIn("TG_CWD='/cwd'", txt)                 # cwd pinned into the commands

    def test_sessionstart_emits_additionalcontext(self):
        os.environ["TG_CWD"] = "/Users/x/proj"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.tg.hook_sessionstart({"cwd": "/Users/x/proj", "session_id": "abcd1234"})
        out = json.loads(buf.getvalue())                 # only the envelope reaches stdout
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("Telegram always-listen is ON", hso["additionalContext"])
        self.assertIn("run_in_background", hso["additionalContext"])

    def test_sessionend_stops_listener_generation(self):
        sid = "session-123"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.tg.hook_sessionstart({"cwd": "/Users/x/proj", "session_id": sid})
        context = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
        lease_id = os.environ["TG_SESSION_LEASE"]
        self.assertIn("TG_KEY='session_123'", context)
        self.assertIn("TG_SESSION_LEASE='%s'" % lease_id, context)

        self.tg.hook_sessionend({"cwd": "/Users/x/proj", "session_id": sid})
        with self.assertRaises(SystemExit) as stopped:
            self.tg.cmd_listen(10)
        self.assertEqual(stopped.exception.code, 5)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tg = load_tg(os.path.join(self.tmp, "state"))
        self.api = FakeAPI()
        setattr(self.tg, "api", self.api)

    def tearDown(self):
        for key in ("TG_CWD", "TG_KEY", "TG_SESSION_ID", "TG_SESSION_LEASE"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp)

    def _start(self, sid):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.tg.hook_sessionstart({"cwd": "/same/cwd", "session_id": sid})
        return os.environ["TG_SESSION_LEASE"]

    def test_running_listener_observes_sessionend(self):
        sid = "live-session"
        self._start(sid)
        original_sleep = self.tg.time.sleep
        self.tg.time.sleep = lambda _seconds: self.tg.hook_sessionend({
            "cwd": "/same/cwd", "session_id": sid,
        })
        try:
            with self.assertRaises(SystemExit) as stopped:
                self.tg.cmd_listen(30)
        finally:
            self.tg.time.sleep = original_sleep
        self.assertEqual(stopped.exception.code, 5)

    def test_listener_does_not_poll_after_end_while_waiting_for_lock(self):
        sid = "lock-race-session"
        self._start(sid)
        lifecycle = self

        class EndSessionOnEnter:
            def __enter__(self):
                lifecycle.tg.hook_sessionend({
                    "cwd": "/same/cwd", "session_id": sid,
                })
                return self

            def __exit__(self, *_args):
                return None

        original_lock = self.tg.Lock
        self.tg.Lock = EndSessionOnEnter
        self.api.calls.clear()
        try:
            with self.assertRaises(SystemExit) as stopped:
                self.tg.cmd_listen(30)
        finally:
            self.tg.Lock = original_lock
        self.assertEqual(stopped.exception.code, 5)
        self.assertNotIn("getUpdates", self.api.calls)

    def test_idlewatch_does_not_mirror_after_sessionend(self):
        sid = "idle-session"
        lease_id = self._start(sid)
        config_path = os.path.join(self.tmp, "state", "config.json")
        with open(config_path) as f:
            config = json.load(f)
        config["idle_mirror_secs"] = 15
        with open(config_path, "w") as f:
            json.dump(config, f)

        original_sleep = self.tg.time.sleep
        self.tg.time.sleep = lambda _seconds: self.tg.hook_sessionend({
            "cwd": "/same/cwd", "session_id": sid,
        })
        try:
            self.tg.cmd_idlewatch(sid, int(time.time()), "/same/cwd", lease_id)
        finally:
            self.tg.time.sleep = original_sleep
        mirrored = [item for item in self.api.sent
                    if "мин без ответа" in (item[1] or {}).get("text", "")]
        self.assertEqual(mirrored, [])

    def test_idlewatch_mirror_keeps_session_routing_key(self):
        sid = "mirror-session"
        lease_id = self._start(sid)
        config_path = os.path.join(self.tmp, "state", "config.json")
        with open(config_path) as f:
            config = json.load(f)
        config["idle_mirror_secs"] = 1
        with open(config_path, "w") as f:
            json.dump(config, f)
        for key in ("TG_KEY", "TG_SESSION_ID", "TG_SESSION_LEASE"):
            os.environ.pop(key, None)

        original_sleep = self.tg.time.sleep
        self.tg.time.sleep = lambda _seconds: None
        try:
            self.tg.cmd_idlewatch(sid, int(time.time()), "/same/cwd", lease_id)
        finally:
            self.tg.time.sleep = original_sleep
        self.assertEqual(self.tg.load_sentmap()[str(self.api._mid)], "mirror_session")

    def test_same_cwd_sessions_have_distinct_routing_keys(self):
        self._start("session-A")
        first_mid = self.api._mid
        self._start("session-B")
        second_mid = self.api._mid
        sent_map = self.tg.load_sentmap()
        self.assertEqual(sent_map[str(first_mid)], "session_A")
        self.assertEqual(sent_map[str(second_mid)], "session_B")

    def test_resumed_generation_invalidates_old_listener(self):
        sid = "resumed-session"
        old_lease = self._start(sid)
        new_lease = self._start(sid)
        self.assertNotEqual(old_lease, new_lease)
        os.environ["TG_KEY"] = "resumed_session"
        os.environ["TG_SESSION_ID"] = "resumed_session"
        os.environ["TG_SESSION_LEASE"] = old_lease
        with self.assertRaises(SystemExit) as stopped:
            self.tg.cmd_listen(30)
        self.assertEqual(stopped.exception.code, 5)

    def test_away_notification_keeps_session_routing_key(self):
        sid = "notification-session"
        self._start(sid)
        with contextlib.redirect_stdout(io.StringIO()):
            self.tg.cmd_away("on", "/same/cwd")
        self.tg.hook_notification({
            "cwd": "/same/cwd", "session_id": sid, "message": "input needed",
        })
        self.assertEqual(self.tg.load_sentmap()[str(self.api._mid)],
                         "notification_session")

    def test_away_notification_is_suppressed_after_sessionend(self):
        sid = "ended-notification-session"
        self._start(sid)
        with contextlib.redirect_stdout(io.StringIO()):
            self.tg.cmd_away("on", "/same/cwd")
        self.tg.hook_sessionend({"cwd": "/same/cwd", "session_id": sid})
        self.api.sent.clear()
        self.tg.hook_notification({
            "cwd": "/same/cwd", "session_id": sid, "message": "stale input",
        })
        self.assertEqual(self.api.sent, [])

    def test_cli_listener_exits_5_for_ended_session(self):
        sid = "process-session"
        lease_id = self._start(sid)
        self.tg.hook_sessionend({"cwd": "/same/cwd", "session_id": sid})
        env = dict(os.environ, TG_STATE_DIR=os.path.join(self.tmp, "state"),
                   TG_KEY="process_session", TG_SESSION_ID="process_session",
                   TG_SESSION_LEASE=lease_id)
        result = subprocess.run(
            [sys.executable, TG_PY, "listen", "30"],
            env=env, capture_output=True, text=True, timeout=2, check=False,
        )
        self.assertEqual(result.returncode, 5, result.stderr)

    def test_session_state_is_versioned_json(self):
        sid = "json-session"
        lease_id = self._start(sid)
        path = os.path.join(self.tmp, "state", "sessions.d", "json_session.json")
        with open(path) as f:
            active = json.load(f)
        self.assertEqual(set(active), {
            "lease_id", "schema_version", "session_id", "started_at", "status",
        })
        self.assertEqual(active["lease_id"], lease_id)
        self.assertEqual(active["schema_version"], 1)
        self.assertEqual(active["session_id"], "json_session")
        self.assertEqual(active["status"], "active")
        self.assertIsInstance(active["started_at"], int)

        self.tg.hook_sessionend({"cwd": "/same/cwd", "session_id": sid})
        with open(path) as f:
            ended = json.load(f)
        self.assertEqual(ended["status"], "ended")
        self.assertEqual(ended["lease_id"], lease_id)
        self.assertIsInstance(ended["ended_at"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
