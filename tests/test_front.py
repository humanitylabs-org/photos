"""Unit tests for the Photos front: routing split with Immich, owner gate, no-sign-in proxy, trash safety, secrecy.
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

    def test_owner_request_gate(self):
        o = self.m._owner_request
        self.assertTrue(o("GET", {"host": "box.ts.net:8871", "tailscale-user-login": "owner@example.com"}))
        self.assertTrue(o("POST", {"host": "box.ts.net:8871"}))  # the phone app: no browser headers
        self.assertFalse(o("GET", {"host": "box.ts.net:8871", "tailscale-user-login": "guest@example.com"}))
        self.assertFalse(o("GET", {"host": "evil.example:8871"}))  # DNS rebinding
        self.assertFalse(o("POST", {"host": "box.ts.net:8871", "sec-fetch-site": "cross-site"}))
        self.assertFalse(o("POST", {"host": "box.ts.net:8871", "origin": "https://evil.example"}))
        self.assertFalse(o("GET", {"host": "box.ts.net:8871", "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}))
        self.assertTrue(o("GET", {"host": "box.ts.net:8871", "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}))

    def test_session_added_only_without_own_credentials(self):
        m = self.m
        m.AUTH["token"] = "SESSION-TOKEN-XYZ"
        try:
            lines = ["GET /api/users/me HTTP/1.1", "Host: box.ts.net:8871"]
            out = m._add_session("GET", "/api/users/me", {"host": "box.ts.net:8871"}, list(lines))
            self.assertIn("x-immich-user-token: SESSION-TOKEN-XYZ", out)
            own = {"host": "box.ts.net:8871", "x-immich-user-token": "phone"}
            self.assertEqual(m._add_session("GET", "/api/users/me", own, list(lines)), lines)
            guest = {"host": "box.ts.net:8871", "tailscale-user-login": "guest@example.com"}
            self.assertEqual(m._add_session("GET", "/api/users/me", guest, list(lines)), lines)
            self.assertEqual(m._add_session("GET", "/api/assets?apiKey=x", {"host": "box.ts.net:8871"}, list(lines)), lines)
        finally:
            m.AUTH["token"] = ""

    def test_intercepts(self):
        m = self.m
        h = {"host": "box.ts.net:8871"}
        self.assertIs(m._intercept("POST", "/api/auth/login", h), m.auth_route)
        self.assertIs(m._intercept("POST", "/api/auth/logout", h), m.auth_route)
        self.assertIs(m._intercept("GET", "/api/server/features", h), m.phone_config_route)
        self.assertIsNone(m._intercept("GET", "/api/server/features", dict(h, **{"sec-fetch-site": "same-origin"})))
        self.assertIsNone(m._intercept("POST", "/api/auth/login", {"host": "box.ts.net:8871", "tailscale-user-login": "guest@example.com"}))
        self.assertIsNone(m._intercept("GET", "/api/assets", h))
        nav = dict(h, **{"sec-fetch-dest": "document", "accept": "text/html"})
        self.assertIsNone(m._intercept("GET", "/photos", nav))  # not set up yet: no cookie promised
        m.AUTH["token"] = "t"
        try:
            self.assertIs(m._intercept("GET", "/photos", nav), m.page_with_cookie_route)
            self.assertIsNone(m._intercept("GET", "/photos", dict(nav, cookie="immich_is_authenticated=true")))
        finally:
            m.AUTH["token"] = ""

    def test_phone_continue_flow_checks_pkce_and_is_single_use(self):
        m = self.m
        old = m._login_upstream
        m._login_upstream = lambda ua="": (201, {"accessToken": "PHONE-SESSION", "userId": "u", "isAdmin": True})
        try:
            h = {"host": "box.ts.net:8871", "content-type": "application/json"}
            verifier = "v" * 50
            body = json.dumps({"redirectUri": "app.immich:///oauth-callback", "state": "S1",
                               "codeChallenge": m._b64url_sha256(verifier)}).encode()
            code, _, out = m.auth_route("POST", "/api/oauth/authorize", "", h, body)[:3]
            self.assertEqual(code, 201)
            url = json.loads(out)["url"]
            self.assertTrue(url.startswith("https://box.ts.net:8871/wizard-sign-in?r="))
            r = m.auth_route("GET", "/wizard-sign-in", url.split("?", 1)[1], h, b"")
            self.assertEqual(r[0], 302)
            loc = r[3]["Location"]
            self.assertTrue(loc.startswith("app.immich:///oauth-callback?code="))
            bad = json.dumps({"url": loc, "state": "S1", "codeVerifier": "wrong"}).encode()
            self.assertEqual(m.auth_route("POST", "/api/oauth/callback", "", h, bad)[0], 401)
            # a failed check burns the code; a fresh round succeeds once
            code, _, out = m.auth_route("POST", "/api/oauth/authorize", "", h, body)[:3]
            r = m.auth_route("GET", "/wizard-sign-in", json.loads(out)["url"].split("?", 1)[1], h, b"")
            good = json.dumps({"url": r[3]["Location"], "state": "S1", "codeVerifier": verifier}).encode()
            code, _, out = m.auth_route("POST", "/api/oauth/callback", "", h, good)[:3]
            self.assertEqual((code, json.loads(out)["accessToken"]), (201, "PHONE-SESSION"))
            self.assertEqual(m.auth_route("POST", "/api/oauth/callback", "", h, good)[0], 401)
            evil = json.dumps({"redirectUri": "https://evil.example/cb"}).encode()
            self.assertEqual(m.auth_route("POST", "/api/oauth/authorize", "", h, evil)[0], 400)
            code, _, out = m.auth_route("POST", "/api/auth/login", "", h, b'{"email":"","password":""}')[:3]
            self.assertEqual(code, 201)
            self.assertEqual(json.loads(m.auth_route("POST", "/api/auth/logout", "", h, b"")[2])["successful"], True)
        finally:
            m._login_upstream = old

    def test_owner_password_never_in_status(self):
        self.m._write_json("owner.json", {"email": "owner@photos.wizard", "password": "PW-SECRET-987", "token": "TOK-555"})
        out = json.dumps(self.m.status())
        self.assertNotIn("PW-SECRET-987", out)
        self.assertNotIn("TOK-555", out)
        self.assertTrue(json.loads(out)["sign_in"]["managed_account"])
        st = os.stat(os.path.join(self.m.STATE, "owner.json"))
        self.assertEqual(st.st_mode & 0o777, 0o600)

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
