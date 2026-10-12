import tempfile
import unittest

from nextclaw.memory import MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="nc-m-")
        self.mem = MemoryStore(self.data)

    def test_remember_recall_forget(self):
        eid = self.mem.remember("Muse likes reliable agents", tags=["pref"], source="test")
        hits = self.mem.recall("reliable agents")
        self.assertTrue(any(h.id == eid for h in hits))
        self.assertTrue(self.mem.update(eid, tags=["pref", "updated"]))
        self.assertIn("updated", self.mem.get(eid).tags)
        self.assertTrue(self.mem.forget(eid))
        self.assertIsNone(self.mem.get(eid))

    def test_provenance_recorded(self):
        eid = self.mem.remember("fact", source="unit-test")
        self.assertEqual(self.mem.get(eid).source, "unit-test")


if __name__ == "__main__":
    unittest.main()
