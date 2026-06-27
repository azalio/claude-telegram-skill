#!/usr/bin/env python3
"""End-to-end tests for the telegram-bridge plugin. Standard library only, no bot
token, no network — the Telegram API is mocked. Run: python3 tests/test_e2e.py

Covers:
  * plugin structure (plugin.json / marketplace.json / hooks.json / SKILL.md / tg.py)
  * Codex & opencode adapter assets (codex/hooks.json, opencode TS plugin)
  * `tg.py install codex|opencode`: merge into a temp CODEX_HOME / OPENCODE_CONFIG_DIR,
    path substitution, idempotency
  * agent-agnostic helpers: notification_message (Claude vs Codex payloads),
    always_listen_text phrasing, the SessionStart additionalContext envelope
  * inbound routing in tg.py: reply-to routing, user_id allowlist, update_id dedup,
    dead-session downgrade-to-broadcast, broadcast claim, outbound message_id recording.
"""
import os, io, json, time, contextlib, tempfile, importlib.util, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_JSON = os.path.join(REPO, ".claude-plugin", "plugin.json")
MARKET_JSON = os.path.join(REPO, ".claude-plugin", "marketplace.json")
HOOKS_JSON = os.path.join(REPO, "hooks", "hooks.json")
SKILL_MD = os.path.join(REPO, "skills", "telegram", "SKILL.md")
TG_PY = os.path.join(REPO, "scripts", "tg.py")
CODEX_HOOKS = os.path.join(REPO, "codex", "hooks.json")
OPENCODE_PLUGIN = os.path.join(REPO, "opencode", "plugin", "telegram-bridge.ts")


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
        self._mid = 1000

    def __call__(self, method, params=None, **kwargs):
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
        self.assertIn("name", m)
        self.assertIn("owner", m)
        names = [pl["name"] for pl in m["plugins"]]
        self.assertIn("telegram-bridge", names)
        for pl in m["plugins"]:
            self.assertIn("source", pl)

    def test_hooks_json(self):
        with open(HOOKS_JSON) as f:
            h = json.load(f)["hooks"]
        for ev in ("SessionStart", "Stop", "UserPromptSubmit", "Notification"):
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

    def test_codex_hooks_template(self):
        with open(CODEX_HOOKS) as f:
            h = json.load(f)["hooks"]
        # Codex's PermissionRequest is our Notification analogue.
        want = {"SessionStart": "sessionstart", "Stop": "stop",
                "UserPromptSubmit": "userprompt", "PermissionRequest": "notification"}
        for ev, sub in want.items():
            self.assertIn(ev, h)
            cmd = h[ev][0]["hooks"][0]["command"]
            self.assertIn("__TG_PY__", cmd)          # resolved to an abs path at install
            self.assertIn("TG_AGENT=codex", cmd)     # so phrasing/labels say Codex
            self.assertIn("hook %s" % sub, cmd)

    def test_opencode_plugin_asset(self):
        with open(OPENCODE_PLUGIN) as f:
            src = f.read()
        self.assertIn("__TG_PY__", src)              # resolved to an abs path at install
        self.assertIn("export const TelegramBridge", src)
        # maps the four events we bridge
        for needle in ("session.created", "session.idle", "chat.message", "permission.ask"):
            self.assertIn(needle, src)
        # inbound is plugin-driven: poll tg.py recv + inject via SDK (no blocking listen)
        self.assertIn("promptAsync", src)
        self.assertIn('"recv"', src)
        self.assertNotIn('"listen"', src)           # never spawns a blocking listener


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tg = load_tg(self.tmp)
        self.api = FakeAPI()
        setattr(self.tg, "api", self.api)

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
                fcntl.flock(held, fcntl.LOCK_UN); held.close(); held = None
                exc.__traceback__ = None
        finally:
            if held is not None:
                fcntl.flock(held, fcntl.LOCK_UN); held.close()
        self.assertEqual(code, 4)

    def test_expired_message_dropped_not_reassigned(self):
        # an unclaimed addressed message expires at INBOX_TTL; it is never broadcast.
        old_ts = int(time.time()) - (self.tg.INBOX_TTL + 60)
        self.tg.write_inbox([{"to": "ghost", "text": "stale",
                              "ts": old_ts, "uid": 9, "mid": 600}])
        with self.tg.Lock():
            self.tg._pump(0)  # prune runs inside pump
        self.assertEqual(self.tg.read_inbox(), [])


