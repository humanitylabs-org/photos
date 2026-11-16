"""Unit tests for the Photos front: routing split with Immich, owner gate, trash safety, key secrecy.
Run: python3 -m unittest discover -s tests"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    tmp = tempfile.mkdtemp()
    os.environ.update(STATE_DIR=tmp, IMPORT_DIR=tmp + "/import", LIBRARY_DIR=tmp, UPSTREAM_HOST="127.0.0.1",
                      UPSTREAM_PORT="1", WIZARD_TRUSTED_HOSTS="box.ts.net:8444,127.0.0.1:8871",
                      WIZARD_OWNER_LOGINS="owner@example.com", APP_PORT="8871")
    spec = importlib.util.spec_from_file_location("front", ROOT / "app" / "front.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Front(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def test_generated_files_current(self):
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "mcp" / "inline.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_routing_leaves_immich_paths_to_immich(self):
        p = self.m._page_path
        self.assertTrue(p("/photos/"))
        self.assertTrue(p("/photos/app.js"))
        self.assertTrue(p("/photos/api/status"))
        self.assertFalse(p("/photos"))  # Immich timeline
        self.assertFalse(p("/photos/0b0c9e3e-6b49-4a8c-9a37-4b1f0d6d2f11"))  # Immich viewer
        self.assertFalse(p("/photos/0b0c9e3e-6b49-4a8c-9a37-4b1f0d6d2f11/x"))
        self.assertFalse(p("/api/assets"))

    def test_owner_gate(self):
        t = self.m._trusted
        self.assertTrue(t({"host": "box.ts.net:8444", "tailscale-user-login": "owner@example.com"}, True))
        self.assertFalse(t({"host": "box.ts.net:8444", "tailscale-user-login": "guest@example.com"}, False))
        self.assertFalse(t({"host": "evil.example:8444"}, False))
        self.assertFalse(t({"host": "box.ts.net:8444", "sec-fetch-site": "cross-site"}, False))
        self.assertFalse(t({"host": "box.ts.net:8444", "origin": "https://evil.example"}, True))

    def test_mcp_tools_are_box_only(self):
        tok = self.m.REQUEST.set({"tailscale-user-login": "owner@example.com"})
        try:
            with self.assertRaises(PermissionError):
                self.m.photos_status()
        finally:
            self.m.REQUEST.reset(tok)

    def test_trash_refuses_whole_library_and_bad_ids(self):
        self.m._write_json("agent-key.json", {"secret": "k"})
        with self.assertRaises(ValueError):
            self.m.move_to_trash(asset_ids=["not-a-uuid"])
        with self.assertRaises(ValueError):
            self.m.move_to_trash()

    def test_trash_never_forces(self):
        calls = []
        m = self.m
        old_api, old_imm = m._api, m._immich
        m._immich = lambda *a, **k: (200, {"trashDays": 30})
        m._api = lambda method, path, payload=None, timeout=60: calls.append((method, path, payload)) or {}
        try:
            ids = ["0b0c9e3e-6b49-4a8c-9a37-4b1f0d6d2f11"]
            prev = m.move_to_trash(asset_ids=ids)
            self.assertTrue(prev["preview"])
            self.assertEqual(calls, [])
            with self.assertRaises(ValueError):
                m.move_to_trash(asset_ids=ids, confirmed=True, expected_count=2)
            m.move_to_trash(asset_ids=ids, confirmed=True, expected_count=1)
            self.assertEqual(calls, [("DELETE", "/assets", {"ids": ids, "force": False})])
        finally:
            m._api, m._immich = old_api, old_imm

    def test_key_never_in_status_or_routes(self):
        self.m._write_json("agent-key.json", {"secret": "SECRET-KEY-123"})
        out = json.dumps(self.m.status())
        self.assertNotIn("SECRET-KEY-123", out)
        code, ctype, body = self.m.page_route("GET", "/photos/api/import", "", {"host": "127.0.0.1:8871"}, b"")[:3]
        self.assertNotIn(b"SECRET-KEY-123", body)

    def test_connect_needs_immich_session(self):
        code, body = self.m.connect({"host": "box.ts.net:8444"})
        self.assertEqual(code, 401)

    def test_page_served_with_csp(self):
        out = self.m.page_route("GET", "/photos/", "", {"host": "box.ts.net:8444"}, b"")
        self.assertEqual(out[0], 200)
        self.assertIn("frame-ancestors 'self'", out[3]["Content-Security-Policy"])

    def test_compose_pins_every_image(self):
        import re
        text = (ROOT / "runtipi" / "docker-compose.yml").read_text()
        images = re.findall(r"^\s+image:\s*(\S+)", text, re.M)
        self.assertEqual(len(images), 5)
        for i in images:
            self.assertIn("@sha256:", i)


if __name__ == "__main__":
    unittest.main()
