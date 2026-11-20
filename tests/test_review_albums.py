"""Review albums (switch review-albums, humanitylabs-org/photos#6): off by default and hidden from the agent, fill an
album from ids or a search, trash exactly what is left after a count-and-confirm, only albums this tool made.
Run: python3 -m unittest discover -s tests"""
import json
import unittest

from test_front import load

A = ["0b0c9e3e-6b49-4a8c-9a37-4b1f0d6d2f1%d" % i for i in range(4)]
OWN_ALBUM = "11111111-2222-4333-8444-555555555555"


class FakeImmich:
    def __init__(self):
        self.albums = {OWN_ALBUM: {"name": "Wedding", "assets": list(A)}}
        self.calls = []

    def api(self, method, path, payload=None, timeout=60):
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/albums":
            return [{"id": k, "albumName": v["name"]} for k, v in self.albums.items()]
        if method == "POST" and path == "/albums":
            aid = "aaaaaaaa-bbbb-4ccc-8ddd-%012d" % len(self.albums)
            self.albums[aid] = {"name": payload["albumName"], "assets": []}
            return {"id": aid}
        if method == "PUT" and path.endswith("/assets"):
            al = self.albums[path.split("/")[2]]["assets"]
            out = []
            for i in payload["ids"]:
                out.append({"id": i, "success": i not in al})
                if i not in al:
                    al.append(i)
            return out
        if method == "POST" and path == "/search/metadata":
            if "albumIds" in payload:
                items = self.albums.get(payload["albumIds"][0], {"assets": []})["assets"]
            else:
                items = A[:3]
            return {"assets": {"items": [{"id": i} for i in items[:payload.get("size", 1000)]], "nextPage": None}}
        if method == "POST" and path == "/search/smart":
            return {"assets": {"items": [{"id": A[1]}, {"id": A[2]}]}}
        if method == "DELETE" and path == "/assets":
            return {}
        if method == "DELETE" and path.startswith("/albums/"):
            self.albums.pop(path.split("/")[2])
            return {}
        raise AssertionError((method, path))


class ReviewAlbums(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def setUp(self):
        m = self.m
        self.fake = FakeImmich()
        self.saved = m._api, m._immich
        m._api = self.fake.api
        m._immich = lambda *a, **k: (200, {"trashDays": 30})
        m._write_json("review-albums.json", {})
        m._write_json("switches.json", {"review-albums": True})

    def tearDown(self):
        self.m._api, self.m._immich = self.saved
        self.m._write_json("switches.json", {})

    def tools_listed(self):
        return [t["name"] for t in self.m.rpc({"method": "tools/list", "id": 1})["result"]["tools"]]

    def test_switch_off_by_default_hides_tools(self):
        m = self.m
        m._write_json("switches.json", {})
        sw = [x for x in m.switches_doc()["switches"] if x["id"] == "review-albums"][0]
        self.assertFalse(sw["on"])
        self.assertEqual(sw["issues"], ["humanitylabs-org/photos#6"])
        self.assertNotIn("photos_review_album", self.tools_listed())
        self.assertNotIn("photos_trash_album", self.tools_listed())
        r = m.rpc({"method": "tools/call", "id": 2, "params": {"name": "photos_trash_album", "arguments": {}}})
        self.assertIn("error", r)
        with self.assertRaises(RuntimeError):
            m.review_album("Screenshots", asset_ids=A[:1])
        self.assertEqual(self.fake.calls, [])
        m._write_json("switches.json", {"review-albums": True})
        self.assertIn("photos_review_album", self.tools_listed())
        self.assertIn("photos_trash_album", self.tools_listed())

    def test_fill_then_refill_then_trash_the_rest(self):
        m = self.m
        out = m.review_album("Screenshots", asset_ids=A[:3])
        self.assertTrue(out["created"])
        self.assertEqual(out["album"], "Cleanup \u00b7 Screenshots")
        self.assertEqual((out["added"], out["in_album_now"]), (3, 3))
        aid = out["album_id"]
        again = m.review_album("Cleanup \u00b7 Screenshots", query="screenshot")
        self.assertFalse(again["created"])
        self.assertEqual(again["album_id"], aid)
        self.assertEqual((again["added"], again["already_in_album"]), (0, 2))
        self.fake.albums[aid]["assets"].remove(A[0])  # the owner keeps one
        no_trash = [c for c in self.fake.calls if c[:2] == ("DELETE", "/assets")]
        self.assertEqual(no_trash, [])
        prev = m.trash_album(name="Screenshots")
        self.assertTrue(prev["preview"])
        self.assertEqual(prev["would_move_to_trash"], 2)
        with self.assertRaises(ValueError):
            m.trash_album(name="Screenshots", confirmed=True, expected_count=3)
        with self.assertRaises(ValueError):
            m.trash_album(name="Screenshots", confirmed=True)
        self.assertEqual([c for c in self.fake.calls if c[0] == "DELETE"], [])
        done = m.trash_album(album_id=aid, confirmed=True, expected_count=2)
        self.assertEqual(done["moved_to_trash"], 2)
        deletes = [c for c in self.fake.calls if c[0] == "DELETE"]
        self.assertEqual(deletes, [("DELETE", "/assets", {"ids": [A[1], A[2]], "force": False}),
                                   ("DELETE", "/albums/%s" % aid, None)])
        self.assertNotIn(aid, m._review_albums())

    def test_never_trashes_owners_own_album_or_whole_library(self):
        m = self.m
        with self.assertRaises(ValueError):
            m.trash_album(album_id=OWN_ALBUM)
        with self.assertRaises(ValueError):
            m.trash_album(name="Wedding")
        with self.assertRaises(ValueError):
            m.review_album("Everything")
        with self.assertRaises(ValueError):
            m.review_album("Bad", asset_ids=["nope"])
        with self.assertRaises(ValueError):
            m.review_album("", asset_ids=A[:1])
        self.assertEqual([c for c in self.fake.calls if c[0] in ("DELETE", "PUT")], [])

    def test_agent_key_gets_album_scopes_and_old_key_is_remade(self):
        m = self.m
        self.assertIn("album.delete", m.KEY_PERMISSIONS)
        self.assertIn("albumAsset.delete", m.KEY_PERMISSIONS)
        m._write_json("agent-key.json", {"id": "old", "secret": "OLD"})
        made = []

        def imm(method, path, payload=None, **k):
            made.append((method, path))
            if path == "/users/me":
                return 200, {}
            if method == "POST" and path == "/api-keys":
                return 201, {"secret": "NEW", "apiKey": {"id": "new"}}
            return 200, {}
        m._immich, m.AUTH["token"] = imm, "t"
        try:
            self.assertTrue(m._ensure_agent_key())
            self.assertEqual(m._key(), "NEW")
            self.assertIn(("DELETE", "/api-keys/old"), made)
            made.clear()
            self.assertTrue(m._ensure_agent_key())  # current key: kept
            self.assertEqual(made, [("GET", "/users/me")])
        finally:
            m.AUTH["token"] = ""
            m._write_json("agent-key.json", {})


if __name__ == "__main__":
    unittest.main()
