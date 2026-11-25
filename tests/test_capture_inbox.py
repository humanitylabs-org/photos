"""Capture inbox (wizard-os issue 696, the box switch wizard/capture-inbox): new camera-roll photos become one batch
for the owner's agent after a quiet period; the agent tags, albums and marks them processed with logged tools, and the
owner's "undo that" on the Agent Service makes Photos take exactly that away again. Switch off: nothing changes.
Run: python3 -m unittest discover -s tests"""
import os
import unittest
import uuid

from test_front import load


def aid():
    return str(uuid.uuid4())


class FakeImmich:
    """The few Immich v3.4 endpoints the capture inbox uses, with the same shapes."""

    def __init__(self):
        self.assets, self.tags, self.albums, self.calls = {}, {}, {}, []
        self.race, self.stale = False, {}   # race: Immich reads a stale sidecar back once after a tag change

    def _snap(self, i):
        if self.race and i not in self.stale:
            self.stale[i] = {t["value"] for t in self.tags.values() if i in t["assets"]}

    def add(self, created, taken=None, trashed=False, name="IMG.jpg"):
        i = aid()
        self.assets[i] = {"id": i, "createdAt": created, "fileCreatedAt": taken or created, "isTrashed": trashed,
                          "originalFileName": name, "type": "IMAGE", "originalPath": "/data/upload/u1/ab/%s.jpg" % i,
                          "tags": set()}
        return i

    def api(self, method, path, payload=None, timeout=60):
        self.calls.append((method, path))
        if path == "/search/metadata" and not payload.get("tagIds"):
            items = sorted((a for a in self.assets.values() if a["createdAt"] > payload["createdAfter"]),
                           key=lambda a: a["createdAt"])
            return {"assets": {"items": [dict(a, tags=None) for a in items], "nextPage": None}}
        if path.startswith("/assets/"):
            i = path.split("/")[2]
            if i in self.stale:   # the sidecar read-back puts the asset's tags back as they were
                old = self.stale.pop(i)
                for t in self.tags.values():
                    (t["assets"].add if t["value"] in old else t["assets"].discard)(i)
                self.race = False
            return dict(self.assets[i], tags=[{"id": t["id"], "value": t["value"]} for t in self.tags.values() if i in t["assets"]])
        if path == "/search/metadata" and payload.get("tagIds"):
            t = next(t for t in self.tags.values() if t["id"] == payload["tagIds"][0])
            return {"assets": {"items": [{"id": i} for i in t["assets"]], "nextPage": None}}
        if path == "/tags" and method == "GET":
            return [{k: t[k] for k in ("id", "name", "value")} for t in self.tags.values()]
        if path == "/tags/assets" and method == "PUT":
            for i in payload["assetIds"]:
                self._snap(i)
            for t in self.tags.values():
                if t["id"] in payload["tagIds"]:
                    t["assets"].update(payload["assetIds"])
            return {"count": len(payload["tagIds"])}
        if path.startswith("/tags/") and path.count("/") == 2 and method == "DELETE":
            self.tags = {k: v for k, v in self.tags.items() if v["id"] != path.split("/")[2]}
            return {}
        if path == "/tags" and method == "PUT":
            out = []
            for n in payload["tags"]:
                parts = n.split("/")
                for depth in range(1, len(parts) + 1):  # Immich makes the parents of a hierarchical tag too
                    v = "/".join(parts[:depth])
                    self.tags.setdefault(v, {"id": aid(), "name": parts[depth - 1], "value": v, "assets": set()})
                t = self.tags[n]
                out.append({k: t[k] for k in ("id", "name", "value")})
            return out
        if path.startswith("/tags/"):
            t = next(t for t in self.tags.values() if t["id"] == path.split("/")[2])
            res = []
            for i in payload["ids"]:
                self._snap(i)
                if method == "PUT":
                    res.append({"id": i, "success": i not in t["assets"], **({} if i not in t["assets"] else {"error": "duplicate"})})
                    t["assets"].add(i)
                else:
                    res.append({"id": i, "success": i in t["assets"]})
                    t["assets"].discard(i)
            return res
        if path == "/albums" and method == "GET":
            return [{"id": k, "albumName": v["name"], "assetCount": len(v["assets"])} for k, v in self.albums.items()]
        if path == "/albums" and method == "POST":
            k = aid()
            self.albums[k] = {"name": payload["albumName"], "assets": set(payload.get("assetIds") or [])}
            return {"id": k, "albumName": payload["albumName"]}
        if path.startswith("/albums/"):
            k = path.split("/")[2]
            if path.endswith("/assets"):
                al, res = self.albums[k], []
                for i in payload["ids"]:
                    has = i in al["assets"]
                    if method == "PUT":
                        res.append({"id": i, "success": not has})
                        al["assets"].add(i)
                    else:
                        res.append({"id": i, "success": has})
                        al["assets"].discard(i)
                return res
            if method == "DELETE":
                self.albums.pop(k)
                return {}
            return {"id": k, "albumName": self.albums[k]["name"], "assetCount": len(self.albums[k]["assets"])}
        raise AssertionError("unexpected Immich call %s %s" % (method, path))

    def tagged(self, i):
        if i in self.stale:   # a read-back still due happens by now
            self.api("GET", "/assets/%s" % i)
        return sorted(t["value"] for t in self.tags.values() if i in t["assets"])


