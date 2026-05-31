#!/usr/bin/env python3
"""End-to-end tests for the telegram-bridge plugin. Standard library only, no bot
token, no network — the Telegram API is mocked. Run: python3 tests/test_e2e.py

Covers:
  * plugin structure (plugin.json / marketplace.json / hooks.json / SKILL.md / tg.py)
  * inbound routing in tg.py: reply-to routing, user_id allowlist, update_id dedup,
    dead-session downgrade-to-broadcast, broadcast claim, outbound message_id recording.
"""
import os, json, time, tempfile, importlib.util, unittest

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
        p = json.load(open(PLUGIN_JSON))
        self.assertEqual(p["name"], "telegram-bridge")
        for k in ("description", "version", "hooks"):
            self.assertIn(k, p)

    def test_marketplace_json(self):
        m = json.load(open(MARKET_JSON))
        self.assertIn("name", m)
        self.assertIn("owner", m)
        names = [pl["name"] for pl in m["plugins"]]
        self.assertIn("telegram-bridge", names)
        for pl in m["plugins"]:
            self.assertIn("source", pl)

    def test_hooks_json(self):
        h = json.load(open(HOOKS_JSON))["hooks"]
        for ev in ("SessionStart", "Stop", "UserPromptSubmit", "Notification"):
            self.assertIn(ev, h)
            cmd = h[ev][0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", cmd)
            self.assertIn("scripts/tg.py", cmd)

    def test_skill_frontmatter(self):
        body = open(SKILL_MD).read()
        self.assertTrue(body.startswith("---"))
        fm = body.split("---", 2)[1]
        self.assertRegex(fm, r"(?m)^name:\s*telegram\s*$")
        self.assertIn("description:", fm)

    def test_tg_py_parses(self):
        import ast
        ast.parse(open(TG_PY).read())


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
        sent_map = open(os.path.join(self.tmp, "sent.map")).read()
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
        self.api.updates = [upd(3, "broadcast hi")]
        with self.tg.Lock():
            self.tg._pump(0)
            self.tg._pump(0)  # same update again (e.g. crash replay)
        items = self.tg.read_inbox()
        self.assertEqual(len([i for i in items if i["uid"] == 3]), 1)

    def test_broadcast_claimable_by_any(self):
        self.api.updates = [upd(4, "to anyone")]
        self.assertEqual(self._recv_as("whoever"), "to anyone")

    def test_dead_session_downgrade(self):
        # message routed to a session that never claims; after ROUTED_TTL (but before
        # INBOX_TTL) it is downgraded to broadcast so a live session can pick it up
        old_ts = int(time.time()) - (self.tg.ROUTED_TTL + 60)
        self.tg.write_inbox([{"to": "sessGhost", "text": "orphan",
                              "ts": old_ts, "uid": 7}])
        with self.tg.Lock():
            self.tg._pump(0)  # prune/downgrade runs inside pump
        items = self.tg.read_inbox()
        self.assertEqual(items[0]["to"], "*")
        self.assertEqual(self._recv_as("freshSession"), "orphan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
