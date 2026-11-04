"""Core logic tests against an in-memory store. No Keychain or network access."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_switcher import api, core
from claude_switcher import storage as st

FUTURE = int(time.time() * 1000) + 3_600_000


def oauth(tag):
    return {"accessToken": f"at-{tag}", "refreshToken": f"rt-{tag}", "expiresAt": FUTURE}


def acct(uuid):
    return {"accountUuid": uuid, "emailAddress": f"{uuid}@example.com"}


def u(five, seven):
    return {"five_hour": (five, None), "seven_day": (seven, None)}


class FakeStore(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.live = {"claudeAiOauth": oauth("a"), "mcpOAuth": {"github": "keep-me"}}
        self.config = {"oauthAccount": acct("a"), "theme": "dark"}
        self.saved = {}
        self.usage_by_token = {}
        patches = [
            mock.patch.object(st, "REGISTRY", tmp / "accounts.json"),
            mock.patch.object(st, "live_creds_get", lambda: self.live),
            mock.patch.object(st, "live_creds_set", lambda d: setattr(self, "live", d)),
            mock.patch.object(st, "config_get", lambda: self.config),
            mock.patch.object(st, "config_set_oauth_account",
                              lambda a: self.config.__setitem__("oauthAccount", a)),
            mock.patch.object(st, "saved_get", lambda k: self.saved.get(k)),
            mock.patch.object(st, "saved_set", lambda k, v: self.saved.__setitem__(k, v)),
            mock.patch.object(st, "saved_delete", lambda k: self.saved.pop(k, None)),
            mock.patch.object(api, "usage", lambda tok: self.usage_by_token[tok]),
            mock.patch.object(core, "notify", lambda *a: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        core.add_current()  # account "a" is logged in
        for uid in ("b", "c"):
            self.saved[uid] = {"claudeAiOauth": oauth(uid), "oauthAccount": acct(uid)}
            reg = st.registry_get()
            reg["accounts"].append({"uuid": uid, "email": f"{uid}@example.com"})
            st.registry_set(reg)


class PickBest(unittest.TestCase):
    def test_lowest_tighter_window_wins(self):
        usages = {"a": u(95, 10), "b": u(20, 70), "c": u(40, 30), "d": None}
        self.assertEqual(core.pick_best(usages, 90, exclude="a"), "c")

    def test_none_below_threshold(self):
        self.assertIsNone(core.pick_best({"b": u(91, 0), "c": u(0, 95)}, 90, exclude="a"))


class Switch(FakeStore):
    def test_switch_writes_target_and_keeps_other_keys(self):
        core.switch_to("b")
        self.assertEqual(self.live["claudeAiOauth"]["accessToken"], "at-b")
        self.assertEqual(self.live["mcpOAuth"], {"github": "keep-me"})
        self.assertEqual(self.config["oauthAccount"]["accountUuid"], "b")
        self.assertEqual(self.config["theme"], "dark")

    def test_switch_saves_rotated_token_of_account_left(self):
        self.live["claudeAiOauth"] = oauth("a-rotated")  # Claude Code refreshed it
        core.switch_to("b")
        self.assertEqual(self.saved["a"]["claudeAiOauth"]["refreshToken"], "rt-a-rotated")

    def test_expired_target_is_refreshed_and_saved(self):
        self.saved["b"]["claudeAiOauth"]["expiresAt"] = 0
        with mock.patch.object(api, "refresh", lambda o: {**o, "accessToken": "at-new", "expiresAt": FUTURE}):
            core.switch_to("b")
        self.assertEqual(self.live["claudeAiOauth"]["accessToken"], "at-new")
        self.assertEqual(self.saved["b"]["claudeAiOauth"]["accessToken"], "at-new")

    def test_find_by_number_alias_email(self):
        reg = st.registry_get()
        reg["accounts"][2]["alias"] = "work"
        self.assertEqual(core.find(reg, "2")["uuid"], "b")
        self.assertEqual(core.find(reg, "work")["uuid"], "c")
        self.assertEqual(core.find(reg, "b@example.com")["uuid"], "b")
        with self.assertRaises(core.SwitchError):
            core.find(reg, "nobody")


class AutoTick(FakeStore):
    def setUp(self):
        super().setUp()
        self.usage_by_token = {"at-a": u(50, 10), "at-b": u(60, 60), "at-c": u(10, 20)}

    def test_under_threshold_stays(self):
        self.assertEqual(core.auto_tick(90, 300).status, "ok")
        self.assertEqual(self.config["oauthAccount"]["accountUuid"], "a")

    def test_over_threshold_switches_to_most_headroom(self):
        self.usage_by_token["at-a"] = u(93, 10)
        t = core.auto_tick(90, 300)
        self.assertEqual((t.status, t.target), ("switched", "c"))
        self.assertEqual(self.config["oauthAccount"]["accountUuid"], "c")

    def test_dry_run_does_not_switch(self):
        self.usage_by_token["at-a"] = u(93, 10)
        self.assertEqual(core.auto_tick(90, 300, dry_run=True).status, "switched")
        self.assertEqual(self.config["oauthAccount"]["accountUuid"], "a")

    def test_cooldown_blocks_second_switch(self):
        self.usage_by_token["at-a"] = u(93, 10)
        core.auto_tick(90, 300)
        self.usage_by_token["at-c"] = u(99, 20)
        self.assertEqual(core.auto_tick(90, 300).status, "cooldown")

    def test_disabled_account_skipped(self):
        self.usage_by_token["at-a"] = u(93, 10)
        reg = st.registry_get()
        reg["accounts"][2]["disabled"] = True
        st.registry_set(reg)
        self.assertEqual(core.auto_tick(90, 300).target, "b")

    def test_all_exhausted(self):
        self.usage_by_token = {"at-a": u(95, 0), "at-b": u(92, 0), "at-c": u(0, 99)}
        self.assertEqual(core.auto_tick(90, 300).status, "exhausted")


if __name__ == "__main__":
    unittest.main()