class FakeService:
    def __init__(self):
        self.on, self.events, self.undone, self.fail = False, [], set(), 0

    def call(self, method, payload=None):
        if not self.on:
            return 404, {"error": "not_found"}
        if method == "GET":
            return 200, {"on": True, "app": "photos", "batches": [
                {"batch": "cap_x", "app_batch": b, "state": "undone", "undone_at": "2026-11-25T10:00:00Z"} for b in self.undone]}
        if self.fail:
            self.fail -= 1
            return 503, {"error": "busy"}
        self.events.append(payload)
        return 202, {"accepted": True, "created": True, "batch": "cap_%016x" % len(self.events)}


class CaptureInbox(unittest.TestCase):
    def setUp(self):
        self.m = m = load()
        self.im, self.sv = FakeImmich(), FakeService()
        m._api, m._capture_service = self.im.api, self.sv.call
        m._key = lambda: "key"
        m._ensure_agent_key = lambda force=False: True
        m.CAPTURE_QUIET_S = 120
        m.CAPTURE_SETTLE_S = 0
        self.clock = [1_800_000_000.0]
        real = m.time.time
        m.time.time = lambda: self.clock[0]
        self.addCleanup(setattr, m.time, "time", real)
        os.environ["PHOTOS_AGENT_INBOX_TOKEN"] = "photos-app-token-0123456789abcdefghijkl"
        self.addCleanup(os.environ.pop, "PHOTOS_AGENT_INBOX_TOKEN", None)

    def tools(self):
        return [t["name"] for t in self.m.rpc({"method": "tools/list", "id": 1})["result"]["tools"]]

    def tick(self, seconds=0):
        self.clock[0] += seconds
        self.m.capture_tick()

    def test_switch_off_or_no_token_nothing_changes(self):
        self.im.add("2026-11-25T09:00:00.000Z")
        self.tick()
        self.tick(600)
        self.assertEqual(self.im.calls, [])
        self.assertEqual(self.sv.events, [])
        self.assertNotIn("photos_capture_batch", self.tools())
        self.assertFalse(os.path.exists(self.m._state_path("capture.json")))
        os.environ.pop("PHOTOS_AGENT_INBOX_TOKEN")
        self.sv.on = True
        self.tick(600)
        self.assertEqual(self.sv.events, [])
        self.assertNotIn("photos_capture_file", self.tools())

    def test_three_book_pages_one_batch_after_quiet_then_tags_album_processed_and_undo(self):
        old = self.im.add("2020-01-01T00:00:00.000Z")
        self.sv.on = True
        self.tick()  # switched on: baseline
        self.assertIn("photos_capture_batch", self.tools())
        self.assertIn("photos_capture_file", self.tools())
        pages = [self.im.add(self.m._iso_now(n + 1), name="IMG_%d.jpg" % n) for n in range(3)]
        first_backup = self.im.add(self.m._iso_now(9), taken="2019-05-05T10:00:00.000Z")  # an old camera-roll photo
        self.tick(30)
        self.assertEqual(self.sv.events, [])  # quiet period
        self.sv.fail = 1
        self.tick(130)  # due, but the Service is busy: kept
        self.assertEqual(self.sv.events, [])
        self.tick(30)
        self.assertEqual(len(self.sv.events), 1)
        ev = self.sv.events[0]
        self.assertEqual(ev["event"], "items_arrived")
        self.assertEqual({k: ev["data"][k] for k in ("source", "count")}, {"source": "photos", "count": 3})
        batch = ev["data"]["batch"]
        self.tick(300)
        self.assertEqual(len(self.sv.events), 1)  # never twice

        listed = self.m.capture_batch(batch)
        self.assertEqual([i["asset_id"] for i in listed["items"]], pages)
        self.assertNotIn(first_backup, [i["asset_id"] for i in listed["items"]])
        self.assertNotIn(old, [i["asset_id"] for i in listed["items"]])
        self.assertEqual(listed["items"][0]["files_path"], "Photos/Library/upload/u1/ab/%s.jpg" % pages[0])
        for p in pages:
            r = self.m.capture_file(batch, p, "book page", "Photos of book pages", ["Why We Sleep"], "Reading")
            self.assertTrue(r["processed"])
        self.assertEqual(self.im.tagged(pages[0]), ["Why We Sleep", "Wizard/Processed"])
        self.assertEqual([a["name"] for a in self.im.albums.values()], ["Reading"])
        self.assertEqual(self.m.capture_batch(batch)["items"][2]["state"], "processed")
        with self.assertRaises(ValueError):
            self.m.capture_file(batch, old)

        # "Undo that" on the Service: Photos takes away exactly what it added.
        self.sv.undone.add(batch)
        self.tick(30)
        for p in pages:
            self.assertEqual(self.im.tagged(p), [])
        self.assertEqual(self.im.albums, {})
        self.assertEqual(self.im.tags, {})  # the tags this batch made are gone too
        self.assertTrue(self.m._capture_state()["batches"][batch]["reverted"])

    def test_an_owner_album_and_tag_he_had_already_stay_after_undo(self):
        self.sv.on = True
        self.tick()
        p = self.im.add(self.m._iso_now(1))
        self.im.albums["mine"] = {"name": "Tokyo", "assets": set()}
        self.im.api("PUT", "/tags", {"tags": ["Tokyo"]})
        tok = next(t for t in self.im.tags.values() if t["value"] == "Tokyo")
        tok["assets"].add(p)
        self.tick(200)
        self.tick(200)
        batch = self.sv.events[0]["data"]["batch"]
        self.m.capture_file(batch, p, "personal photo", "Personal photos", ["Tokyo", "Ana"], "Tokyo")
        self.assertEqual(self.im.tagged(p), ["Ana", "Tokyo", "Wizard/Processed"])
        self.sv.undone.add(batch)
        self.tick(30)
        self.assertEqual(self.im.tagged(p), ["Tokyo"])
        self.assertEqual(sorted(self.im.tags), ["Tokyo"])
        self.assertEqual(self.im.albums, {"mine": {"name": "Tokyo", "assets": set()}})

    def test_tags_hold_when_immich_reads_a_stale_sidecar_back(self):
        self.sv.on = True
        self.tick()
        p = self.im.add(self.m._iso_now(1))
        self.tick(200)
        self.tick(200)
        batch = self.sv.events[0]["data"]["batch"]
        self.im.race = True
        self.m.capture_file(batch, p, "personal photo", "Personal photos", ["Ana"], "")
        self.assertEqual(self.im.tagged(p), ["Ana", "Wizard/Processed"])
        self.im.race = True
        self.sv.undone.add(batch)
        self.tick(30)
        self.assertEqual(self.im.tagged(p), [])
        self.assertTrue(self.m._capture_state()["batches"][batch]["reverted"])

    def test_switch_off_again_takes_a_new_baseline_and_hides_the_tools(self):
        self.sv.on = True
        self.tick()
        self.sv.on = False
        self.tick(30)
        self.assertNotIn("photos_capture_batch", self.tools())
        self.im.add(self.m._iso_now(10))   # arrives while the switch is off
        self.clock[0] += 60
        self.sv.on = True
        self.tick(30)  # a new baseline: the photo from while it was off is never handed over
        self.tick(300)
        self.tick(300)
        self.assertEqual(self.sv.events, [])


if __name__ == "__main__":
    unittest.main()
