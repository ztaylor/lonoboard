import tempfile
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401
from lonoboard.usage import Usage


def ev(name, at, tokens=100, tps=50.0, source="log"):
    return {"at": at, "name": name, "tokens": tokens, "gen_tps": tps, "prompt_tokens": 10, "prompt_tps": 500.0, "source": source}


class TestUsage(unittest.TestCase):
    def test_dedupes_and_counts(self):
        u = Usage(None)
        now = int(time.time())
        self.assertTrue(u.add_event(ev("a", now)))
        self.assertFalse(u.add_event(ev("a", now)))
        u.add_event(ev("b", now - 5, tokens=300, tps=100.0))
        self.assertEqual(u.today(), 2)
        self.assertEqual(u.last("a")["at"], now)
        s = u.summary(24)
        self.assertEqual(len(s["hours"]), 24)
        self.assertEqual(s["hours"][-1]["requests"], 2)
        self.assertEqual([t["name"] for t in s["top"]], ["b", "a"])
        self.assertEqual(s["top"][0]["avg_tps"], 100.0)

    def test_slot_events_count_but_are_not_listed(self):
        u = Usage(None)
        u.add_event(ev("a", int(time.time()), tokens=None, tps=None, source="slots"))
        self.assertEqual(u.today("a"), 1)
        self.assertEqual(u.recent(), [])

    def test_counts_without_detail(self):
        u = Usage(None)
        u.add_counts("v", time.time(), requests=3, tokens=900, gen_s=9.0)
        top = u.summary()["top"][0]
        self.assertEqual((top["requests"], top["tokens"], top["avg_tps"]), (3, 900, 100.0))

    def test_persists(self):
        path = Path(tempfile.mkdtemp()) / "usage.json"
        u = Usage(path)
        u.add_event(ev("a", int(time.time())))
        u.save()
        again = Usage(path)
        self.assertEqual(again.today(), 1)
        self.assertFalse(again.add_event(ev("a", u.events[0]["at"])))   # still deduped after a restart

    def test_old_data_is_pruned(self):
        u = Usage(None)
        u.add_event(ev("a", int(time.time()) - 3 * 86400))
        u.prune()
        self.assertEqual(len(u.events), 0)


if __name__ == "__main__":
    unittest.main()
