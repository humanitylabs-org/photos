"""Monthly print review (app switch print-review, humanitylabs-org/photos#2): off changes nothing; on, Photos suggests a
small shortlist from last month, the owner adjusts and saves it with a note; empty library explained; nothing orders.
Run: python3 -m unittest discover -s tests"""
import json
import os
import unittest

from test_front import ROOT, load

OWNER = {"host": "127.0.0.1:8871", "content-type": "application/json"}


def uid(n):
    return "00000000-0000-4000-8000-%012d" % n


def asset(n, taken, name=None, fav=False, people=(), make="Pixel", w=4000, h=3000, kind="IMAGE"):
    return {"id": uid(n), "type": kind, "originalFileName": name or "PXL_%d.jpg" % n, "localDateTime": taken,
            "fileCreatedAt": taken, "isFavorite": fav, "isTrashed": False, "visibility": "timeline",
            "people": [{"name": p} for p in people],
            "exifInfo": {"make": make, "exifImageWidth": w, "exifImageHeight": h}}


class FakeImmich:
    def __init__(self, assets):
        self.assets, self.calls = assets, []

    def api(self, method, path, payload=None, timeout=60):
        self.calls.append((method, path, payload))
        if path == "/assets/statistics":
            return {"images": len(self.assets), "videos": 0, "total": len(self.assets)}
        if path == "/search/metadata":
            a, b = payload["takenAfter"], payload["takenBefore"]
            items = [x for x in self.assets if a <= x["fileCreatedAt"] <= b and x["type"] == payload.get("type", x["type"])]
            return {"assets": {"items": items, "nextPage": None}}
        raise AssertionError("unexpected Immich call %s %s" % (method, path))


class PrintReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def setUp(self):
        m = self.m
        m._write_json("agent-key.json", {"secret": "k"})
        for f in ("switches.json", "print-review.json"):
            try:
                os.remove(m._state_path(f))
            except OSError:
                pass
        october = [asset(i, "2026-10-%02dT10:00:00.000Z" % (1 + i % 28)) for i in range(1, 41)]
        october += [asset(50, "2026-10-05T12:00:00.000Z", fav=True, people=("Ana",)),
                    asset(51, "2026-10-05T12:00:30.000Z", fav=True, people=("Ana",)),  # same burst
                    asset(52, "2026-10-06T09:00:00.000Z", name="Screenshot_20261006.png", make=None, fav=True),
                    asset(53, "2026-10-07T09:00:00.000Z", kind="VIDEO", fav=True),
                    asset(54, "2026-10-08T09:00:00.000Z", people=("Ana", "Leo"))]
        self.fake = FakeImmich(october + [asset(99, "2026-09-30T23:00:00.000Z")])
        self.old = m._api
        m._api = self.fake.api

    def tearDown(self):
        self.m._api = self.old

    def on(self, value=True):
        self.m._write_json("switches.json", {"print-review": value})

    def page(self, method, sub, query="", body=b""):
        return self.m.page_route(method, "/photos/" + sub, query, OWNER, body)

    def test_switch_declared_off_by_default_and_linked(self):
        sw = next(x for x in self.m.switches_doc()["switches"] if x["id"] == "print-review")
        self.assertFalse(sw["on"])
        self.assertFalse(sw["default"])
        self.assertEqual(sw["issues"], ["humanitylabs-org/photos#2"])

    def test_off_changes_nothing(self):
        m = self.m
        for sub in ("api/print-review", "api/print-review/thumb"):
            self.assertEqual(self.page("GET", sub, "id=" + uid(1))[0], 404)
        self.assertEqual(self.page("POST", "api/print-review", "", json.dumps({"month": "2026-10", "assets": [uid(1)]}).encode())[0], 404)
        names = [t["name"] for t in m.rpc({"method": "tools/list", "id": 1})["result"]["tools"]]
        self.assertNotIn("photos_print_review", names)
        self.assertIn("error", m.rpc({"method": "tools/call", "id": 2, "params": {"name": "photos_print_review"}}))
        self.assertFalse(os.path.exists(m._state_path("print-review.json")))
        self.assertEqual(self.fake.calls, [])
        old_immich = m._immich
        m._immich = lambda *a, **k: (0, {"error": "down"})
        try:
            self.assertNotIn("print_review", m.status())
            self.on()
            self.assertTrue(m.status().get("print_review"))
        finally:
            m._immich = old_immich
        # the page hides the entry until status says it is on
        html = (ROOT / "web" / "index.html").read_text()
        self.assertIn('id="open-prints" class="wz-chip" type="button" hidden', html)
        self.assertIn("el('open-prints').hidden = !s.print_review", (ROOT / "web" / "app.js").read_text())

    def test_shortlist_is_small_printable_spread_and_from_the_month(self):
        self.on()
        d = self.m.print_review("2026-10")
        picked = [c["id"] for c in d["candidates"] if c["selected"]]
        self.assertEqual(len(picked), 8)
        self.assertLessEqual(len(d["candidates"]), 24)
        ids = {c["id"] for c in d["candidates"]}
        self.assertNotIn(uid(52), ids)  # screenshot
        self.assertNotIn(uid(53), ids)  # video
        self.assertNotIn(uid(99), ids)  # September
        self.assertIn(uid(50), picked)  # favourite with a named person
        self.assertIn(uid(54), picked)  # named people
        self.assertNotIn(uid(51), picked)  # same burst as 50
        days = {c["taken"][:10] for c in d["candidates"] if c["selected"]}
        self.assertEqual(len(days), 8)  # spread over different days
        self.assertGreaterEqual(int(max(days)[8:]) - int(min(days)[8:]), 20)  # over the month, not its first week
        self.assertIsNone(d["saved"])
        self.assertIn("never orders", d["never_orders"])

    def test_owner_adjusts_and_saves_with_note_through_the_page(self):
        self.on()
        d = json.loads(self.page("GET", "api/print-review", "month=2026-10")[2])
        picked = [c["id"] for c in d["candidates"] if c["selected"]]
        extra = next(c["id"] for c in d["candidates"] if not c["selected"])
        chosen = picked[1:] + [extra]
        code, _, out = self.page("POST", "api/print-review", "", json.dumps(
            {"month": "2026-10", "assets": chosen, "note": "  Ana's   first beach day  "}).encode())[:3]
        self.assertEqual(code, 200, out)
        out = json.loads(out)
        self.assertEqual(out["saved"]["count"], 8)
        self.assertEqual(out["saved"]["note"], "Ana's first beach day")
        self.assertEqual({c["id"] for c in out["candidates"] if c["selected"]}, set(chosen))
        again = self.m.print_review("2026-10")
        self.assertEqual({c["id"] for c in again["candidates"] if c["selected"]}, set(chosen))
        self.assertEqual(again["saved_months"][0]["month"], "2026-10")
        tool = self.m.rpc({"method": "tools/call", "id": 3, "params": {"name": "photos_print_review",
                                                                     "arguments": {"month": "2026-10"}}})
        res = json.loads(tool["result"]["content"][0]["text"])
        self.assertTrue(res["approved_by_owner"])
        self.assertEqual(len(res["photos"]), 8)

    def test_save_refuses_photos_from_other_months_bad_ids_and_long_notes(self):
        self.on()
        m = self.m
        for ids, note in (([uid(99)], ""), (["nope"], ""), ([], ""), ([uid(1)], "x" * 281), ([uid(i) for i in range(1, 32)], "")):
            with self.assertRaises(ValueError):
                m.print_review_save("2026-10", ids, note)
        with self.assertRaises(ValueError):
            m.print_review_save("2026-13", [uid(1)])
        self.assertFalse(os.path.exists(m._state_path("print-review.json")))

    def test_empty_library_explains_backup_or_import(self):
        self.on()
        self.fake.assets = []
        d = self.m.print_review("")
        self.assertTrue(d["library_empty"])
        self.assertEqual(d["candidates"], [])
        self.assertIn("Phone backup", d["next"])
        self.assertIn("Import from Google", d["next"])
        self.assertEqual(d["month"], self.m._prev_month())

    def test_previous_month(self):
        self.assertEqual(self.m._prev_month(1767225600), "2025-12")  # 1 Jan 2026
        self.assertEqual(self.m._prev_month(1795000000), "2026-10")  # Nov 2026

    def test_nothing_can_order_or_pay(self):
        src = (ROOT / "mcp" / "tools.py").read_text()
        block = src[src.index("# ---- monthly print review"):src.index("# ---- end monthly print review")]
        for word in ("urlopen", "checkout", "stripe", "payment_method", "smtp"):
            self.assertNotIn(word, block.lower())
        names = list(self.m.TOOLS)
        self.assertFalse([n for n in names if "order" in n or "buy" in n or "purchase" in n])


if __name__ == "__main__":
    unittest.main()
