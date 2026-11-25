"""Review albums (switch review-albums, humanitylabs-org/photos#6) were dropped (#14): the tools, the switch, the
extra Immich scopes and their stored values are gone; the plain trash tools stay.
Run: python3 -m unittest discover -s tests"""
import os
import unittest

from test_front import load


class ReviewAlbumsRemoved(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_tools_and_switch_gone(self):
        m = self.m
        names = [t["name"] for t in m.rpc({"method": "tools/list", "id": 1})["result"]["tools"]]
        self.assertNotIn("photos_review_album", names)
        self.assertNotIn("photos_trash_album", names)
        self.assertIn("photos_move_to_trash", names)
        r = m.rpc({"method": "tools/call", "id": 2, "params": {"name": "photos_trash_album", "arguments": {}}})
        self.assertIn("error", r)
        self.assertNotIn("review-albums", [x["id"] for x in m.switches_doc()["switches"]])
        self.assertFalse(hasattr(m, "review_album") or hasattr(m, "trash_album"))

    def test_album_delete_scopes_dropped(self):
        self.assertNotIn("album.delete", self.m.KEY_PERMISSIONS)
        self.assertNotIn("albumAsset.delete", self.m.KEY_PERMISSIONS)

    def test_stored_values_forgotten_at_start(self):
        m = self.m
        m._write_json("review-albums.json", {"00000000-0000-4000-8000-000000000001": {"name": "Cleanup \u00b7 X"}})
        m._write_json("switches.json", {"review-albums": True})
        self.assertIn(m._drop_removed_switch_state, m.STARTUP)
        m._drop_removed_switch_state()
        self.assertFalse(os.path.exists(m._state_path("review-albums.json")))
        self.assertEqual(m._read_json("switches.json"), {})
        m._drop_removed_switch_state()  # idempotent


if __name__ == "__main__":
    unittest.main()