class AdapterTests(unittest.TestCase):
    """The Codex & opencode adapters: `tg.py install`, idempotency, and the
    agent-agnostic helpers that let one runtime serve all three agents."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tg = load_tg(os.path.join(self.tmp, "state"))
        self.api = FakeAPI()
        setattr(self.tg, "api", self.api)
        self.codex_home = os.path.join(self.tmp, "codex")
        self.oc_dir = os.path.join(self.tmp, "oc")
        os.environ["CODEX_HOME"] = self.codex_home
        os.environ["OPENCODE_CONFIG_DIR"] = self.oc_dir

    def tearDown(self):
        for k in ("CODEX_HOME", "OPENCODE_CONFIG_DIR", "TG_AGENT", "TG_CWD"):
            os.environ.pop(k, None)

    def test_install_codex_merges_and_is_idempotent(self):
        self.tg.cmd_install("codex")
        path = os.path.join(self.codex_home, "hooks.json")
        with open(path) as f:
            h = json.load(f)["hooks"]
        self.assertEqual(sorted(h),
                         ["PermissionRequest", "SessionStart", "Stop", "UserPromptSubmit"])
        cmd = h["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(self.tg.SELF, cmd)          # placeholder resolved to abs path
        self.assertNotIn("__TG_PY__", cmd)
        # reinstall must not duplicate our entries
        self.tg.cmd_install("codex")
        with open(path) as f:
            h2 = json.load(f)["hooks"]
        self.assertEqual(len(h2["SessionStart"]), 1)
        self.assertEqual(len(h2["Stop"]), 1)

    def test_install_codex_preserves_foreign_hooks(self):
        os.makedirs(self.codex_home, exist_ok=True)
        path = os.path.join(self.codex_home, "hooks.json")
        with open(path, "w") as f:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "other-tool"}]}]}}, f)
        self.tg.cmd_install("codex")
        with open(path) as f:
            entries = json.load(f)["hooks"]["SessionStart"]
        cmds = [e["hooks"][0]["command"] for e in entries]
        self.assertIn("other-tool", cmds)                       # foreign entry kept
        self.assertTrue(any(self.tg.SELF in c for c in cmds))   # ours added

    def test_install_opencode_writes_plugin_and_agents(self):
        self.tg.cmd_install("opencode")
        with open(os.path.join(self.oc_dir, "plugin", "telegram-bridge.ts")) as f:
            src = f.read()
        self.assertIn(self.tg.SELF, src)
        self.assertNotIn("__TG_PY__", src)
        with open(os.path.join(self.oc_dir, "AGENTS.md")) as f:
            agents = f.read()
        self.assertIn("<!-- telegram-bridge:begin -->", agents)
        self.assertIn("Telegram bridge is ON", agents)        # non-blocking opencode phrasing
        self.assertNotIn("run_in_background", agents)         # no Claude-only listen loop
        # idempotent: reinstall keeps exactly one marked block
        self.tg.cmd_install("opencode")
        with open(os.path.join(self.oc_dir, "AGENTS.md")) as f:
            self.assertEqual(f.read().count("<!-- telegram-bridge:begin -->"), 1)

    def test_install_opencode_preserves_existing_agents(self):
        os.makedirs(self.oc_dir, exist_ok=True)
        with open(os.path.join(self.oc_dir, "AGENTS.md"), "w") as f:
            f.write("# My rules\n\nkeep me\n")
        self.tg.cmd_install("opencode")
        with open(os.path.join(self.oc_dir, "AGENTS.md")) as f:
            agents = f.read()
        self.assertIn("keep me", agents)
        self.assertIn("<!-- telegram-bridge:begin -->", agents)

    def test_notification_message_from_claude_and_codex(self):
        # Claude Notification carries `message`; Codex PermissionRequest carries tool data.
        self.assertEqual(self.tg.notification_message({"message": "hi"}), "hi")
        codex = self.tg.notification_message(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}})
        self.assertIn("Bash", codex)
        self.assertIn("rm -rf x", codex)
        self.assertTrue(self.tg.notification_message({}))   # non-empty fallback

    def test_always_listen_text_phrasing(self):
        # Only Claude Code is told to run the blocking `tg listen` loop (it has a true
        # background task). Codex/opencode shells block the turn, so they must NOT.
        claude = self.tg.always_listen_text("/x/tg", "/cwd", "claude")
        self.assertIn("run_in_background", claude)
        self.assertIn("/x/tg listen", claude)
        for agent in ("codex", "opencode"):
            txt = self.tg.always_listen_text("/x/tg", "/cwd", agent)
            self.assertNotIn("run_in_background", txt)
            self.assertNotIn("/x/tg listen", txt)            # no launch of a blocking listener
            self.assertNotIn("exit 0:", txt)                 # no listen-loop exit-code handling
            self.assertIn("Do NOT run a blocking", txt)

    def test_codex_stop_reinjects_pending_message(self):
        # Codex inbound: a Telegram message waiting at turn end is emitted as a Stop-hook
        # "block" decision so Codex re-injects it as the next prompt.
        os.environ["TG_AGENT"] = "codex"
        os.environ["TG_CWD"] = "/Users/x/proj"
        key = self.tg.session_key()
        self.tg.write_inbox([{"to": key, "text": "do the thing",
                              "ts": int(time.time()), "uid": 1, "mid": 5}])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            injected = self.tg.stop_reinject("/Users/x/proj")
        self.assertTrue(injected)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "block")
        self.assertIn("do the thing", out["reason"])
        # message was claimed exactly once -> no continuation loop
        self.assertEqual(self.tg.read_inbox(), [])

    def test_codex_stop_reinject_noop_when_empty(self):
        os.environ["TG_AGENT"] = "codex"
        os.environ["TG_CWD"] = "/Users/x/proj2"
        self.tg.write_inbox([])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            injected = self.tg.stop_reinject("/Users/x/proj2")
        self.assertFalse(injected)
        self.assertEqual(buf.getvalue(), "")   # nothing printed -> turn ends normally

    def test_sessionstart_emits_additionalcontext(self):
        os.environ["TG_AGENT"] = "codex"
        os.environ["TG_CWD"] = "/Users/x/proj"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.tg.hook_sessionstart({"cwd": "/Users/x/proj", "session_id": "abcd1234"})
        out = json.loads(buf.getvalue())                 # only the envelope reaches stdout
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("Telegram bridge is ON", hso["additionalContext"])  # non-claude phrasing
        self.assertIn("Do NOT run a blocking", hso["additionalContext"])
        self.assertNotIn("run_in_background", hso["additionalContext"])    # never the listen loop


if __name__ == "__main__":
    unittest.main(verbosity=2)
