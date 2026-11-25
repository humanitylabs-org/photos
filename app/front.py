"""Wizard MCP front: serves this app's MCP tools at /mcp and passes every other request,
byte for byte, to the app behind it (HTTP, streaming and WebSockets alike).
Stdlib only, so it runs on a stock python image with no build step.
Generated from the app repo's mcp/ folder; edit there, then run mcp/inline.py."""
import asyncio
import contextvars
import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
import uuid

APP = os.environ.get("WIZARD_APP", "app")
VERSION = os.environ.get("APP_VERSION", "0")
PORT = int(os.environ.get("FRONT_PORT") or os.environ.get("PORT") or "8080")
UP_HOST = os.environ.get("UPSTREAM_HOST", "")
UP_PORT = int(os.environ.get("UPSTREAM_PORT", str(PORT)))
MCP_PATH = "/mcp"
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")  # when set, /mcp needs Authorization: Bearer <token>
MAX_HEAD = 65536
STRIP_FORWARDED = tuple(h.strip().lower() + ":" for h in os.environ.get("STRIP_FORWARDED", "").split(",") if h.strip())
MAX_BODY = 1 << 20
TOOLS = {}
TOOL_SWITCH = {}  # tool name -> app switch id: the tool is listed and callable only while that switch is on
ROUTES = {}  # path prefix -> fn(method, path, query, headers, body) -> (status, content_type, bytes); app pages and APIs
ROUTE_MATCH = {}  # path prefix -> fn(path) -> bool: a route can leave some paths under its prefix to the engine
REQUEST = contextvars.ContextVar("request", default={})  # headers of the current /mcp call


def tool(name, description, props=None, required=(), switch=None):
    def deco(fn):
        schema = {"type": "object", "properties": props or {}, "required": list(required)}
        TOOLS[name] = ({"name": name, "description": description, "inputSchema": schema}, fn)
        if switch:
            TOOL_SWITCH[name] = switch
        return fn
    return deco


def route(prefix, match=None):
    def deco(fn):
        ROUTES[prefix] = fn
        if match:
            ROUTE_MATCH[prefix] = match
        return fn
    return deco


async def serve_route(reader, writer, fn, method, target, rest):
    headers = {}
    for line in rest.decode("latin-1").split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length") or 0)
    if n > MAX_BODY:
        return await respond(writer, 413, {"error": "request too large"})
    body = await reader.readexactly(n) if n else b""
    path, _, query = target.partition("?")
    try:
        out = await asyncio.to_thread(fn, method, path, query, headers, body)
        code, ctype, data = out[:3]
        extra = "".join("%s: %s\r\n" % kv for kv in (out[3] if len(out) > 3 else {}).items())
    except Exception as e:  # an app route error is a 500 for that request only
        code, ctype, data, extra = 500, "application/json", json.dumps({"error": "%s: %s" % (type(e).__name__, e)}).encode(), ""
    if method == "HEAD":
        data = b""
    head = "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n%sConnection: close\r\n\r\n" % (
        code, REASONS.get(code, "OK"), ctype, len(data), extra)
    writer.write(head.encode() + data)
    try:
        await writer.drain()
    finally:
        writer.close()


def up_addr():
    try:
        return socket.gethostbyname(UP_HOST)
    except OSError:
        return UP_HOST


def http(method, path, body=None, headers=None, timeout=60):
    url = "http://%s:%d%s" % (up_addr(), UP_PORT, path)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def err(i, code, message):
    return {"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": message}}


def tool_enabled(name):
    sw = TOOL_SWITCH.get(name)
    if not sw:
        return True
    on = globals().get("switch_on")  # the app's switches (wizard.app-switches/1), defined with its tools
    try:
        return bool(on and on(sw))
    except Exception:
        return False


def rpc(msg):
    m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        r = {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
             "serverInfo": {"name": APP, "version": VERSION}}
    elif m == "tools/list":
        r = {"tools": [t[0] for n, t in TOOLS.items() if tool_enabled(n)]}
    elif m == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS or not tool_enabled(name):
            return err(i, -32602, "unknown tool %s" % name)
        try:
            out = TOOLS[name][1](**args)
            if isinstance(out, dict) and "_content" in out:
                r = {"content": out["_content"]}
            else:
                text = out if isinstance(out, str) else json.dumps(out, indent=2)
                r = {"content": [{"type": "text", "text": text[:200000]}]}
        except Exception as e:  # tool errors go back to the agent, not the transport
            r = {"content": [{"type": "text", "text": "%s: %s" % (type(e).__name__, e)}], "isError": True}
    elif m == "ping":
        r = {}
    elif i is None:
        return None  # notification
    else:
        return err(i, -32601, "unknown method %s" % m)
    return {"jsonrpc": "2.0", "id": i, "result": r}


REASONS = {200: "OK", 201: "Created", 202: "Accepted", 302: "Found", 409: "Conflict", 503: "Service Unavailable", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           401: "Unauthorized", 403: "Forbidden", 415: "Unsupported Media Type", 500: "Internal Server Error", 413: "Payload Too Large", 502: "Bad Gateway"}


async def respond(writer, code, obj=None, extra=""):
    body = b"" if obj is None else json.dumps(obj).encode()
    head = "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n%s\r\n" % (
        code, REASONS.get(code, "OK"), len(body), extra)
    writer.write(head.encode() + body)
    try:
        await writer.drain()
    finally:
        writer.close()


async def mcp(reader, writer, method, rest):
    headers = {}
    for line in rest.decode("latin-1").split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if MCP_TOKEN:
        import hmac
        if not hmac.compare_digest(headers.get("authorization", "").encode(), ("Bearer " + MCP_TOKEN).encode()):
            return await respond(writer, 401, {"error": "this MCP needs Authorization: Bearer <token>"}, 'WWW-Authenticate: Bearer\r\n')
    if method != "POST":
        return await respond(writer, 405, {"error": "use POST %s" % MCP_PATH}, "Allow: POST\r\n")
    n = int(headers.get("content-length") or 0)
    if n > MAX_BODY:
        return await respond(writer, 413, {"error": "request too large"})
    body = await reader.readexactly(n) if n else b""
    try:
        msg = json.loads(body or b"{}")
    except ValueError:
        return await respond(writer, 400, err(None, -32700, "parse error"))
    REQUEST.set(headers)
    batch = msg if isinstance(msg, list) else [msg]
    results = await asyncio.gather(*(asyncio.to_thread(rpc, x) for x in batch if isinstance(x, dict)))
    out = [x for x in results if x]
    if not out:
        return await respond(writer, 202)
    await respond(writer, 200, out if isinstance(msg, list) else out[0])


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    line, _, rest = head.partition(b"\r\n")
    parts = line.decode("latin-1").split(" ")
    path = parts[1].split("?", 1)[0] if len(parts) >= 3 else ""
    if path == MCP_PATH:
        return await mcp(reader, writer, parts[0], rest)
    for prefix, fn in ROUTES.items():
        if (path == prefix.rstrip("/") or path.startswith(prefix)) and ROUTE_MATCH.get(prefix, bool)(path or "/"):
            return await serve_route(reader, writer, fn, parts[0], parts[1], rest)
    hdrs = {}
    if INTERCEPTS or HEAD_REWRITE:
        for l in rest.decode("latin-1").split("\r\n"):
            if ":" in l:
                k, v = l.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        for hook in INTERCEPTS:
            fn = hook(parts[0], parts[1] if len(parts) >= 3 else "", hdrs)
            if fn:
                return await serve_route(reader, writer, fn, parts[0], parts[1], rest)
    if not UP_HOST:
        return await respond(writer, 404, {"error": "not found"})
    try:
        ur, uw = await asyncio.open_connection(UP_HOST, UP_PORT)
    except OSError:
        return await respond(writer, 502, {"error": "%s is starting or not reachable" % APP})
    # One request per proxied connection (unless it is an upgrade such as a WebSocket), so a
    # later /mcp request can never ride an upstream keep-alive connection.
    lines = [l for l in head.decode("latin-1").split("\r\n") if l]
    for hook in HEAD_REWRITE:
        lines = hook(parts[0], parts[1] if len(parts) >= 3 else "", hdrs, lines)
    if STRIP_FORWARDED:
        # The engine sees this front as its only client: Home Assistant answers 400 to forwarded headers from a
        # proxy it was not told to trust, so a visit through Tailscale Serve or a tunnel would otherwise fail.
        lines = [l for l in lines if not l.lower().startswith(STRIP_FORWARDED)]
    if not any(l.lower().startswith("upgrade:") for l in lines):
        lines = [l for l in lines if not l.lower().startswith(("connection:", "keep-alive:"))] + ["Connection: close"]
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    uw.write(head)
    await uw.drain()
    await asyncio.gather(pipe(reader, uw), pipe(ur, writer))


STARTUP = []  # fn() called once before serving (an app's own background threads)
# Proxy hooks (no I/O: they run on the event loop). INTERCEPTS: fn(method, target, headers) -> a route fn that answers
# this request here instead of the engine, or None. HEAD_REWRITE: fn(method, target, headers, lines) -> the request
# head lines sent to the engine (lines[0] is the request line).
INTERCEPTS = []
HEAD_REWRITE = []


async def main():
    for fn in STARTUP:
        fn()
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=MAX_HEAD)
    async with srv:
        await srv.serve_forever()


# ---- tools for this app ----
"""Photos: Wizard's front for Immich (https://immich.app, AGPL-3.0), run unmodified next to this front.

- The Wizard page lives at /photos/ (and its files under /photos/). Immich's own web app keeps every other path,
  including /photos (its timeline) and /photos/<asset-uuid> (its viewer), so the app's own port is plain Immich.
- No sign-in: reaching this box over Tailscale is the only access control. On first start this front creates Immich's
  one account itself (a placeholder email and a random password kept only in the private state volume, never shown),
  and every request it passes to Immich without its own credentials gets that account's session added. The Immich
  phone app's sign-in (password or the "Continue" button) is answered here with a real session for the same account.
- The agent's tools at /mcp talk to Immich's documented REST API (https://api.immich.app) with a narrow API key this
  front makes for itself on first start. Secrets never leave the private state volume and no route returns them.
- Google Takeout import uses immich-go (https://github.com/simulot/immich-go, AGPL-3.0), downloaded on the
  owner's first import and checked against its pinned release checksum.
"""
import calendar
import hashlib
import hmac
import http.client as httpclient  # bound as httpclient only: the front's own http() helper keeps its name
import io
import re
import secrets
import subprocess
import tarfile
import threading
import urllib.error

STATE = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or "/state"
IMPORT_DIR = os.environ.get("IMPORT_DIR", "/import")
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", "/library")
APP_PORT = os.environ.get("APP_PORT", "")
PAGE = "/photos/"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[0-9:.]+Z?)?\Z")
KEY_NAME = "Wizard agent (Photos app)"
# What the agent tools and immich-go need (immich-go docs/installation.md "API Permissions"), nothing more.
KEY_PERMISSIONS = [
    "asset.read", "asset.statistics", "asset.update", "asset.upload", "asset.copy", "asset.delete", "asset.download",
    "asset.view", "album.create", "album.read", "albumAsset.create", "server.about", "server.statistics",
    "server.storage", "stack.create", "tag.asset", "tag.create", "tag.read", "user.read", "job.create", "job.read",
    "person.read", "person.statistics", "timeline.read",
]
# Only while the capture inbox is on (wizard-os issue 696): its undo takes a photo out of an album it added it to and
# takes away an album or a tag it made. With the box switch off the key has exactly KEY_PERMISSIONS.
CAPTURE_PERMISSIONS = ["albumAsset.delete", "album.delete", "tag.delete"]


def _key_permissions():
    on = globals().get("_capture_on")
    try:
        extra = CAPTURE_PERMISSIONS if on and on() else []
    except Exception:  # noqa: BLE001
        extra = []
    return KEY_PERMISSIONS + extra
IMMICH_GO = {
    "version": "v0.32.0",
    "amd64": ("https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_x86_64.tar.gz",
              "6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97"),
    "arm64": ("https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_arm64.tar.gz",
              "2c35d9284baae407ef9540bdac5f488971b0bdc7be758a4d7c05ab270af09fdb"),
}
IMPORT_DEFAULTS = {"include_archived": True, "include_partner": True, "sync_albums": True, "include_trashed": False}
LOCK = threading.Lock()
RUN = {"proc": None}


# ---------------- private state (0700 dir, 0600 files) ----------------

def _state_path(name):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    try:
        os.chmod(STATE, 0o700)
    except OSError:
        pass
    return os.path.join(STATE, name)


def _read_json(name, default=None):
    try:
        with open(_state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(name, data):
    p = _state_path(name)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


# ---------------- Immich API ----------------

def _immich(method, path, payload=None, key=None, cookie=None, timeout=30, raw=False, token=None, extra=None):
    """Call Immich's REST API. Returns (status, parsed JSON or bytes). Never raises for HTTP errors."""
    headers = dict(extra or {}, Accept="application/json")
    if key:
        headers["x-api-key"] = key
    if token:
        headers["x-immich-user-token"] = token
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        code, body = http(method, "/api" + path, data, headers, timeout=timeout)
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except OSError as e:
        return 0, {"error": "Immich is not reachable yet (%s)" % type(e).__name__}
    if raw:
        return code, body
    try:
        return code, json.loads(body) if body else {}
    except ValueError:
        return code, {"raw": body[:200].decode("utf-8", "replace")}


def _key():
    return (_read_json("agent-key.json") or {}).get("secret") or ""


def _api(method, path, payload=None, timeout=60):
    key = _key()
    if not key:
        raise RuntimeError("Photos is still setting itself up (Immich starting, or the agent key not made yet). "
                           "Try again in a minute; photos_status shows progress.")
    code, body = _immich(method, path, payload, key=key, timeout=timeout)
    if code == 401 and _ensure_agent_key(force=True):
        code, body = _immich(method, path, payload, key=_key(), timeout=timeout)
    if code == 401:
        raise RuntimeError("Immich does not accept the agent's key and Photos could not make a new one yet. "
                           "Try again shortly; photos_status shows progress.")
    if code == 0:
        raise RuntimeError(body.get("error"))
    if code >= 400:
        raise RuntimeError("Immich answered %d: %s" % (code, (body.get("message") if isinstance(body, dict) else body)))
    return body


def _own_url(headers=None):
    host = ""
    for h in (os.environ.get("WIZARD_TRUSTED_HOSTS") or "").split(","):
        name = h.strip().rsplit(":", 1)[0]
        if name.endswith(".ts.net") or ("." in name and not name.replace(".", "").isdigit()):
            host = name
            break
    if not host and headers:
        host = (headers.get("host") or "").rsplit(":", 1)[0]
    if not host or not APP_PORT:
        return ""
    return "https://%s:%s" % (host, APP_PORT)


# ---------------- status ----------------

def _du(path):
    try:
        st = os.statvfs(path)
        return {"free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1), "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1)}
    except OSError:
        return None


def status(headers=None):
    out = {"app": APP, "version": VERSION, "immich": {"reachable": False}, "agent_connected": False}
    code, ping = _immich("GET", "/server/ping", timeout=5)
    if code == 200:
        out["immich"]["reachable"] = True
        _, ver = _immich("GET", "/server/version", timeout=5)
        if isinstance(ver, dict) and "major" in ver:
            out["immich"]["version"] = "%s.%s.%s" % (ver["major"], ver["minor"], ver["patch"])
        _, cfg = _immich("GET", "/server/config", timeout=5)
        out["immich"]["has_owner_account"] = bool(cfg.get("isInitialized"))
        out["immich"]["trash_days"] = cfg.get("trashDays")
    owner = _owner()
    out["sign_in"] = {"mode": "none: anyone who reaches this box over Tailscale (the owner's tailnet) is the owner",
                      "managed_account": bool(owner.get("password")), "ready": bool(AUTH.get("token")),
                      **({"problem": AUTH["error"]} if AUTH.get("error") else {})}
    key = _key()
    if key and out["immich"]["reachable"]:
        c, me = _immich("GET", "/users/me", key=key, timeout=10)
        out["agent_connected"] = c == 200
        if c == 200:
            out["account"] = {"name": me.get("name"), "email": me.get("email"), "is_admin": me.get("isAdmin")}
            c, stats = _immich("GET", "/server/statistics", key=key, timeout=20)
            if c == 200:
                out["library"] = {"photos": stats.get("photos"), "videos": stats.get("videos"),
                                  "size_gb": round((stats.get("usage") or 0) / 1e9, 2)}
            c, jobs = _immich("GET", "/jobs", key=key, timeout=10)
            if c == 200:
                out["background_work"] = {
                    n: {"waiting": (jobs.get(n) or {}).get("jobCounts", {}).get("waiting", 0),
                        "active": (jobs.get(n) or {}).get("jobCounts", {}).get("active", 0),
                        "paused": (jobs.get(n) or {}).get("queueStatus", {}).get("isPaused", False)}
                    for n in ("metadataExtraction", "thumbnailGeneration", "faceDetection", "facialRecognition",
                              "smartSearch", "duplicateDetection", "videoConversion")}
    out["disk"] = _du(LIBRARY_DIR) or _du(STATE)
    out["import"] = import_summary()
    own = _own_url(headers)
    out["links"] = {"wizard": "?open=photos", **({"immich": own + "/", "mobile_server_url": own} if own else {})}
    sw = globals().get("switch_on")
    if sw and sw("print-review"):
        out["print_review"] = True  # the page shows Print review (switch print-review); absent while off
    if not out["immich"]["reachable"]:
        out["next"] = "Immich is starting (the first start takes a minute or two). Try again shortly."
    elif not out["immich"]["has_owner_account"] or not out["agent_connected"]:
        out["next"] = ("Photos is setting itself up (it makes its own account; no sign-in for the owner). Check again "
                       "in a minute." if not AUTH.get("error") else "Setup problem: " + AUTH["error"])
    return out


# ---------------- people and search ----------------

def _person_ids(person):
    if not person:
        return []
    wanted = [p.strip() for p in str(person).split(",") if p.strip()]
    ids, everyone = [], None
    for w in wanted:
        if UUID_RE.match(w):
            ids.append(w)
            continue
        if everyone is None:
            everyone = _all_people()
        hits = [p for p in everyone if (p.get("name") or "").strip().lower() == w.lower()]
        if not hits:
            hits = [p for p in everyone if w.lower() in (p.get("name") or "").lower()]
        if len(hits) != 1:
            names = sorted({p.get("name") for p in hits if p.get("name")})[:10]
            raise ValueError("No single person named %r (%s). Use photos_people to get the exact id." %
                             (w, "matches: " + ", ".join(names) if names else "no match"))
        ids.append(hits[0]["id"])
    return ids


def _all_people(with_hidden=False):
    people, page = [], 1
    while page <= 50:
        q = urllib.parse.urlencode({"page": page, "size": 500, "withHidden": "true" if with_hidden else "false"})
        body = _api("GET", "/people?" + q)
        people += body.get("people") or []
        if not body.get("hasNextPage"):
            break
        page += 1
    return people


def people(name="", include_unnamed=False, limit=50):
    limit = max(1, min(int(limit or 50), 500))
    everyone = _all_people()
    rows = [p for p in everyone if include_unnamed or (p.get("name") or "").strip()]
    if name:
        rows = [p for p in rows if name.lower() in (p.get("name") or "").lower()]
    out = []
    for i, p in enumerate(rows[:limit]):
        row = {"id": p["id"], "name": p.get("name") or "(unnamed)", "favorite": p.get("isFavorite", False)}
        if i < 40:
            try:
                row["photos"] = _api("GET", "/people/%s/statistics" % p["id"]).get("assets")
            except RuntimeError:
                pass
        out.append(row)
    unnamed = sum(1 for p in everyone if not (p.get("name") or "").strip())
    return {"people": out, "shown": len(out), "named_total": len(everyone) - unnamed, "unnamed_faces_groups": unnamed,
            "note": "Immich groups faces automatically after import. Name a face group in Immich (People) and it "
                    "shows here by name."}


def _filters(person="", taken_after="", taken_before="", media_type="", city="", favorite=None, album_id=""):
    f = {}
    ids = _person_ids(person)
    if ids:
        f["personIds"] = ids
    for k, v in (("takenAfter", taken_after), ("takenBefore", taken_before)):
        if v:
            if not DATE_RE.match(str(v)):
                raise ValueError("%s must be a date like 2019-06-30" % k)
            f[k] = v if "T" in v else v + ("T00:00:00.000Z" if k == "takenAfter" else "T23:59:59.999Z")
    if media_type:
        t = str(media_type).upper()
        if t not in ("IMAGE", "VIDEO"):
            raise ValueError("media_type is IMAGE or VIDEO")
        f["type"] = t
    if city:
        f["city"] = city
    if favorite is not None:
        f["isFavorite"] = bool(favorite)
    if album_id:
        if not UUID_RE.match(album_id):
            raise ValueError("album_id must be an Immich album id")
        f["albumIds"] = [album_id]
    return f


def _asset_row(a, own):
    ex = a.get("exifInfo") or {}
    row = {"id": a["id"], "file": a.get("originalFileName"), "type": a.get("type"),
           "taken": a.get("localDateTime") or a.get("fileCreatedAt"),
           "people": [p.get("name") for p in (a.get("people") or []) if p.get("name")],
           "place": ", ".join(x for x in (ex.get("city"), ex.get("country")) if x) or None}
    if own:
        row["open"] = "%s/photos/%s" % (own, a["id"])
    return row


def search(query="", person="", taken_after="", taken_before="", media_type="", city="", favorite=None, limit=25):
    limit = max(1, min(int(limit or 25), 200))
    f = _filters(person, taken_after, taken_before, media_type, city, favorite)
    own = _own_url()
    if query:
        body = _api("POST", "/search/smart", dict(f, query=str(query)[:500], size=limit, withExif=True))
        items = (body.get("assets") or {}).get("items") or []
        return {"mode": "smart (meaning-based, best matches first)", "shown": len(items),
                "assets": [_asset_row(a, own) for a in items]}
    total = _api("POST", "/search/statistics", f).get("total")
    body = _api("POST", "/search/metadata", dict(f, size=limit, withExif=True, withPeople=True, order="desc"))
    items = (body.get("assets") or {}).get("items") or []
    return {"mode": "filters", "total_matching": total, "shown": len(items), "assets": [_asset_row(a, own) for a in items]}


# ---------------- trash (never a permanent delete) ----------------

def _matching_ids(f, cap=50000):
    ids, page = [], 1
    while len(ids) < cap:
        body = _api("POST", "/search/metadata", dict(f, size=1000, page=page), timeout=120)
        a = body.get("assets") or {}
        ids += [x["id"] for x in a.get("items") or []]
        if not a.get("nextPage"):
            break
        page = int(a["nextPage"])
    return ids[:cap]


def move_to_trash(asset_ids=None, person="", taken_after="", taken_before="", media_type="", expected_count=None,
                  confirmed=False):
    asset_ids = [str(x) for x in (asset_ids or [])]
    bad = [x for x in asset_ids if not UUID_RE.match(x)]
    if bad:
        raise ValueError("not Immich asset ids: %s" % ", ".join(bad[:5]))
    if asset_ids and (person or taken_after or taken_before or media_type):
        raise ValueError("Give either asset_ids or filters, not both.")
    trash_days = (_immich("GET", "/server/config", timeout=5)[1] or {}).get("trashDays", 30)
    if asset_ids:
        count = len(set(asset_ids))
        ids = sorted(set(asset_ids))
        f = None
    else:
        if not (person or taken_after or taken_before):
            raise ValueError("Say which photos: a person and/or a date range (taken_after/taken_before), or asset_ids. "
                             "This tool never trashes the whole library.")
        f = _filters(person, taken_after, taken_before, media_type)
        count = _api("POST", "/search/statistics", f).get("total") or 0
        ids = None
    if not confirmed:
        sample = []
        if f is not None and count:
            body = _api("POST", "/search/metadata", dict(f, size=5, withPeople=True, withExif=True))
            sample = [_asset_row(a, _own_url()) for a in (body.get("assets") or {}).get("items") or []]
        return {"preview": True, "would_move_to_trash": count, "sample": sample,
                "next": "Tell the owner exactly how many photos/videos this is and get his yes. Then call again with "
                        "confirmed=true and expected_count=%d. They go to Immich's trash (kept %s days, restorable), "
                        "not deleted." % (count, trash_days)}
    if expected_count is None or int(expected_count) != count:
        raise ValueError("The matching set is now %d items, not %s. Preview again and re-confirm with the owner." %
                         (count, expected_count))
    if ids is None:
        ids = _matching_ids(f)
        if len(ids) != count:
            raise ValueError("Found %d ids for %d matches; nothing moved. Try again." % (len(ids), count))
    moved = 0
    for i in range(0, len(ids), 500):
        _api("DELETE", "/assets", {"ids": ids[i:i + 500], "force": False}, timeout=300)
        moved += len(ids[i:i + 500])
    return {"moved_to_trash": moved, "restorable_for_days": trash_days,
            "undo": "photos_restore_from_trash with the same asset ids, or Immich > Trash > Restore."}


def restore_from_trash(asset_ids=None, everything=False):
    asset_ids = [str(x) for x in (asset_ids or []) if UUID_RE.match(str(x))]
    if everything:
        body = _api("POST", "/trash/restore")
        return {"restored": body.get("count"), "scope": "everything in the trash"}
    if not asset_ids:
        raise ValueError("Give asset_ids, or everything=true to restore the whole trash.")
    n = 0
    for i in range(0, len(asset_ids), 500):
        body = _api("POST", "/trash/restore/assets", {"ids": asset_ids[i:i + 500]})
        n += body.get("count") or 0
    return {"restored": n}


# ---------------- Google Takeout import (immich-go) ----------------

def _zips():
    try:
        names = sorted(os.listdir(IMPORT_DIR))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(IMPORT_DIR, n)
        if n.lower().endswith((".zip", ".tgz", ".tar.gz")) and not n.startswith(".") and os.path.isfile(p):
            st = os.stat(p)
            out.append({"name": n, "size_gb": round(st.st_size / 1e9, 2), "modified": int(st.st_mtime)})
    return out


def import_options():
    saved = _read_json("import-options.json", {}) or {}
    return {k: saved.get(k, v) if isinstance(saved.get(k), bool) else v for k, v in IMPORT_DEFAULTS.items()}


def set_import_options(d):
    if not isinstance(d, dict) or set(d) - set(IMPORT_DEFAULTS) or not all(isinstance(v, bool) for v in d.values()):
        raise ValueError("options are booleans: " + ", ".join(IMPORT_DEFAULTS))
    _write_json("import-options.json", dict(import_options(), **d))
    return import_options()


LINE_RE = re.compile(r"(uploaded|server duplicate|local duplicate|error|discarded|already|skipped|album)", re.I)


def _log_tail(path, n=12):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    hide = [x for x in (_key(), _owner().get("password"), _owner().get("token"), AUTH.get("token")) if x]
    out = []
    for l in lines[-n:]:
        for x in hide:
            l = l.replace(x, "***")
        out.append(l[:300])
    return out


def import_summary():
    st = _read_json("import.json", {}) or {}
    running = bool(RUN["proc"] and RUN["proc"].poll() is None)
    if st.get("state") == "running" and not running:
        st["state"] = "interrupted"
    zips = _zips()
    out = {"state": st.get("state", "idle"), "drop_folder": "Files > Photos > Import",
           "waiting_files": len(zips), "waiting_gb": round(sum(z["size_gb"] for z in zips), 2)}
    for k in ("started_at", "finished_at", "exit_code", "files", "message"):
        if k in st:
            out[k] = st[k]
    return out


def import_status():
    st = _read_json("import.json", {}) or {}
    out = import_summary()
    out["files_in_drop_folder"] = _zips()
    out["options"] = import_options()
    if st.get("log"):
        out["log_tail"] = _log_tail(st["log"])
    out["how"] = ("Google Takeout: choose only Google Photos, .zip, 50 GB parts. Put every part in Files > Photos > "
                  "Import (or upload them there from a computer), then start the import. Running it again is safe: "
                  "photos already in Immich are skipped.")
    return out


def _arch():
    m = os.uname().machine
    return "arm64" if m in ("aarch64", "arm64") else "amd64"


def _immich_go():
    url, sha = IMMICH_GO[_arch()]
    bindir = _state_path("bin")
    os.makedirs(bindir, mode=0o700, exist_ok=True)
    exe = os.path.join(bindir, "immich-go-" + IMMICH_GO["version"])
    if os.path.isfile(exe):
        return exe
    with urllib.request.urlopen(url, timeout=300) as r:
        blob = r.read()
    if hashlib.sha256(blob).hexdigest() != sha:
        raise RuntimeError("immich-go download did not match its pinned checksum; not running it")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        member = t.getmember("immich-go")
        data = t.extractfile(member).read()
    tmp = exe + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o700)
    os.replace(tmp, exe)
    return exe


def _run_import(files, opts):
    st = {"state": "running", "started_at": int(time.time()), "files": [z["name"] for z in files]}
    logdir = _state_path("import-logs")
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    st["log"] = os.path.join(logdir, time.strftime("%Y%m%d-%H%M%S") + ".log")
    _write_json("import.json", st)
    try:
        exe = _immich_go()
        server = "http://%s:%d" % (UP_HOST, UP_PORT)
        env = {"PATH": "/usr/bin:/bin", "HOME": STATE, "TMPDIR": _state_path("tmp"),
               "IMMICH_GO_UPLOAD_SERVER": server, "IMMICH_GO_UPLOAD_API_KEY": _key(),
               "IMMICH_GO_UPLOAD_ADMIN_API_KEY": _key()}
        os.makedirs(env["TMPDIR"], mode=0o700, exist_ok=True)
        argv = [exe, "upload", "from-google-photos", "--no-ui", "--on-errors=continue", "--concurrent-tasks=4",
                "--log-file=" + st["log"], "--client-timeout=60m",
                "--include-archived=%s" % str(opts["include_archived"]).lower(),
                "--include-partner=%s" % str(opts["include_partner"]).lower(),
                "--sync-albums=%s" % str(opts["sync_albums"]).lower(),
                "--include-trashed=%s" % str(opts["include_trashed"]).lower(),
                "--people-tag=true", "--takeout-tag=true"] + [os.path.join(IMPORT_DIR, z["name"]) for z in files]
        with open(st["log"] + ".out", "ab") as out:
            RUN["proc"] = subprocess.Popen(argv, env=env, cwd=STATE, stdout=out, stderr=subprocess.STDOUT)
            code = RUN["proc"].wait()
        st.update(state="done" if code == 0 else "failed", exit_code=code, finished_at=int(time.time()))
        if code:
            st["message"] = " | ".join(_log_tail(st["log"] + ".out", 3))[:600]
    except Exception as e:
        st.update(state="failed", finished_at=int(time.time()), message="%s: %s" % (type(e).__name__, e))
    finally:
        RUN["proc"] = None
        _write_json("import.json", st)


def import_start(confirmed=False):
    with LOCK:
        if RUN["proc"] and RUN["proc"].poll() is None:
            return {"started": False, "state": "running", "message": "An import is already running."}
        files = _zips()
        if not files:
            return {"started": False, "message": "No Takeout files yet. Put the .zip parts in Files > Photos > Import."}
        if not _key():
            raise RuntimeError("Photos is still setting itself up (no agent key yet). Try again in a minute.")
        if not confirmed:
            return {"started": False, "preview": True, "files": files,
                    "next": "Call again with confirmed=true to import these %d file(s) into Immich." % len(files)}
        opts = import_options()
        threading.Thread(target=_run_import, args=(files, opts), daemon=True).start()
        for _ in range(20):
            if (_read_json("import.json", {}) or {}).get("state") == "running":
                break
            time.sleep(0.1)
    return {"started": True, "files": [z["name"] for z in files], "options": opts,
            "note": "Large Takeouts take hours. Face grouping and search indexing run after the upload."}


def _prepare_import_dir():
    """Let the Files app (uid 1000) drop Takeout files here: group 1000, setgid, group-writable. Never recursive."""
    try:
        st = os.stat(IMPORT_DIR)
        if st.st_gid != 1000:
            os.chown(IMPORT_DIR, -1, 1000)
        if (st.st_mode & 0o7777) != 0o2775:
            os.chmod(IMPORT_DIR, 0o2775)
    except OSError as e:
        print("photos: could not prepare %s (%s)" % (IMPORT_DIR, e), flush=True)


STARTUP.append(_prepare_import_dir)


# ---------------- no sign-in: Photos' own Immich account, added to every request ----------------
# Miguel: "Tailscale will be my one and only access. I don't want to have to log into something. I'm the only user."
# Immich keeps its normal accounts; this front owns the single account and answers for it:
# - first start: admin sign-up with a placeholder email and a random password (owner.json, 0600, never shown),
#   then one login session for browsers and a narrow API key for the agent;
# - every request passed to Immich that brings no credentials of its own gets that session (x-immich-user-token),
#   and browser page loads get Immich's readable "immich_is_authenticated" cookie, so the web app opens signed in;
# - the phone app's sign-in (POST /api/auth/login with anything, or its OAuth "Continue" button, answered here with
#   PKCE checked) returns a real Immich session for that account, so the phone has its own device session.
# Requests from another tailnet login than the owner's, from a host name this box doesn't serve, or cross-site from
# a web page (writes) get nothing added and meet Immich's normal sign-in.

OWNER_EMAIL = "owner@photos.wizard"  # a placeholder, never a person's address; Immich only needs some email
OWNER_NAME = "Owner"
AUTH = {"token": "", "error": ""}
AUTH_LOCK = threading.Lock()
KEY_LOCK = threading.Lock()
PENDING = {}  # phone sign-in starts: nonce -> {"redirect", "state", "challenge", "exp"}
CODES = {}  # one-time phone sign-in codes: code -> {"state", "challenge", "exp"}
CODE_TTL = 300
OWN_CREDENTIALS = ("x-immich-user-token", "x-immich-session-token", "authorization", "x-api-key")
SIGN_IN_PATH = "/wizard-sign-in"
COOKIE_AGE = 400 * 86400


def _owner():
    return _read_json("owner.json", {}) or {}


def _login_upstream(user_agent=""):
    """A real Immich password login for Photos' own account: (status, Immich's LoginResponseDto)."""
    o = _owner()
    if not o.get("password"):
        return 503, {"message": "Photos is still setting itself up. Try again in a minute.", "statusCode": 503}
    return _immich("POST", "/auth/login", {"email": o["email"], "password": o["password"]},
                   extra={"User-Agent": user_agent or "Wizard Photos"}, timeout=30)


def _ensure_agent_key(force=False):
    with KEY_LOCK:
        old = _read_json("agent-key.json") or {}
        tok = AUTH.get("token")
        if old.get("secret") and not force:
            c, _ = _immich("GET", "/users/me", key=old["secret"], timeout=10)
            # A key whose stored scopes differ from KEY_PERMISSIONS (a scope added or dropped) is remade once.
            upgrade = c == 200 and tok and sorted(old.get("permissions") or []) != sorted(_key_permissions())
            if c != 401 and not upgrade:
                return c == 200
        if not tok:
            return False
        if old.get("id"):
            _immich("DELETE", "/api-keys/%s" % old["id"], token=tok)
        perms = _key_permissions()
        c, made = _immich("POST", "/api-keys", {"name": KEY_NAME, "permissions": perms}, token=tok)
        if c not in (200, 201) or not made.get("secret"):
            return False
        _write_json("agent-key.json", {"id": (made.get("apiKey") or {}).get("id"), "secret": made["secret"],
                                        "user": _owner().get("email"), "permissions": list(perms),
                                        "created_at": int(time.time())})
        return True


def _setup_once():
    """Make sure Photos' own account, its session and the agent key exist. True when everything is ready."""
    with AUTH_LOCK:
        code, _ = _immich("GET", "/server/ping", timeout=5)
        if code != 200:
            return False
        _, cfg = _immich("GET", "/server/config", timeout=10)
        o = _owner()
        if not cfg.get("isInitialized"):
            if not o.get("password"):
                o = {"email": OWNER_EMAIL, "password": secrets.token_urlsafe(32), "created_at": int(time.time())}
                _write_json("owner.json", o)  # saved before Immich hears it, so a crash never strands the account
            c, b = _immich("POST", "/auth/admin-sign-up",
                           {"email": o["email"], "password": o["password"], "name": OWNER_NAME}, timeout=30)
            if c not in (200, 201):
                AUTH["error"] = "Immich did not create Photos' account (%s: %s)" % (c, (b or {}).get("message"))
                return False
        elif not o.get("password"):
            AUTH["error"] = ("Immich already has an account that Photos did not make, so Photos cannot open it without "
                             "a sign-in. Reinstalling Photos with its data removed starts fresh.")
            return False
        tok = AUTH.get("token") or o.get("token") or ""
        me = {}
        if tok:
            c, me = _immich("GET", "/users/me", token=tok, timeout=10)
            if c == 401:
                tok = ""
        if not tok:
            c, b = _login_upstream()
            if c not in (200, 201) or not (b or {}).get("accessToken"):
                AUTH["error"] = "Photos could not open its own Immich account (%s)." % c
                return False
            tok = b["accessToken"]
            o["token"] = tok
            _write_json("owner.json", o)
            c, me = _immich("GET", "/users/me", token=tok, timeout=10)
        AUTH["token"] = tok
        if isinstance(me, dict) and me.get("shouldChangePassword") and me.get("id"):
            # Immich asks a new admin to change his password on the first phone sign-in; there is no password to change.
            _immich("PUT", "/admin/users/%s" % me["id"], {"shouldChangePassword": False}, token=tok)
        if not o.get("onboarded"):
            # Skip Immich's first-run tour for the one account; its settings stay in Immich > Administration.
            c1, _ = _immich("POST", "/system-metadata/admin-onboarding", {"isOnboarded": True}, token=tok)
            c2, _ = _immich("PUT", "/users/me/onboarding", {"isOnboarded": True}, token=tok)
            if c1 in (200, 201, 204) and c2 in (200, 201, 204):
                o["onboarded"] = True
                _write_json("owner.json", o)
        ok = _ensure_agent_key()
        AUTH["error"] = "" if ok else "Photos could not make the agent's key yet."
        return ok


def _auth_loop():
    while True:
        try:
            ok = _setup_once()
        except Exception as e:  # keep retrying; the status shows the problem
            ok, AUTH["error"] = False, "%s: %s" % (type(e).__name__, e)
        time.sleep(300 if ok else 10)


STARTUP.append(lambda: threading.Thread(target=_auth_loop, daemon=True).start())


def _hostname(netloc):
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _served_names():
    names = {_hostname(h) for h in (os.environ.get("WIZARD_TRUSTED_HOSTS") or "").split(",") if h.strip()}
    return names | {"127.0.0.1", "localhost"} if names else set()


def _owner_request(method, headers):
    """Is this request the box owner's own (his tailnet, this box's address, not a cross-site write)?"""
    names = _served_names()
    if names and _hostname(headers.get("host")) not in names:
        return False
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    owners = {x.strip().lower() for x in (os.environ.get("WIZARD_OWNER_LOGINS") or "").split(",") if x.strip()}
    if owners and login and login not in owners:
        return False
    read = method in ("GET", "HEAD")
    if headers.get("sec-fetch-site") == "cross-site" and not (read and headers.get("sec-fetch-mode") == "navigate"):
        return False
    origin = headers.get("origin")
    if origin and not read:
        oname = urllib.parse.urlsplit(origin).hostname if origin != "null" else None
        if not oname or (names and oname not in names) or (not names and oname != _hostname(headers.get("host"))):
            return False
    return True


def _has_own_credentials(target, headers):
    if any(headers.get(h) for h in OWN_CREDENTIALS) or "immich_access_token=" in (headers.get("cookie") or ""):
        return True
    q = target.partition("?")[2]
    return bool(q) and any(k in ("apiKey", "sessionKey") for k, _ in urllib.parse.parse_qsl(q))


def _add_session(method, target, headers, lines):
    tok = AUTH.get("token")
    if not tok or _has_own_credentials(target, headers) or not _owner_request(method, headers):
        return lines
    return lines[:1] + [l for l in lines[1:] if not l.lower().startswith("x-immich-user-token:")] + [
        "x-immich-user-token: " + tok]


HEAD_REWRITE.append(_add_session)


def _base_url(headers):
    host = (headers.get("host") or "").strip()
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "https" if _hostname(host).endswith(".ts.net") else "http"
    return "%s://%s" % (proto, host)


def _b64url_sha256(text):
    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).decode().rstrip("=")


def _expire():
    now = time.time()
    for d in (PENDING, CODES):
        for k in [k for k, v in d.items() if v["exp"] < now]:
            d.pop(k, None)


def _sign_in_page(code, text):
    html = ("<!doctype html><meta name=viewport content='width=device-width'><title>Photos</title>"
            "<body style='font:1rem system-ui;padding:1.5rem'><p>%s</p>" % text)
    return code, "text/html; charset=utf-8", html.encode()


def _login_answer(headers):
    c, b = _login_upstream(headers.get("user-agent", ""))
    if c in (200, 201) and (b or {}).get("accessToken"):
        return _json(201, b)
    return _json(503 if c in (0, 503) else 502, {"message": "Photos could not open its account right now. Try again "
                                                            "in a minute.", "statusCode": 503})


def auth_route(method, path, query, headers, body):
    """Immich's sign-in endpoints, answered for Photos' own account (see the section comment)."""
    _expire()
    if path == "/api/auth/login":
        return _login_answer(headers)
    if path == "/api/auth/logout":
        # Nothing to sign out of: the session stays, the web app goes back to the library.
        return _json(200, {"successful": True, "redirectUri": "/photos"})
    if path == "/api/oauth/authorize":
        d = json.loads(body or b"{}") if body else {}
        redirect = str(d.get("redirectUri") or "")
        rp = urllib.parse.urlsplit(redirect)
        if not (rp.scheme == "app.immich" or (rp.scheme in ("http", "https") and
                                              _hostname(rp.netloc) == _hostname(headers.get("host")))):
            return _json(400, {"message": "unsupported redirect", "statusCode": 400})
        if len(PENDING) > 200:
            return _json(429, {"message": "too many sign-ins in progress", "statusCode": 429})
        nonce = secrets.token_urlsafe(24)
        PENDING[nonce] = {"redirect": redirect, "state": str(d.get("state") or ""),
                          "challenge": str(d.get("codeChallenge") or ""), "exp": time.time() + CODE_TTL}
        return _json(201, {"url": _base_url(headers) + SIGN_IN_PATH + "?" + urllib.parse.urlencode({"r": nonce})})
    if path == SIGN_IN_PATH:
        nonce = dict(urllib.parse.parse_qsl(query)).get("r", "")
        p = PENDING.pop(nonce, None)
        if not p:
            return _sign_in_page(400, "This link has expired. Go back to the Immich app and tap Continue again.")
        code = secrets.token_urlsafe(32)
        CODES[code] = {"state": p["state"], "challenge": p["challenge"], "exp": time.time() + CODE_TTL}
        sep = "&" if "?" in p["redirect"] else "?"
        loc = p["redirect"] + sep + urllib.parse.urlencode({"code": code, "state": p["state"]})
        return 302, "text/html; charset=utf-8", b"", {"Location": loc}
    if path == "/api/oauth/callback":
        d = json.loads(body or b"{}") if body else {}
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(str(d.get("url") or "")).query))
        entry = CODES.pop(q.get("code", ""), None)
        if not entry:
            return _json(401, {"message": "This sign-in has expired. Tap Continue again.", "statusCode": 401})
        state = str(d.get("state") or q.get("state") or "")
        if entry["state"] and not hmac.compare_digest(entry["state"], state):
            return _json(401, {"message": "Sign-in state did not match. Tap Continue again.", "statusCode": 401})
        if entry["challenge"] and not hmac.compare_digest(entry["challenge"], _b64url_sha256(str(d.get("codeVerifier") or ""))):
            return _json(401, {"message": "Sign-in check failed. Tap Continue again.", "statusCode": 401})
        return _login_answer(headers)
    return _json(404, {"message": "not found", "statusCode": 404})


def phone_config_route(method, path, query, headers, body):
    """Immich's public features/config for the phone app: one "Continue" button instead of an email/password form."""
    c, b = _immich("GET", path[len("/api"):] + ("?" + query if query else ""), timeout=15)
    if c == 200 and isinstance(b, dict):
        if path.endswith("/features"):
            b.update(oauth=True, oauthAutoLaunch=False, passwordLogin=False)
        else:
            b["oauthButtonText"] = "Continue"
    return _json(c or 502, b)


HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-type", "cache-control",
       "x-content-type-options", "set-cookie", "upgrade", "proxy-connection", "te", "trailer"}


def page_with_cookie_route(method, path, query, headers, body):
    """A browser page load: Immich's own answer plus its readable sign-in marker cookie."""
    target = path + ("?" + query if query else "")
    fwd = {k: headers[k] for k in ("accept", "accept-language", "user-agent") if headers.get(k)}
    fwd["x-immich-user-token"] = AUTH.get("token", "")
    conn = httpclient.HTTPConnection(up_addr(), UP_PORT, timeout=30)
    try:
        conn.request(method, target, headers=fwd)
        r = conn.getresponse()
        data = r.read()
        extra = {k: v for k, v in r.getheaders() if k.lower() not in HOP}
        ctype = r.getheader("content-type") or "text/html; charset=utf-8"
        code = r.status
    finally:
        conn.close()
    secure = "; Secure" if _base_url(headers).startswith("https:") else ""
    extra["Set-Cookie"] = "immich_is_authenticated=true; Path=/; Max-Age=%d; SameSite=Lax%s" % (COOKIE_AGE, secure)
    return code, ctype, data, extra


AUTH_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/oauth/authorize", "/api/oauth/callback"}


def _intercept(method, target, headers):
    path = target.split("?", 1)[0]
    if not (path in AUTH_PATHS or path == SIGN_IN_PATH or path.startswith("/api/server/") or
            not path.startswith(("/api/", "/.well-known/"))):
        return None
    if not _owner_request(method, headers):
        return None
    if path in AUTH_PATHS and method == "POST":
        return auth_route
    if path == SIGN_IN_PATH and method in ("GET", "HEAD"):
        return auth_route
    if method == "GET" and path in ("/api/server/features", "/api/server/config") and not headers.get("sec-fetch-site"):
        return phone_config_route  # the phone app (browsers always send Sec-Fetch-Site)
    if (method in ("GET", "HEAD") and AUTH.get("token") and not path.startswith(("/api/", "/.well-known/"))
            and "immich_is_authenticated=" not in (headers.get("cookie") or "")
            and not _has_own_credentials(target, headers)
            and (headers.get("sec-fetch-dest") in ("document", "iframe") or "text/html" in (headers.get("accept") or ""))):
        return page_with_cookie_route
    return None


INTERCEPTS.append(_intercept)


# ---------------- MCP tools (box-only: the agent through the Wizard hub) ----------------

def _box_only():
    h = REQUEST.get()
    if any(h.get(k) for k in ("tailscale-user-login", "x-forwarded-for", "forwarded", "cf-connecting-ip",
                              "cf-access-jwt-assertion", "x-real-ip")):
        raise PermissionError("the Photos tools are only for apps on this box")


@tool("photos_status", "Photos (Immich) status: whether Immich is up and the owner's account exists, whether the agent "
      "is connected, library size (photos, videos, GB), background work still running (face detection, face "
      "grouping, search indexing), Google Takeout import state, free disk, links, and the owner's next step.")
def photos_status():
    _box_only()
    return status()


@tool("photos_people", "List the people Immich recognised by face (face groups), with how many photos each is in. "
      "Named people by default; include_unnamed=true also lists face groups the owner has not named yet.",
      {"name": {"type": "string", "description": "Only people whose name contains this"},
       "include_unnamed": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})
def photos_people(name="", include_unnamed=False, limit=50):
    _box_only()
    return people(name, include_unnamed, limit)


@tool("photos_search", "Find photos and videos. With `query` it is a meaning-based search (e.g. 'beach at sunset', "
      "'birthday cake'); without it, filter by person, dates, type, city or favourite and get the total count. "
      "Returns ids, file names, dates, people and an `open` link per item.",
      {"query": {"type": "string"}, "person": {"type": "string", "description": "Person name or id; several comma-separated means photos with all of them"},
       "taken_after": {"type": "string", "description": "YYYY-MM-DD"}, "taken_before": {"type": "string", "description": "YYYY-MM-DD"},
       "media_type": {"type": "string", "enum": ["IMAGE", "VIDEO"]}, "city": {"type": "string"},
       "favorite": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}})
def photos_search(query="", person="", taken_after="", taken_before="", media_type="", city="", favorite=None, limit=25):
    _box_only()
    return search(query, person, taken_after, taken_before, media_type, city, favorite, limit)


@tool("photos_move_to_trash", "Move photos/videos to Immich's TRASH (restorable for the trash period, 30 days by "
      "default; never a permanent delete). Pick them by asset_ids, or by person and/or date range. First call without "
      "confirmed: it returns the exact count and a sample. Tell the owner the count, get his explicit yes, then call "
      "again with confirmed=true and expected_count equal to that count.",
      {"asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5000},
       "person": {"type": "string"}, "taken_after": {"type": "string"}, "taken_before": {"type": "string"},
       "media_type": {"type": "string", "enum": ["IMAGE", "VIDEO"]},
       "expected_count": {"type": "integer"}, "confirmed": {"type": "boolean"}})
def photos_move_to_trash(asset_ids=None, person="", taken_after="", taken_before="", media_type="", expected_count=None,
                         confirmed=False):
    _box_only()
    return move_to_trash(asset_ids, person, taken_after, taken_before, media_type, expected_count, confirmed)


@tool("photos_restore_from_trash", "Undo: bring photos back from Immich's trash, by asset_ids or everything=true.",
      {"asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5000}, "everything": {"type": "boolean"}})
def photos_restore_from_trash(asset_ids=None, everything=False):
    _box_only()
    return restore_from_trash(asset_ids, everything)


@tool("photos_import_status", "Google Takeout import: Takeout files waiting in Files > Photos > Import, whether an "
      "import is running, done or failed, its options and the last log lines.")
def photos_import_status():
    _box_only()
    return import_status()


@tool("photos_import_start", "Import every Google Takeout file in Files > Photos > Import into Immich with immich-go "
      "(keeps dates, places, descriptions and albums from Google's JSON files; skips photos already imported). "
      "Without confirmed it only lists what would be imported.",
      {"confirmed": {"type": "boolean"}})
def photos_import_start(confirmed=False):
    _box_only()
    return import_start(confirmed)


# ---------------- owner page and its API ----------------

def _json(code, obj, extra=None):
    return (code, "application/json", json.dumps(obj).encode()) + ((extra,) if extra else ())


_LOCAL = ("127.0.0.1", "localhost")


def _trusted(headers, write):
    host = (headers.get("host") or "").strip().lower()
    hosts = {h.strip().lower() for h in (os.environ.get("WIZARD_TRUSTED_HOSTS") or "").split(",") if h.strip()}
    if hosts and host not in hosts and host.rsplit(":", 1)[0] not in _LOCAL:
        return False
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    owners = {x.strip().lower() for x in (os.environ.get("WIZARD_OWNER_LOGINS") or "").split(",") if x.strip()}
    if owners and login and login not in owners:
        return False
    if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if write and origin and urllib.parse.urlsplit(origin).netloc.lower() != host:
        return False
    return True


def _body(headers, body):
    if (headers.get("content-type") or "").split(";")[0].strip() != "application/json" or len(body) > 8192:
        raise ValueError("send JSON")
    d = json.loads(body or b"{}")
    if not isinstance(d, dict):
        raise ValueError("send a JSON object")
    return d


# ---- capture inbox (wizard-os issue 696, spec/CAPTURE_INBOXES.md; the box switch wizard/capture-inbox) ----
# New photos from the camera-roll backup are an inbox: after a quiet period Photos hands a batch (its own id and count,
# no content) to the owner's main agent through the Agent Service, with Photos's own app token. The agent reads the
# batch with photos_capture_batch and handles each photo by the owner's rules with photos_capture_file (tags, an album,
# the processed mark; all logged here) and the Service's wizard_capture (copies in Files, notes). Photos never moves or
# deletes an original. "Undo that" on the Service marks the batch undone; Photos reads that from the probe and takes
# away its own tags, album additions and processed marks.
# Off (the Service answers 404, an older Service, or no token): Photos does nothing new, and these tools are not listed.
CAPTURE_SERVICE = (os.environ.get("PHOTOS_AGENT_SERVICE") or "http://wizard-agent-service:8787").rstrip("/")
CAPTURE_PATH = "/wizard/v1/capture/events"
CAPTURE_POLL_S = max(5, int(os.environ.get("PHOTOS_CAPTURE_POLL_S") or 30))
CAPTURE_QUIET_S = max(1, int(os.environ.get("PHOTOS_CAPTURE_QUIET_S") or 120))
CAPTURE_RECENT_S = 3 * 86400   # only photos taken at most 3 days before they arrived: a phone's first backup of years
                               # of camera roll, or a Takeout import, is the owner's library, not new captures
CAPTURE_BATCH_MAX = 50
CAPTURE_GATE = "box:capture-inbox"
CAPTURE_SETTLE_S = 2.0
PROCESSED_TAG = "Wizard/Processed"
CAPTURE_LOCK = threading.Lock()
BATCH_RE = re.compile(r"^pb_[0-9a-f]{12}\Z")


def _capture_token():
    return (os.environ.get("PHOTOS_AGENT_INBOX_TOKEN") or "").strip()


def _capture_state():
    st = _read_json("capture.json", None)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("active", False)
    st.setdefault("cursor", "")
    st.setdefault("edge", [])
    st.setdefault("pending", {})
    st.setdefault("last_new", 0)
    st.setdefault("batches", {})
    return st


def _capture_service(method, payload=None):
    """Call the Agent Service's capture route with Photos's own app token. Returns (status, JSON or {})."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(CAPTURE_SERVICE + CAPTURE_PATH, data=data, method=method, headers=dict(
        {"Authorization": "Bearer " + _capture_token(), "Accept": "application/json"},
        **({"Content-Type": "application/json"} if data is not None else {})))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except OSError:
        return 0, {}
    try:
        return code, json.loads(body or b"{}")
    except ValueError:
        return code, {}


def _iso_now(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + offset))


def _epoch(iso):
    try:
        return calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _import_running():
    p = RUN.get("proc")
    return p is not None and p.poll() is None


def _capture_collect(st):
    """New assets since the cursor go to pending (uploaded after the switch went on, taken recently, not an import)."""
    known = set(st["pending"]) | {a for b in st["batches"].values() for a in b.get("assets", [])}
    edge = set(st.get("edge") or [])
    newest, newest_ids = st["cursor"], set(edge)
    page, added = 1, 0
    while page and page <= 20:
        body = _api("POST", "/search/metadata", {"createdAfter": st["cursor"], "withDeleted": False,
                                                  "visibility": "timeline", "size": 250, "page": page})
        assets = (body.get("assets") or {}) if isinstance(body, dict) else {}
        for a in assets.get("items") or []:
            aid, created = a.get("id"), a.get("createdAt") or ""
            if not aid or aid in known or aid in edge:
                continue
            if created > newest:
                newest, newest_ids = created, {aid}
            elif created == newest:
                newest_ids.add(aid)
            taken = a.get("fileCreatedAt") or created
            if a.get("isTrashed") or _import_running() or _epoch(created) - _epoch(taken) > CAPTURE_RECENT_S:
                continue
            st["pending"][aid] = {"created": created, "seen": time.time()}
            known.add(aid)
            added += 1
        nxt = assets.get("nextPage")
        page = int(nxt) if nxt and str(nxt).isdigit() else 0
    st["cursor"], st["edge"] = newest, sorted(newest_ids)[:500]
    if added:
        st["last_new"] = time.time()
    return added


def _capture_send(st):
    """Hand every unsent batch to the Service; make a new batch once the quiet period is over."""
    if st["pending"] and time.time() - st["last_new"] >= CAPTURE_QUIET_S:
        ids = sorted(st["pending"], key=lambda k: (st["pending"][k]["created"], k))[:CAPTURE_BATCH_MAX]
        bid = "pb_" + secrets.token_hex(6)
        st["batches"][bid] = {"assets": ids, "at": _iso_now(), "sent": False, "log": [], "reverted": False}
        for k in ids:
            st["pending"].pop(k, None)
    for bid, b in sorted(st["batches"].items(), key=lambda kv: kv[1].get("at", "")):
        if b.get("sent"):
            continue
        code, ans = _capture_service("POST", {"event": "items_arrived", "data": {
            "source": "photos", "batch": bid, "count": len(b["assets"]), "at": b["at"]}})
        if code in (200, 202) and ans.get("accepted"):
            b["sent"], b["service_batch"] = True, ans.get("batch") or ""
        else:
            break   # the Service is busy, restarting or switched off: the same batch goes again next time


def _capture_revert(st, batches):
    """Batches the owner undid on the Service: take away exactly what this app added (tags, albums, processed)."""
    undone = {b.get("app_batch") for b in batches or [] if isinstance(b, dict) and b.get("undone_at")}
    for bid in sorted(undone & set(st["batches"])):
        b = st["batches"][bid]
        if b.get("reverted"):
            continue
        problems = []
        for e in reversed(b.get("log") or []):
            if e.get("reverted"):
                continue
            try:
                gone = [t["id"] for t in e.get("tags_added") or []]
                for t in gone:
                    _api("DELETE", "/tags/%s/assets" % t, {"ids": [e["asset"]]})
                if gone and not _settle_tags(e["asset"], absent=gone, wait=CAPTURE_SETTLE_S):
                    raise RuntimeError("tags came back")
                al = e.get("album") or {}
                if al.get("added"):
                    _api("DELETE", "/albums/%s/assets" % al["id"], {"ids": [e["asset"]]})
                if al.get("created"):
                    left = _api("GET", "/albums/%s" % al["id"])
                    if isinstance(left, dict) and not left.get("assetCount"):
                        _api("DELETE", "/albums/%s" % al["id"])
                for t in e.get("tags_made") or []:
                    # A tag this batch made is taken away again once no photo has it (deepest first).
                    used = _api("POST", "/search/metadata", {"tagIds": [t["id"]], "size": 1, "withDeleted": True})
                    if not ((used.get("assets") or {}).get("items") if isinstance(used, dict) else None):
                        _api("DELETE", "/tags/%s" % t["id"])
                e["reverted"] = True
            except Exception as ex:  # noqa: BLE001 - kept for the next look, never lost
                problems.append("%s: %s" % (e.get("asset"), ex))
        b["reverted"] = not problems
        b["revert_problems"] = problems[:10]


def capture_tick():
    """One look: the switch (the Service's probe), undone batches, new photos, batches due."""
    if not _capture_token() or not _key():
        return
    with CAPTURE_LOCK:
        st = _capture_state()
        code, probe = _capture_service("GET")
        if code == 404 or (code == 200 and probe.get("on") is not True):
            if st["active"]:
                st.update(active=False, pending={}, cursor="", edge=[])
                _write_json("capture.json", st)
                _ensure_agent_key()   # back to the plain scopes
            return
        if code != 200:
            return
        if not st["active"]:
            # Switched on now: what is already in the library is the owner's own, never handed over.
            st.update(active=True, cursor=_iso_now(), edge=[], pending={}, last_new=0)
            _write_json("capture.json", st)
            _ensure_agent_key()   # adds the undo scopes (album out, album made) while the inbox is on
            return
        _capture_revert(st, probe.get("batches"))
        _capture_collect(st)
        _capture_send(st)
        if len(st["batches"]) > 200:
            for old in sorted(st["batches"], key=lambda k: st["batches"][k].get("at", ""))[:-200]:
                st["batches"].pop(old)
        _write_json("capture.json", st)


def _capture_loop():
    while True:
        try:
            capture_tick()
        except Exception:  # noqa: BLE001 - Immich restarting or the Service away: look again next time
            pass
        time.sleep(CAPTURE_POLL_S)


STARTUP.append(lambda: threading.Thread(target=_capture_loop, daemon=True).start())


def _capture_on():
    return bool(_capture_token()) and _capture_state().get("active") is True


def _capture_batch(batch):
    st = _capture_state()
    b = st["batches"].get(str(batch or "")) if BATCH_RE.match(str(batch or "")) else None
    if not b:
        raise ValueError("No Photos capture batch %r. Recent: %s" % (batch, ", ".join(sorted(st["batches"])[-5:]) or "none"))
    return st, b


def _files_path(original):
    p = str(original or "")
    if not p.startswith("/data/"):
        return ""
    rel = p[len("/data/"):]
    if ".." in rel.split("/"):
        return ""
    return "Photos/Library/" + rel


def capture_batch(batch=""):
    if not batch:
        st = _capture_state()
        recent = sorted(st["batches"].items(), key=lambda kv: kv[1].get("at", ""))[-10:]
        return {"active": st["active"], "pending": len(st["pending"]), "batches": [
            {"batch": k, "at": b.get("at"), "count": len(b.get("assets", [])), "sent": b.get("sent"),
             "undone": b.get("reverted")} for k, b in reversed(recent)]}
    st, b = _capture_batch(batch)
    own, items = _own_url(), []
    done = {e["asset"]: e for e in b.get("log") or [] if not e.get("reverted")}
    for aid in b["assets"]:
        try:
            a = _api("GET", "/assets/%s" % aid)
        except RuntimeError as ex:
            items.append({"asset_id": aid, "problem": str(ex)})
            continue
        row = {"asset_id": aid, "file_name": a.get("originalFileName"), "type": a.get("type"),
               "taken": a.get("fileCreatedAt"), "arrived": a.get("createdAt"),
               "files_path": _files_path(a.get("originalPath")), "trashed": bool(a.get("isTrashed")),
               "state": "processed" if aid in done else "waiting"}
        if own:
            row["open"] = own + "/photos/" + aid
        if aid in done:
            e = done[aid]
            row["done"] = {"judged_as": e.get("judged_as"), "tags": [t["name"] for t in e.get("tags_added") or []],
                           "album": (e.get("album") or {}).get("name")}
        items.append(row)
    return {"batch": batch, "service_batch": b.get("service_batch", ""), "items": items,
            "how": "Look at each original (files_path is inside the owner's Files) and handle it by the capture rules: "
                   "photos_capture_file for every photo (tags, album; marks it processed); for document photos also "
                   "wizard_capture action copy (from = files_path) and note. Originals never move."}


def _tag_values():
    out = _api("GET", "/tags")
    return {t.get("value"): t.get("id") for t in out if isinstance(t, dict)} if isinstance(out, list) else {}


def _asset_tags(asset_id):
    a = _api("GET", "/assets/%s" % asset_id)
    return {t.get("id") for t in (a.get("tags") or []) if isinstance(t, dict)}


def _settle_tags(asset_id, present=(), absent=(), rounds=6, wait=2.0):
    """Immich writes tags to the photo's sidecar and reads the sidecar back a moment later, which can undo a tag change
    made in between. Look again until the tags hold: re-add what is missing, take away what came back."""
    present, absent = set(present), set(absent)
    clean = 0
    for _ in range(rounds):
        time.sleep(wait)
        have = _asset_tags(asset_id)
        missing, extra = present - have, absent & have
        if missing:
            _api("PUT", "/tags/assets", {"assetIds": [asset_id], "tagIds": sorted(missing)})
        for t in extra:
            _api("DELETE", "/tags/%s/assets" % t, {"ids": [asset_id]})
        clean = clean + 1 if not (missing or extra) else 0
        if clean >= 2:
            return True
    return False


def capture_file(batch, asset_id, judged_as="", rule="", tags=None, album=""):
    with CAPTURE_LOCK:
        st, b = _capture_batch(batch)
        if asset_id not in b["assets"]:
            raise ValueError("Batch %s has no photo %r. Photos: %s" % (batch, asset_id, ", ".join(b["assets"])))
        if b.get("reverted"):
            raise ValueError("That batch was undone; nothing more is done with it.")
        names = []
        for t in (tags or [])[:10]:
            t = re.sub(r"\s+", " ", str(t)).strip().strip("/")
            if t and len(t) <= 80 and t not in names and t != PROCESSED_TAG:
                names.append(t)
        names.append(PROCESSED_TAG)
        before_tags = _tag_values()
        _api("PUT", "/tags", {"tags": names})
        after_tags = _tag_values()
        made_tags = sorted((v for v in after_tags if v not in before_tags), key=lambda v: -v.count("/"))
        had = _asset_tags(asset_id)
        want = {after_tags[n] for n in names if after_tags.get(n)}
        new = sorted(want - had)
        if new:
            # One change for all tags, so Immich's sidecar holds them all at once.
            _api("PUT", "/tags/assets", {"assetIds": [asset_id], "tagIds": new})
        added = [{"id": t, "name": next(n for n in names if after_tags.get(n) == t)} for t in new]
        al = {}
        album = re.sub(r"\s+", " ", str(album or "")).strip()[:120]
        if album:
            existing = next((x for x in _api("GET", "/albums") or [] if x.get("albumName") == album), None)
            if existing:
                res = _api("PUT", "/albums/%s/assets" % existing["id"], {"ids": [asset_id]})
                ok = any(r.get("id") == asset_id and r.get("success") for r in res if isinstance(r, dict))
                al = {"id": existing["id"], "name": album, "created": False, "added": ok}
            else:
                made = _api("POST", "/albums", {"albumName": album, "assetIds": [asset_id]})
                al = {"id": made.get("id"), "name": album, "created": True, "added": True}
        b["log"].append({"asset": asset_id, "judged_as": str(judged_as)[:80], "rule": str(rule)[:200],
                         "tags_added": added, "tags_made": [{"id": after_tags[v], "value": v} for v in made_tags],
                         "album": al, "at": _iso_now()})
        _write_json("capture.json", st)
    if new:
        _settle_tags(asset_id, present=new, wait=CAPTURE_SETTLE_S)
    return {"asset_id": asset_id, "processed": True, "tags_added": [t["name"] for t in added],
            "album": al.get("name") or "", "album_created": bool(al.get("created"))}

@tool("photos_capture_batch", "Capture inbox: the new photos of one Photos batch (the batch id from the capture "
      "message): each photo's asset_id, file name, when it was taken, files_path (its original inside the owner's "
      "Files, to look at with vision or copy with wizard_capture action copy) and whether it was handled. Without "
      "batch: the recent batches.", {"batch": {"type": "string", "description": "pb_... from the capture message"}},
      switch=CAPTURE_GATE)
def photos_capture_batch(batch=""):
    _box_only()
    return capture_batch(batch)


@tool("photos_capture_file", "Capture inbox: handle one photo of a batch by the owner's capture rules. It stays in "
      "Photos: adds the tags you give (people, places, events; e.g. 'Tokyo 2026'), adds it to an album (made when "
      "missing), and marks it processed (tag Wizard/Processed). Every step is logged so the owner's 'undo that' takes it "
      "away again. Call it once for every photo of the batch, document photos too (after copying them to Files).",
      {"batch": {"type": "string"}, "asset_id": {"type": "string"},
       "judged_as": {"type": "string", "description": "What you judged it to be: personal photo, book page, receipt ..."},
       "rule": {"type": "string", "description": "The rule you applied, in the owner's words, or 'best guess'"},
       "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
       "album": {"type": "string", "description": "Album name for the event or trip, if the rules call for one"}},
      required=("batch", "asset_id"), switch=CAPTURE_GATE)
def photos_capture_file(batch, asset_id, judged_as="", rule="", tags=None, album=""):
    _box_only()
    return capture_file(batch, asset_id, judged_as, rule, tags, album)
# ---- end capture inbox ----


# ---- monthly print review (app switch print-review, humanitylabs-org/photos#2) ----
# Once a month the owner picks a few photos of last month to print (for the grandparents): Photos suggests a small
# shortlist from that month, the owner takes photos out or puts others in, adds a short month note and saves the
# approved set. It is kept on this box (STATE_DIR/print-review.json), per month. Photos has no ordering, mailing or
# payment code at all: printing stays a separate step the owner takes himself. Switch off: no route, no tool, no
# status field; a saved file stays untouched until the switch is on again.
PRINT_SWITCH = "print-review"
PRINT_SHORTLIST = 8     # suggested set
PRINT_POOL = 24         # candidates shown to pick from
PRINT_MAX = 30          # most photos in one saved set
PRINT_NOTE_MAX = 280
PRINT_SCAN_PAGES = 4    # up to 2,000 photos of one month are looked at
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\Z")
SCREENSHOT_RE = re.compile(r"screen[ _-]?shot|^scr_|^screen_", re.I)
PRINT_LOCK = threading.Lock()
NEVER_ORDERS = ("Photos never orders, mails or pays for prints. Printing is a separate step you take yourself, "
                "with the photos you approved here.")
PRINT_EMPTY = ("Your library is empty, so there is nothing to choose from yet. First back up your phone (Phone backup "
               "in the Wizard OS app, or the Immich app) or bring your photos over with Import from Google.")


def _prev_month(now=None):
    t = time.gmtime(time.time() if now is None else now)
    y, m = (t.tm_year, t.tm_mon - 1) if t.tm_mon > 1 else (t.tm_year - 1, 12)
    return "%04d-%02d" % (y, m)


def _month_range(month):
    m = MONTH_RE.match(str(month or ""))
    if not m:
        raise ValueError("month is YYYY-MM, e.g. 2026-10")
    y, mo = int(m.group(1)), int(m.group(2))
    return ("%04d-%02d-01T00:00:00.000Z" % (y, mo),
            "%04d-%02d-%02dT23:59:59.999Z" % (y, mo, calendar.monthrange(y, mo)[1]))


def _month_label(month):
    y, mo = month.split("-")
    return "%s %s" % (calendar.month_name[int(mo)], y)


def _print_saved():
    d = _read_json("print-review.json", None)
    months = d.get("months") if isinstance(d, dict) else None
    return months if isinstance(months, dict) else {}


def _printable(a):
    """A photo worth printing: an image, still in the library, not a screenshot or a tiny saved picture."""
    if a.get("type") != "IMAGE" or a.get("isTrashed") or a.get("isArchived"):
        return False
    if a.get("visibility") not in (None, "timeline"):
        return False
    name = a.get("originalFileName") or ""
    ex = a.get("exifInfo") or {}
    if SCREENSHOT_RE.search(name) or name.lower().endswith(".gif"):
        return False
    if name.lower().endswith(".png") and not (ex.get("make") or ex.get("model")):
        return False  # a saved image or screenshot, not a camera photo
    return True


def _print_score(a):
    s = 0.0
    ex = a.get("exifInfo") or {}
    if a.get("isFavorite"):
        s += 4
    if (ex.get("rating") or 0) >= 4:
        s += 2
    named = [p for p in a.get("people") or [] if (p.get("name") or "").strip()]
    if named:
        s += 2 + 0.5 * min(len(named), 3)
    elif a.get("people"):
        s += 1
    if ex.get("make") or ex.get("model"):
        s += 0.5
    w, h = ex.get("exifImageWidth") or 0, ex.get("exifImageHeight") or 0
    if w and h and min(w, h) < 1000:
        s -= 2  # too small for a good print
    return s


def _taken(a):
    return a.get("localDateTime") or a.get("fileCreatedAt") or ""


def _print_rank(items):
    """Best photos first, spread over the month: each round takes the best photo of every day not used yet, and a
    photo taken within two minutes of one already picked (a burst) waits until the end."""
    days = {}
    for a in items:
        days.setdefault(_taken(a)[:10], []).append(a)
    for d in days.values():
        d.sort(key=lambda a: (-_print_score(a), _taken(a)))
    # Days by their best photo; days that score the same are taken far apart (not the first week of the month).
    left, order = sorted(days), []
    while left:
        top = max(_print_score(days[k][0]) for k in left)
        tied = [k for k in left if _print_score(days[k][0]) >= top - 1e-9]
        near = lambda k: min((abs(_epoch(k + "T00:00:00") - _epoch(o + "T00:00:00")) for o in order), default=0)
        pick = max(tied, key=lambda k: (near(k), -tied.index(k)))
        order.append(pick)
        left.remove(pick)
    ranked, later, times = [], [], []
    for r in range(max((len(v) for v in days.values()), default=0)):
        for k in order:
            if r < len(days[k]):
                a = days[k][r]
                t = _epoch(_taken(a))
                if t and any(abs(t - x) < 120 for x in times):
                    later.append(a)
                    continue
                ranked.append(a)
                times.append(t)
    return ranked + later


def _print_month_assets(month):
    after, before = _month_range(month)
    out, page = [], 1
    while page and page <= PRINT_SCAN_PAGES:
        body = _api("POST", "/search/metadata", {"takenAfter": after, "takenBefore": before, "type": "IMAGE",
                                                  "withPeople": True, "withExif": True, "size": 500, "page": page,
                                                  "order": "asc"})
        assets = (body.get("assets") or {}) if isinstance(body, dict) else {}
        out += [a for a in assets.get("items") or [] if isinstance(a, dict) and a.get("id")]
        nxt = assets.get("nextPage")
        page = int(nxt) if nxt and str(nxt).isdigit() else 0
    return out


def _print_row(a, own, selected):
    row = {"id": a["id"], "file": a.get("originalFileName"), "taken": _taken(a),
           "people": [p.get("name") for p in (a.get("people") or []) if (p.get("name") or "").strip()],
           "favorite": bool(a.get("isFavorite")), "selected": a["id"] in selected}
    if own:
        row["open"] = "%s/photos/%s" % (own, a["id"])
    return row


def _library_empty():
    st = _api("GET", "/assets/statistics")
    return isinstance(st, dict) and not (st.get("total") or st.get("images") or st.get("videos"))


def print_review(month=""):
    month = month or _prev_month()
    _month_range(month)
    saved_all = _print_saved()
    saved = saved_all.get(month) if isinstance(saved_all.get(month), dict) else None
    doc = {"month": month, "label": _month_label(month), "previous_month": _prev_month(),
           "shortlist_size": PRINT_SHORTLIST, "max": PRINT_MAX, "note_max": PRINT_NOTE_MAX,
           "never_orders": NEVER_ORDERS, "library_empty": False, "candidates": [],
           "saved": ({"count": len(saved.get("assets") or []), "note": saved.get("note") or "",
                      "saved_at": saved.get("saved_at")} if saved else None),
           "saved_months": [{"month": k, "label": _month_label(k), "count": len(v.get("assets") or []),
                             "note": v.get("note") or "", "saved_at": v.get("saved_at")}
                            for k, v in sorted(saved_all.items(), reverse=True) if MONTH_RE.match(k) and isinstance(v, dict)]}
    if _library_empty():
        doc.update(library_empty=True, next=PRINT_EMPTY)
        return doc
    items = [a for a in _print_month_assets(month) if _printable(a)]
    ranked = _print_rank(items)
    pool = ranked[:PRINT_POOL]
    chosen = list((saved or {}).get("assets") or []) or [a["id"] for a in ranked[:PRINT_SHORTLIST]]
    by_id = {a["id"]: a for a in items}
    pool += [by_id[i] for i in chosen if i in by_id and by_id[i] not in pool]
    pool.sort(key=_taken)
    own = _own_url()
    doc["candidates"] = [_print_row(a, own, set(chosen)) for a in pool]
    doc["photos_in_month"] = len(items)
    if not items:
        doc["next"] = "No photos taken in %s. Pick another month." % doc["label"]
    elif saved:
        doc["next"] = "This is the set you approved. Change it and save again, or leave it."
    else:
        doc["next"] = ("Photos picked %d of %d photos from %s. Tap a photo to take it out or put it in, add a note, "
                       "then save the approved set." % (min(PRINT_SHORTLIST, len(ranked)), len(items), doc["label"]))
    return doc


def print_review_save(month, asset_ids, note=""):
    month = str(month or "")
    _month_range(month)
    if not isinstance(asset_ids, list) or not all(isinstance(x, str) and UUID_RE.match(x) for x in asset_ids):
        raise ValueError("assets is a list of Immich photo ids")
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        raise ValueError("Pick at least one photo for the set.")
    if len(ids) > PRINT_MAX:
        raise ValueError("At most %d photos in one month's set." % PRINT_MAX)
    note = re.sub(r"\s+", " ", str(note or "")).strip()
    if len(note) > PRINT_NOTE_MAX:
        raise ValueError("Keep the note to %d characters." % PRINT_NOTE_MAX)
    in_month = {a["id"] for a in _print_month_assets(month) if _printable(a)}
    stray = [x for x in ids if x not in in_month]
    if stray:
        raise ValueError("%d of these photos are not from %s (or no longer in the library). Reload and pick again."
                         % (len(stray), _month_label(month)))
    with PRINT_LOCK:
        months = _print_saved()
        months[month] = {"assets": ids, "note": note, "saved_at": int(time.time())}
        _write_json("print-review.json", {"months": months})
    return print_review(month)


def print_thumb(asset_id):
    if not UUID_RE.match(str(asset_id or "")):
        raise ValueError("not an Immich photo id")
    key = _key()
    if not key:
        raise RuntimeError("Photos is still setting itself up.")
    code, data = _immich("GET", "/assets/%s/thumbnail?size=thumbnail" % asset_id, key=key, raw=True, timeout=30)
    if code != 200 or not data:
        raise RuntimeError("No picture for that photo yet.")
    ctype = "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else "image/jpeg"
    return 200, ctype, data, {"Cache-Control": "private, max-age=86400"}


@tool("photos_print_review", "Monthly print review (read only): the photos the owner approved to print for a month, his "
      "note, and Photos' suggested shortlist when nothing is saved yet. Default month: last month. This never orders, "
      "mails or pays for prints, and no tool does: printing is a separate step the owner takes himself.",
      {"month": {"type": "string", "description": "YYYY-MM; default last month"}}, switch=PRINT_SWITCH)
def photos_print_review(month=""):
    _box_only()
    d = print_review(month)
    picked = [c for c in d["candidates"] if c["selected"]]
    return {"month": d["month"], "label": d["label"], "approved_by_owner": bool(d["saved"]),
            "note": (d["saved"] or {}).get("note", ""), "photos": picked, "library_empty": d["library_empty"],
            "next": d.get("next", ""), "saved_months": d["saved_months"], "never_orders": NEVER_ORDERS}
# ---- end monthly print review ----


# ---- wizard.app-switches/1 (wizard-os docs/APP_SWITCHES.md) ----
# Owner feature switches. New features ship behind one, off by default: append it to SWITCHES
# ({"id", "name", "description", "default": False, "issues": ["humanitylabs-org/photos#N"]}) and gate it on
# switch_on(id). Kept on the box (STATE_DIR/switches.json). Owner only: the box's Tailscale Serve login with
# X-Wizard-Switch: 1; never a bearer or a box-local host.
SWITCHES = [
    {"id": PRINT_SWITCH, "name": "Monthly print review",
     "description": "Once a month Photos suggests a few photos from last month to print (for the grandparents, say). "
                    "You take photos out or put others in, add a short note and save the approved set. Photos never "
                    "orders, mails or pays for prints.",
     "default": False, "issues": ["humanitylabs-org/photos#2"]},
]
_SW_LOCAL = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def switches_doc():
    saved = _read_json("switches.json", {}) or {}
    return {"contract": "wizard.app-switches/1", "switches": [
        dict(x, on=saved[x["id"]] if isinstance(saved.get(x["id"]), bool) else bool(x.get("default"))) for x in SWITCHES]}


def switch_on(switch_id):
    if switch_id == CAPTURE_GATE:   # the box's switch (wizard/capture-inbox), read from the Agent Service's probe
        return _capture_on()
    return next((x["on"] for x in switches_doc()["switches"] if x["id"] == switch_id), False)


def _sw_owner(headers, write):
    if headers.get("authorization") or headers.get("x-wizard-switch") != "1":
        return False
    host = (headers.get("host") or "").strip().lower()
    name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    if not name or name in _SW_LOCAL or "." not in name:
        return False
    origin = headers.get("origin")
    if origin and urllib.parse.urlsplit(origin).netloc.lower() != host:
        return False
    if write and not origin:
        return False
    login = headers.get("tailscale-user-login") or ""
    owners = {x.strip().lower() for x in (os.environ.get("WIZARD_OWNER_LOGINS") or "").split(",") if x.strip()}
    return "@" in login and "," not in login and (not owners or login.strip().lower() in owners)


def switches_route(method, headers, body):
    if method in ("GET", "HEAD"):
        if not _sw_owner(headers, False):
            return _json(403, {"error": "Only this Wizard's owner can see these switches.", "code": "owner_only"})
        return _json(200, switches_doc())
    if method != "POST":
        return _json(405, {"error": "method"})
    if not _sw_owner(headers, True):
        return _json(403, {"error": "Only this Wizard's owner can change these switches.", "code": "owner_only"})
    try:
        d = _body(headers, body)
        if not isinstance(d.get("id"), str) or not isinstance(d.get("on"), bool) or set(d) - {"id", "on"}:
            raise ValueError
    except (ValueError, TypeError):
        return _json(400, {"error": 'Send JSON {"id": "...", "on": true|false}', "code": "invalid_input"})
    if d["id"] not in {x["id"] for x in SWITCHES}:
        return _json(404, {"error": "No such switch", "code": "unknown_switch"})
    ids = {x["id"] for x in SWITCHES}
    saved = {k: v for k, v in (_read_json("switches.json", {}) or {}).items() if k in ids and isinstance(v, bool)}
    saved[d["id"]] = d["on"]
    _write_json("switches.json", saved)
    return _json(200, switches_doc())


def _drop_removed_switch_state():
    # Review albums (switch review-albums, humanitylabs-org/photos#6) were dropped (#14): forget their stored values.
    # Albums already made in Immich are the owner's photos and stay as they are.
    try:
        os.remove(_state_path("review-albums.json"))
    except OSError:
        pass
    saved = _read_json("switches.json", None)
    if isinstance(saved, dict) and "review-albums" in saved:
        saved.pop("review-albums")
        _write_json("switches.json", saved)


STARTUP.append(_drop_removed_switch_state)


@route("/api/switches", match=lambda p: p.rstrip("/") == "/api/switches")
def switches_bare_route(method, path, query, headers, body):
    return switches_route(method, headers, body)
# ---- end wizard.app-switches/1 ----


def _health():
    code, _ = _immich("GET", "/server/ping", timeout=3)
    return {"ok": True, "app": APP, "version": VERSION, "immich": code == 200}


@route("/healthz", match=lambda p: p == "/healthz")
def health_route(method, path, query, headers, body):
    return _json(200, _health())


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json"}
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "frame-src https: http:; connect-src 'self'; frame-ancestors 'self'")


def _page_path(p):
    """Wizard owns /photos/ and its own files; Immich keeps /photos and /photos/<asset-uuid>[/...]."""
    if not p.startswith(PAGE):
        return False
    first = p[len(PAGE):].split("/", 1)[0]
    return not UUID_RE.match(first)


@route(PAGE, match=_page_path)
def page_route(method, path, query, headers, body):
    sub = path[len(PAGE):]
    if sub in ("healthz",):
        return _json(200, _health())
    if sub in ("api/switches", "api/switches/"):
        return switches_route(method, headers, body)
    if sub.startswith("api/"):
        write = method not in ("GET", "HEAD")
        if not _trusted(headers, write):
            return _json(403, {"error": "Open Photos from your Wizard.", "code": "owner_only"})
        try:
            if method == "GET" and sub == "api/status":
                return _json(200, status(headers))
            if method == "GET" and sub == "api/import":
                return _json(200, import_status())
            if method == "GET" and sub == "api/import/options":
                return _json(200, {"options": import_options()})
            if method == "POST" and sub == "api/import/options":
                return _json(200, {"options": set_import_options(_body(headers, body))})
            if method == "POST" and sub == "api/import/start":
                _body(headers, body)
                return _json(200, import_start(True))
            if sub.startswith("api/print-review") and switch_on(PRINT_SWITCH):
                q = dict(urllib.parse.parse_qsl(query))
                if method == "GET" and sub == "api/print-review":
                    return _json(200, print_review(q.get("month", "")))
                if method == "GET" and sub == "api/print-review/thumb":
                    return print_thumb(q.get("id", ""))
                if method == "POST" and sub == "api/print-review":
                    d = _body(headers, body)
                    if set(d) - {"month", "assets", "note"}:
                        raise ValueError("send month, assets and note")
                    return _json(200, print_review_save(d.get("month"), d.get("assets"), d.get("note", "")))
        except (ValueError, RuntimeError) as e:
            return _json(400, {"error": str(e)})
        return _json(404, {"error": "not found"})
    if method not in ("GET", "HEAD"):
        return _json(405, {"error": "method"})
    name = sub or "index.html"
    if name in VENDOR:
        ext = "." + name.rsplit(".", 1)[-1]
        extra = {"Content-Security-Policy": CSP} if ext == ".html" else None
        out = (200, MIME.get(ext, "application/octet-stream"), base64.b64decode(VENDOR[name]))
        return out + ((extra,) if extra else ())
    return _json(404, {"error": "not found"})


# ---- the Wizard page (generated by mcp/inline.py from web/) ----
VENDOR = {'index.html': 'PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIiBkYXRhLWFwcD0icGhvdG9zIj4KPGhlYWQ+CiAgPG1ldGEgY2hhcnNldD0idXRmLTgiPgogIDxtZXRhIG5hbWU9InZpZXdwb3J0IiBjb250ZW50PSJ3aWR0aD1kZXZpY2Utd2lkdGgsIGluaXRpYWwtc2NhbGU9MSwgdmlld3BvcnQtZml0PWNvdmVyIj4KICA8bWV0YSBuYW1lPSJjb2xvci1zY2hlbWUiIGNvbnRlbnQ9ImRhcmsgbGlnaHQiPgogIDx0aXRsZT5QaG90b3M8L3RpdGxlPgogIDxzY3JpcHQgc3JjPSJib290LmpzIj48L3NjcmlwdD4KICA8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9IndpemFyZC10b2tlbnMuY3NzIj4KICA8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9IndpemFyZC11aS5jc3MiPgogIDxsaW5rIHJlbD0ic3R5bGVzaGVldCIgaHJlZj0ic3RhbmRhbG9uZS5jc3MiPgogIDxsaW5rIHJlbD0ic3R5bGVzaGVldCIgaHJlZj0ic3R5bGVzLmNzcyI+CiAgPHNjcmlwdCBkZWZlciBzcmM9Ii93aXphcmQtb3MtaG9tZS9jb3JlL2FwcC1jbGllbnQuanMiPjwvc2NyaXB0PgogIDxzY3JpcHQgZGVmZXIgc3JjPSJ3aXphcmQtc2hlZXQuanMiPjwvc2NyaXB0PgogIDxzY3JpcHQgZGVmZXIgc3JjPSJwaG90b3MuanMiPjwvc2NyaXB0PgogIDxzY3JpcHQgZGVmZXIgc3JjPSJhcHAuanMiPjwvc2NyaXB0Pgo8L2hlYWQ+Cjxib2R5PgogIDxkaXYgY2xhc3M9Ind6LWFwcCBwaC1hcHAiPgogICAgPGhlYWRlciBjbGFzcz0id3otaGVhZCBwaC1oZWFkIj4KICAgICAgPGgxPlBob3RvczwvaDE+CiAgICAgIDxwIGlkPSJsaW5lIj5Mb2FkaW5n4oCmPC9wPgogICAgICA8bmF2IGNsYXNzPSJwaC1hY3Rpb25zIiBhcmlhLWxhYmVsPSJQaG90b3MgYWN0aW9ucyI+CiAgICAgICAgPGJ1dHRvbiBpZD0ib3Blbi1iYWNrdXAiIGNsYXNzPSJ3ei1jaGlwIiB0eXBlPSJidXR0b24iIGhpZGRlbj5QaG9uZSBiYWNrdXA8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGlkPSJvcGVuLWltcG9ydCIgY2xhc3M9Ind6LWNoaXAiIHR5cGU9ImJ1dHRvbiI+SW1wb3J0IGZyb20gR29vZ2xlPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBpZD0ib3Blbi1wcmludHMiIGNsYXNzPSJ3ei1jaGlwIiB0eXBlPSJidXR0b24iIGhpZGRlbj5QcmludCByZXZpZXc8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGlkPSJvcGVuLWFnZW50IiBjbGFzcz0id3otY2hpcCIgdHlwZT0iYnV0dG9uIj5BZ2VudDwvYnV0dG9uPgogICAgICAgIDxhIGlkPSJvcGVuLWZ1bGwiIGNsYXNzPSJ3ei1jaGlwIiB0YXJnZXQ9Il9ibGFuayIgcmVsPSJub29wZW5lciIgaGlkZGVuPk9wZW4gZnVsbCBzY3JlZW4g4oaXPC9hPgogICAgICA8L25hdj4KICAgIDwvaGVhZGVyPgogICAgPGJ1dHRvbiBpZD0ib3Blbi1tZW51IiBjbGFzcz0icGgtbW9yZSIgdHlwZT0iYnV0dG9uIiBhcmlhLWxhYmVsPSJQaG90b3MgbWVudSIgYXJpYS1oYXNwb3B1cD0iZGlhbG9nIiBhcmlhLWNvbnRyb2xzPSJtZW51LXNoZWV0Ij48c3ZnIHZpZXdCb3g9IjAgMCAyNCAyNCIgYXJpYS1oaWRkZW49InRydWUiPjxjaXJjbGUgY3g9IjUiIGN5PSIxMiIgcj0iMS45Ii8+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMS45Ii8+PGNpcmNsZSBjeD0iMTkiIGN5PSIxMiIgcj0iMS45Ii8+PC9zdmc+PC9idXR0b24+CiAgICA8c2VjdGlvbiBpZD0ic2V0dXAiIGNsYXNzPSJ3ei1jYXJkIHBoLXNldHVwIiBoaWRkZW4+PC9zZWN0aW9uPgogICAgPGRpdiBpZD0iZnJhbWUtd3JhcCIgY2xhc3M9InBoLWZyYW1lLXdyYXAiIGhpZGRlbj4KICAgICAgPGlmcmFtZSBpZD0iaW1taWNoIiB0aXRsZT0iSW1taWNoIiBhbGxvdz0iZnVsbHNjcmVlbjsgY2xpcGJvYXJkLXJlYWQ7IGNsaXBib2FyZC13cml0ZSIgcmVmZXJyZXJwb2xpY3k9InNhbWUtb3JpZ2luIj48L2lmcmFtZT4KICAgIDwvZGl2PgogICAgPHAgaWQ9InN0YXR1cyIgY2xhc3M9Ind6LXN0YXR1cyIgcm9sZT0ic3RhdHVzIiBhcmlhLWxpdmU9InBvbGl0ZSI+PC9wPgogIDwvZGl2PgoKICA8ZGlhbG9nIGlkPSJtZW51LXNoZWV0IiBjbGFzcz0id3otc2hlZXQiIGFyaWEtbGFiZWxsZWRieT0ibWVudS1oIj4KICAgIDxoMiBpZD0ibWVudS1oIj5QaG90b3M8L2gyPgogICAgPHAgaWQ9Im1lbnUtbGluZSIgY2xhc3M9InBoLWhlbHAiPjwvcD4KICAgIDx1bCBjbGFzcz0id3otbGlzdCBwaC1tZW51IiBhcmlhLWxhYmVsPSJQaG90b3MgYWN0aW9ucyI+CiAgICAgIDxsaSBoaWRkZW4+PGJ1dHRvbiBjbGFzcz0id3otcm93IiB0eXBlPSJidXR0b24iIGRhdGEtbWVudT0ib3Blbi1iYWNrdXAiPjxzcGFuIGNsYXNzPSJ3ei1tYWluIj48Yj5QaG9uZSBiYWNrdXA8L2I+PC9zcGFuPjwvYnV0dG9uPjwvbGk+CiAgICAgIDxsaT48YnV0dG9uIGNsYXNzPSJ3ei1yb3ciIHR5cGU9ImJ1dHRvbiIgZGF0YS1tZW51PSJvcGVuLWltcG9ydCI+PHNwYW4gY2xhc3M9Ind6LW1haW4iPjxiPkltcG9ydCBmcm9tIEdvb2dsZTwvYj48L3NwYW4+PC9idXR0b24+PC9saT4KICAgICAgPGxpIGhpZGRlbj48YnV0dG9uIGNsYXNzPSJ3ei1yb3ciIHR5cGU9ImJ1dHRvbiIgZGF0YS1tZW51PSJvcGVuLXByaW50cyI+PHNwYW4gY2xhc3M9Ind6LW1haW4iPjxiPlByaW50IHJldmlldzwvYj48L3NwYW4+PC9idXR0b24+PC9saT4KICAgICAgPGxpPjxidXR0b24gY2xhc3M9Ind6LXJvdyIgdHlwZT0iYnV0dG9uIiBkYXRhLW1lbnU9Im9wZW4tYWdlbnQiPjxzcGFuIGNsYXNzPSJ3ei1tYWluIj48Yj5BZ2VudDwvYj48L3NwYW4+PC9idXR0b24+PC9saT4KICAgICAgPGxpIGhpZGRlbj48YSBjbGFzcz0id3otcm93IiBkYXRhLW1lbnU9Im9wZW4tZnVsbCIgdGFyZ2V0PSJfYmxhbmsiIHJlbD0ibm9vcGVuZXIiPjxzcGFuIGNsYXNzPSJ3ei1tYWluIj48Yj5PcGVuIGZ1bGwgc2NyZWVuIOKGlzwvYj48L3NwYW4+PC9hPjwvbGk+CiAgICA8L3VsPgogIDwvZGlhbG9nPgoKICA8ZGlhbG9nIGlkPSJiYWNrdXAtc2hlZXQiIGNsYXNzPSJ3ei1zaGVldCIgYXJpYS1sYWJlbGxlZGJ5PSJiYWNrdXAtaCI+CiAgICA8aDIgaWQ9ImJhY2t1cC1oIj5CYWNrIHVwIHRoaXMgcGhvbmU8L2gyPgogICAgPGRpdiBpZD0iYmFja3VwLXJvb3QiIGFyaWEtbGl2ZT0icG9saXRlIj48L2Rpdj4KICA8L2RpYWxvZz4KCiAgPGRpYWxvZyBpZD0iaW1wb3J0LXNoZWV0IiBjbGFzcz0id3otc2hlZXQiIGFyaWEtbGFiZWxsZWRieT0iaW1wb3J0LWgiPgogICAgPGgyIGlkPSJpbXBvcnQtaCI+SW1wb3J0IGZyb20gR29vZ2xlIFBob3RvczwvaDI+CiAgICA8b2wgY2xhc3M9InBoLXN0ZXBzIj4KICAgICAgPGxpPk9uIGEgY29tcHV0ZXIsIG9wZW4gPGEgaHJlZj0iaHR0cHM6Ly90YWtlb3V0Lmdvb2dsZS5jb20iIHRhcmdldD0iX2JsYW5rIiByZWw9Im5vb3BlbmVyIj50YWtlb3V0Lmdvb2dsZS5jb208L2E+LCB0YXAgPGI+RGVzZWxlY3QgYWxsPC9iPiwgdGhlbiB0aWNrIG9ubHkgPGI+R29vZ2xlIFBob3RvczwvYj4uPC9saT4KICAgICAgPGxpPk5leHQgc3RlcDogPGI+U2VuZCBkb3dubG9hZCBsaW5rIHZpYSBlbWFpbDwvYj4sIDxiPkV4cG9ydCBvbmNlPC9iPiwgZmlsZSB0eXBlIDxiPi56aXA8L2I+LCBzaXplIDxiPjUwIEdCPC9iPi4gQ3JlYXRlIGV4cG9ydC48L2xpPgogICAgICA8bGk+V2hlbiBHb29nbGUncyBlbWFpbCBhcnJpdmVzLCBkb3dubG9hZCBldmVyeSBwYXJ0IGFuZCBwdXQgdGhlbSBhbGwgaW4gPGI+RmlsZXMg4oC6IFBob3RvcyDigLogSW1wb3J0PC9iPi48L2xpPgogICAgICA8bGk+VGFwIDxiPkltcG9ydCBub3c8L2I+LiBEYXRlcywgcGxhY2VzLCBkZXNjcmlwdGlvbnMgYW5kIGFsYnVtcyBjb21lIGFjcm9zcy4gUnVubmluZyBpdCBhZ2FpbiBpcyBzYWZlLjwvbGk+CiAgICA8L29sPgogICAgPGRpdiBpZD0iaW1wb3J0LXN0YXRlIiBjbGFzcz0icGgtaW1wb3J0LXN0YXRlIj48L2Rpdj4KICAgIDx1bCBjbGFzcz0id3otbGlzdCIgaWQ9ImltcG9ydC1vcHRpb25zIiBhcmlhLWxhYmVsPSJJbXBvcnQgb3B0aW9ucyI+PC91bD4KICAgIDxkaXYgY2xhc3M9Ind6LWFjdGlvbnMiPjxidXR0b24gaWQ9ImltcG9ydC1nbyIgY2xhc3M9Ind6LWJ1dHRvbiIgdHlwZT0iYnV0dG9uIiBkaXNhYmxlZD5JbXBvcnQgbm93PC9idXR0b24+PC9kaXY+CiAgICA8cHJlIGlkPSJpbXBvcnQtbG9nIiBjbGFzcz0icGgtbG9nIiBoaWRkZW4+PC9wcmU+CiAgPC9kaWFsb2c+CgogIDxkaWFsb2cgaWQ9InByaW50LXNoZWV0IiBjbGFzcz0id3otc2hlZXQiIGFyaWEtbGFiZWxsZWRieT0icHJpbnQtaCI+CiAgICA8aDIgaWQ9InByaW50LWgiPlByaW50IHJldmlldzwvaDI+CiAgICA8ZGl2IGNsYXNzPSJwaC1tb250aCIgcm9sZT0iZ3JvdXAiIGFyaWEtbGFiZWw9Ik1vbnRoIj4KICAgICAgPGJ1dHRvbiBpZD0icHJpbnQtcHJldiIgY2xhc3M9Ind6LWJ1dHRvbiBxdWlldCIgdHlwZT0iYnV0dG9uIiBhcmlhLWxhYmVsPSJNb250aCBiZWZvcmUiPuKAuTwvYnV0dG9uPgogICAgICA8YiBpZD0icHJpbnQtbW9udGgiIGFyaWEtbGl2ZT0icG9saXRlIj48L2I+CiAgICAgIDxidXR0b24gaWQ9InByaW50LW5leHQiIGNsYXNzPSJ3ei1idXR0b24gcXVpZXQiIHR5cGU9ImJ1dHRvbiIgYXJpYS1sYWJlbD0iTW9udGggYWZ0ZXIiPuKAujwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8cCBpZD0icHJpbnQtbGluZSIgY2xhc3M9InBoLWhlbHAiPjwvcD4KICAgIDx1bCBpZD0icHJpbnQtZ3JpZCIgY2xhc3M9InBoLXByaW50LWdyaWQiIGFyaWEtbGFiZWw9IlBob3RvcyB0byBwcmludCI+PC91bD4KICAgIDxkaXYgaWQ9InByaW50LWZvcm0iIGhpZGRlbj4KICAgICAgPHAgaWQ9InByaW50LWNvdW50IiBjbGFzcz0icGgtaW1wb3J0LXN0YXRlIj48L3A+CiAgICAgIDxsYWJlbCBjbGFzcz0icGgtcHJpbnQtbm90ZSI+Tm90ZSBmb3IgdGhpcyBtb250aAogICAgICAgIDx0ZXh0YXJlYSBpZD0icHJpbnQtbm90ZSIgY2xhc3M9Ind6LWZpZWxkIiByb3dzPSIyIiBtYXhsZW5ndGg9IjI4MCIgcGxhY2Vob2xkZXI9IlNvZmlhJ3MgZmlyc3Qgc3RlcHMsIHRoZSBiZWFjaCB3ZWVrZW5k4oCmIj48L3RleHRhcmVhPgogICAgICA8L2xhYmVsPgogICAgICA8ZGl2IGNsYXNzPSJ3ei1hY3Rpb25zIj48YnV0dG9uIGlkPSJwcmludC1zYXZlIiBjbGFzcz0id3otYnV0dG9uIiB0eXBlPSJidXR0b24iPlNhdmUgYXBwcm92ZWQgc2V0PC9idXR0b24+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxwIGlkPSJwcmludC1uZXZlciIgY2xhc3M9InBoLWhlbHAgcGgtcHJpbnQtbmV2ZXIiPjwvcD4KICAgIDx1bCBpZD0icHJpbnQtc2F2ZWQiIGNsYXNzPSJ3ei1saXN0IiBhcmlhLWxhYmVsPSJTYXZlZCBtb250aHMiPjwvdWw+CiAgPC9kaWFsb2c+CgogIDxkaWFsb2cgaWQ9ImFnZW50LXNoZWV0IiBjbGFzcz0id3otc2hlZXQiIGFyaWEtbGFiZWxsZWRieT0iYWdlbnQtaCI+CiAgICA8aDIgaWQ9ImFnZW50LWgiPllvdXIgYWdlbnQgYW5kIFBob3RvczwvaDI+CiAgICA8cCBpZD0iYWdlbnQtbGluZSIgY2xhc3M9InBoLWhlbHAiPjwvcD4KICAgIDxwIGNsYXNzPSJwaC1oZWxwIj5XaXRoIHlvdXIgeWVzLCB0aGUgYWdlbnQgY2FuIGNvdW50IGFuZCBmaW5kIHBob3RvcyAoYnkgcGVyc29uLCBkYXRlLCBwbGFjZSBvciB3aGF0IGlzIGluIHRoZW0pLCBzZWUgZmFjZSBncm91cHMsIHJ1biB0aGUgR29vZ2xlIGltcG9ydCBhbmQgbW92ZSBwaG90b3MgeW91IGNob29zZSB0byBJbW1pY2gncyB0cmFzaC4gSXQgY2Fubm90IGVtcHR5IHRoZSB0cmFzaCBvciBkZWxldGUgYW55dGhpbmcgcGVybWFuZW50bHkuPC9wPgogICAgPHAgY2xhc3M9InBoLWhlbHAiPlRoZXJlIGlzIG5vIHNpZ24taW46IHlvdXIgVGFpbHNjYWxlIGlzIHRoZSBsb2NrLiBPbmx5IGRldmljZXMgb24geW91ciB0YWlsbmV0IGNhbiBvcGVuIFBob3RvcywgaGVyZSBvciBpbiB0aGUgSW1taWNoIHBob25lIGFwcC48L3A+CiAgICA8cCBpZD0icGhvbmUtbGluZSIgY2xhc3M9InBoLWhlbHAiPjwvcD4KICA8L2RpYWxvZz4KPC9ib2R5Pgo8L2h0bWw+Cg==', 'app.js': 'J3VzZSBzdHJpY3QnOwpjb25zdCBlbCA9IGlkID0+IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKTsKbGV0IHN0YXRlID0gbnVsbDsKCmZ1bmN0aW9uIHNheSh0ZXh0LCBraW5kID0gJycpIHsgLy8ga2luZDogJycgKHNjcmVlbiByZWFkZXJzIG9ubHkpLCAnZXJyb3InLCAnbm90ZScKICBjb25zdCBzID0gZWwoJ3N0YXR1cycpOwogIHMuY2xhc3NMaXN0LnRvZ2dsZSgnd3otc3InLCAha2luZCk7CiAgcy5jbGFzc0xpc3QudG9nZ2xlKCdub3RlJywga2luZCA9PT0gJ25vdGUnKTsKICBzLnRleHRDb250ZW50ID0gdGV4dDsKfQoKYXN5bmMgZnVuY3Rpb24gcmVxdWVzdChwYXRoLCBkYXRhKSB7CiAgY29uc3QgaW5pdCA9IGRhdGEgPT09IHVuZGVmaW5lZCA/IHsgY3JlZGVudGlhbHM6ICdzYW1lLW9yaWdpbicgfQogICAgOiB7IG1ldGhvZDogJ1BPU1QnLCBjcmVkZW50aWFsczogJ3NhbWUtb3JpZ2luJywgaGVhZGVyczogeyAnQ29udGVudC1UeXBlJzogJ2FwcGxpY2F0aW9uL2pzb24nIH0sIGJvZHk6IEpTT04uc3RyaW5naWZ5KGRhdGEpIH07CiAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBmZXRjaChwYXRoLCBpbml0KTsKICBsZXQgYm9keSA9IHt9OwogIHRyeSB7IGJvZHkgPSBhd2FpdCByZXNwb25zZS5qc29uKCk7IH0gY2F0Y2ggKGUpIHt9CiAgaWYgKCFyZXNwb25zZS5vaykgeyBjb25zdCBlID0gbmV3IEVycm9yKGJvZHkuZXJyb3IgfHwgJ1Bob3RvcyBjb3VsZCBub3QgZG8gdGhhdC4gVHJ5IGFnYWluLicpOyBlLmNvZGUgPSBib2R5LmNvZGU7IHRocm93IGU7IH0KICByZXR1cm4gYm9keTsKfQoKZnVuY3Rpb24gbih4KSB7IHJldHVybiB0eXBlb2YgeCA9PT0gJ251bWJlcicgPyB4LnRvTG9jYWxlU3RyaW5nKCkgOiAnMCc7IH0KCmZ1bmN0aW9uIHNldHVwQ2FyZCh0aXRsZSwgdGV4dCkgewogIGNvbnN0IGNhcmQgPSBlbCgnc2V0dXAnKTsKICBjYXJkLnJlcGxhY2VDaGlsZHJlbigpOwogIGNvbnN0IGggPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdoMicpOyBoLnRleHRDb250ZW50ID0gdGl0bGU7CiAgY29uc3QgcCA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3AnKTsgcC5jbGFzc05hbWUgPSAncGgtaGVscCc7IHAudGV4dENvbnRlbnQgPSB0ZXh0OwogIGNhcmQuYXBwZW5kKGgsIHApOwogIGNhcmQuaGlkZGVuID0gZmFsc2U7Cn0KCmZ1bmN0aW9uIHJlbmRlcihzKSB7CiAgc3RhdGUgPSBzOwogIGNvbnN0IG93biA9IHMubGlua3MgJiYgcy5saW5rcy5pbW1pY2g7CiAgY29uc3QgaW0gPSBzLmltbWljaCB8fCB7fTsKICBsZXQgbGluZSA9ICcnOwogIGlmICghaW0ucmVhY2hhYmxlKSBsaW5lID0gJ1N0YXJ0aW5nIEltbWljaOKApic7CiAgZWxzZSBpZiAoIWltLmhhc19vd25lcl9hY2NvdW50IHx8ICFzLmFnZW50X2Nvbm5lY3RlZCkgbGluZSA9ICdTZXR0aW5nIHVw4oCmJzsKICBlbHNlIGlmIChzLmxpYnJhcnkpIGxpbmUgPSBuKHMubGlicmFyeS5waG90b3MpICsgJyBwaG90b3MgwrcgJyArIG4ocy5saWJyYXJ5LnZpZGVvcykgKyAnIHZpZGVvcyDCtyAnICsgKHMubGlicmFyeS5zaXplX2diIHx8IDApICsgJyBHQic7CiAgZWxzZSBsaW5lID0gJ0ltbWljaCAnICsgKGltLnZlcnNpb24gfHwgJycpOwogIGNvbnN0IGJ1c3kgPSBzLmJhY2tncm91bmRfd29yayAmJiBPYmplY3QudmFsdWVzKHMuYmFja2dyb3VuZF93b3JrKS5yZWR1Y2UoKGEsIHEpID0+IGEgKyAocS53YWl0aW5nIHx8IDApICsgKHEuYWN0aXZlIHx8IDApLCAwKTsKICBpZiAoYnVzeSkgbGluZSArPSAnIMK3IG9yZ2FuaXNpbmcgJyArIG4oYnVzeSk7CiAgaWYgKHMuaW1wb3J0ICYmIHMuaW1wb3J0LnN0YXRlID09PSAncnVubmluZycpIGxpbmUgKz0gJyDCtyBpbXBvcnRpbmcnOwogIGVsKCdsaW5lJykudGV4dENvbnRlbnQgPSBsaW5lOwogIGVsKCdtZW51LWxpbmUnKS50ZXh0Q29udGVudCA9IGxpbmU7CgogIGVsKCdzZXR1cCcpLmhpZGRlbiA9IHRydWU7CiAgaWYgKCFvd24pIHsKICAgIHNldHVwQ2FyZCgnT3BlbiBQaG90b3MgZnJvbSB5b3VyIFdpemFyZCcsICdUaGlzIHBhZ2UgbmVlZHMgeW91ciBXaXphcmRcdTIwMTlzIHByaXZhdGUgYWRkcmVzcyB0byBzaG93IHlvdXIgbGlicmFyeS4nKTsKICB9IGVsc2UgaWYgKCFpbS5yZWFjaGFibGUpIHsKICAgIHNldHVwQ2FyZCgnUGhvdG9zIGlzIHN0YXJ0aW5nJywgJ1RoZSBmaXJzdCBzdGFydCB0YWtlcyBhIG1pbnV0ZSBvciB0d28uIFRoaXMgcGFnZSByZWZyZXNoZXMgb24gaXRzIG93bi4nKTsKICB9IGVsc2UgaWYgKCEocy5zaWduX2luICYmIHMuc2lnbl9pbi5yZWFkeSkpIHsKICAgIHNldHVwQ2FyZCgnUGhvdG9zIGlzIHNldHRpbmcgdXAnLCAocy5zaWduX2luICYmIHMuc2lnbl9pbi5wcm9ibGVtKSB8fCAnUGhvdG9zIG1ha2VzIGl0cyBvd24gbGlicmFyeSBmb3IgeW91OyB0aGVyZSBpcyBub3RoaW5nIHRvIHNpZ24gaW4gdG8uIFRoaXMgcGFnZSByZWZyZXNoZXMgb24gaXRzIG93bi4nKTsKICB9CiAgZWwoJ29wZW4tcHJpbnRzJykuaGlkZGVuID0gIXMucHJpbnRfcmV2aWV3OwogIGVsKCdvcGVuLWZ1bGwnKS5oaWRkZW4gPSAhb3duOwogIGlmIChvd24pIGVsKCdvcGVuLWZ1bGwnKS5ocmVmID0gb3duOwogIGNvbnN0IGZyYW1lID0gZWwoJ2ltbWljaCcpOwogIGlmIChvd24gJiYgaW0ucmVhY2hhYmxlICYmIHMuc2lnbl9pbiAmJiBzLnNpZ25faW4ucmVhZHkpIHsKICAgIGVsKCdmcmFtZS13cmFwJykuaGlkZGVuID0gZmFsc2U7CiAgICBpZiAoIWZyYW1lLmdldEF0dHJpYnV0ZSgnc3JjJykpIGZyYW1lLnNyYyA9IG93bjsKICB9CiAgY29uc3QgcGhvbmUgPSBzLmxpbmtzICYmIHMubGlua3MubW9iaWxlX3NlcnZlcl91cmw7CiAgZWwoJ3Bob25lLWxpbmUnKS50ZXh0Q29udGVudCA9IHBob25lID8gJ1Bob25lIGJhY2t1cDogaW5zdGFsbCB0aGUgSW1taWNoIGFwcCwgdHlwZSAnICsgcGhvbmUgKyAnIGFzIHRoZSBzZXJ2ZXIsIHRhcCBOZXh0LCB0aGVuIENvbnRpbnVlLiBLZWVwIFRhaWxzY2FsZSBvbi4nIDogJyc7CiAgaWYgKHBob25lKSBiYWNrdXAuc2VydmVyID0gcGhvbmU7CiAgZWwoJ2FnZW50LWxpbmUnKS50ZXh0Q29udGVudCA9IHMuYWdlbnRfY29ubmVjdGVkID8gJ1lvdXIgYWdlbnQgaXMgY29ubmVjdGVkIHRvIFBob3Rvcy4nIDogJ1lvdXIgYWdlbnQgY29ubmVjdHMgYnkgaXRzZWxmIG9uY2UgUGhvdG9zIGhhcyBmaW5pc2hlZCBzZXR0aW5nIHVwLic7Cn0KCmZ1bmN0aW9uIHJlbmRlckltcG9ydChkKSB7CiAgY29uc3Qgc3QgPSBkLnN0YXRlOwogIGNvbnN0IHdvcmRzID0geyBpZGxlOiAnTm8gaW1wb3J0IHlldC4nLCBydW5uaW5nOiAnSW1wb3J0aW5n4oCmIHlvdSBjYW4gY2xvc2UgdGhpczsgaXQga2VlcHMgZ29pbmcuJywgZG9uZTogJ0xhc3QgaW1wb3J0IGZpbmlzaGVkLicsCiAgICBmYWlsZWQ6ICdUaGUgbGFzdCBpbXBvcnQgc3RvcHBlZDogJyArIChkLm1lc3NhZ2UgfHwgJ3NlZSB0aGUgbG9nIGJlbG93LicpLCBpbnRlcnJ1cHRlZDogJ1RoZSBsYXN0IGltcG9ydCB3YXMgaW50ZXJydXB0ZWQuIEltcG9ydCBhZ2FpbiB0byBjb250aW51ZTsgbm90aGluZyBpcyBkdXBsaWNhdGVkLicgfTsKICBlbCgnaW1wb3J0LXN0YXRlJykudGV4dENvbnRlbnQgPSAod29yZHNbc3RdIHx8IHN0KSArICcgJyArIChkLndhaXRpbmdfZmlsZXMgPyBkLndhaXRpbmdfZmlsZXMgKyAnIGZpbGUocyksICcgKyBkLndhaXRpbmdfZ2IgKyAnIEdCIGluIEZpbGVzIOKAuiBQaG90b3Mg4oC6IEltcG9ydC4nIDogJ05vIFRha2VvdXQgZmlsZXMgaW4gRmlsZXMg4oC6IFBob3RvcyDigLogSW1wb3J0IHlldC4nKTsKICBlbCgnaW1wb3J0LWdvJykuZGlzYWJsZWQgPSBzdCA9PT0gJ3J1bm5pbmcnIHx8ICFkLndhaXRpbmdfZmlsZXMgfHwgIShzdGF0ZSAmJiBzdGF0ZS5hZ2VudF9jb25uZWN0ZWQpOwogIGlmICghKHN0YXRlICYmIHN0YXRlLmFnZW50X2Nvbm5lY3RlZCkgJiYgZC53YWl0aW5nX2ZpbGVzKSBlbCgnaW1wb3J0LXN0YXRlJykudGV4dENvbnRlbnQgKz0gJyBQaG90b3MgaXMgc3RpbGwgc2V0dGluZyB1cDsgSW1wb3J0IG5vdyB3b3JrcyBpbiBhIG1pbnV0ZS4nOwogIGNvbnN0IGxpc3QgPSBlbCgnaW1wb3J0LW9wdGlvbnMnKTsgbGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICBjb25zdCBsYWJlbHMgPSB7IGluY2x1ZGVfYXJjaGl2ZWQ6ICdJbmNsdWRlIGFyY2hpdmVkIHBob3RvcycsIGluY2x1ZGVfcGFydG5lcjogJ0luY2x1ZGUgcGFydG5lci1zaGFyZWQgcGhvdG9zJywgc3luY19hbGJ1bXM6ICdSZWNyZWF0ZSBteSBHb29nbGUgYWxidW1zJywgaW5jbHVkZV90cmFzaGVkOiAnSW5jbHVkZSBwaG90b3MgaW4gR29vZ2xlXHUyMDE5cyB0cmFzaCcgfTsKICBmb3IgKGNvbnN0IFtrLCBsYWJlbF0gb2YgT2JqZWN0LmVudHJpZXMobGFiZWxzKSkgewogICAgY29uc3QgbGkgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdsaScpOwogICAgY29uc3Qgcm93ID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7IHJvdy50eXBlID0gJ2J1dHRvbic7IHJvdy5jbGFzc05hbWUgPSAnd3otcm93JzsKICAgIGNvbnN0IG9uID0gISEoZC5vcHRpb25zIHx8IHt9KVtrXTsKICAgIHJvdy5zZXRBdHRyaWJ1dGUoJ2FyaWEtcHJlc3NlZCcsIFN0cmluZyhvbikpOwogICAgY29uc3QgYm94ID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpOyBib3guY2xhc3NOYW1lID0gJ3d6LWNoZWNrJzsgYm94LnNldEF0dHJpYnV0ZSgnYXJpYS1oaWRkZW4nLCAndHJ1ZScpOwogICAgYm94LmlubmVySFRNTCA9ICc8aT48c3ZnIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTUgMTIuNWw0LjUgNC41TDE5IDcuNSIvPjwvc3ZnPjwvaT4nOwogICAgY29uc3QgbWFpbiA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTsgbWFpbi5jbGFzc05hbWUgPSAnd3otbWFpbic7CiAgICBjb25zdCBiID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYicpOyBiLnRleHRDb250ZW50ID0gbGFiZWw7IG1haW4uYXBwZW5kKGIpOwogICAgcm93LmFwcGVuZChib3gsIG1haW4pOwogICAgcm93Lm9uY2xpY2sgPSBhc3luYyAoKSA9PiB7CiAgICAgIHJvdy5kaXNhYmxlZCA9IHRydWU7CiAgICAgIHRyeSB7IGF3YWl0IHJlcXVlc3QoJ2FwaS9pbXBvcnQvb3B0aW9ucycsIHsgW2tdOiAhb24gfSk7IHJlbmRlckltcG9ydChhd2FpdCByZXF1ZXN0KCdhcGkvaW1wb3J0JykpOyB9CiAgICAgIGNhdGNoIChlKSB7IHNheShlLm1lc3NhZ2UsICdlcnJvcicpOyByb3cuZGlzYWJsZWQgPSBmYWxzZTsgfQogICAgfTsKICAgIGxpLmFwcGVuZChyb3cpOyBsaXN0LmFwcGVuZChsaSk7CiAgfQogIGNvbnN0IGxvZyA9IGVsKCdpbXBvcnQtbG9nJyk7CiAgbG9nLmhpZGRlbiA9ICEoZC5sb2dfdGFpbCAmJiBkLmxvZ190YWlsLmxlbmd0aCk7CiAgbG9nLnRleHRDb250ZW50ID0gKGQubG9nX3RhaWwgfHwgW10pLmpvaW4oJ1xuJyk7Cn0KCmFzeW5jIGZ1bmN0aW9uIHJlZnJlc2goKSB7CiAgdHJ5IHsgcmVuZGVyKGF3YWl0IHJlcXVlc3QoJ2FwaS9zdGF0dXMnKSk7IH0KICBjYXRjaCAoZSkgeyBzYXkoZS5tZXNzYWdlLCAnZXJyb3InKTsgfQp9CgplbCgnb3Blbi1pbXBvcnQnKS5vbmNsaWNrID0gYXN5bmMgKCkgPT4gewogIFd6U2hlZXQub3BlbihlbCgnaW1wb3J0LXNoZWV0JykpOwogIHRyeSB7IHJlbmRlckltcG9ydChhd2FpdCByZXF1ZXN0KCdhcGkvaW1wb3J0JykpOyB9IGNhdGNoIChlKSB7IHNheShlLm1lc3NhZ2UsICdlcnJvcicpOyB9Cn07CmVsKCdvcGVuLWFnZW50Jykub25jbGljayA9ICgpID0+IFd6U2hlZXQub3BlbihlbCgnYWdlbnQtc2hlZXQnKSk7CgovLyBUaGUg4ouvIG1lbnUgKG5hcnJvdyB3aWR0aHMsIHdoZXJlIHRoZSBoZWFkZXIgaXMgaGlkZGVuKTogdGhlIHNhbWUgYWN0aW9ucyBhcyB0aGUgaGVhZGVyIGNoaXBzLCBpbiBvbmUgc2hlZXQuCmZ1bmN0aW9uIHN5bmNNZW51KCkgewogIGZvciAoY29uc3Qgcm93IG9mIGVsKCdtZW51LXNoZWV0JykucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtbWVudV0nKSkgewogICAgY29uc3QgY2hpcCA9IGVsKHJvdy5kYXRhc2V0Lm1lbnUpOwogICAgcm93LnBhcmVudEVsZW1lbnQuaGlkZGVuID0gY2hpcC5oaWRkZW47CiAgICByb3cucXVlcnlTZWxlY3RvcignYicpLnRleHRDb250ZW50ID0gY2hpcC50ZXh0Q29udGVudDsKICAgIGlmIChjaGlwLmhyZWYpIHJvdy5ocmVmID0gY2hpcC5ocmVmOwogIH0KfQplbCgnb3Blbi1tZW51Jykub25jbGljayA9ICgpID0+IHsgc3luY01lbnUoKTsgV3pTaGVldC5vcGVuKGVsKCdtZW51LXNoZWV0JykpOyB9OwplbCgnbWVudS1zaGVldCcpLmFkZEV2ZW50TGlzdGVuZXIoJ2NsaWNrJywgZSA9PiB7CiAgY29uc3Qgcm93ID0gZS50YXJnZXQuY2xvc2VzdCgnW2RhdGEtbWVudV0nKTsgaWYgKCFyb3cpIHJldHVybjsKICBXelNoZWV0LmNsb3NlKGVsKCdtZW51LXNoZWV0JykpOwogIGlmIChyb3cuZGF0YXNldC5tZW51ICE9PSAnb3Blbi1mdWxsJykgZWwocm93LmRhdGFzZXQubWVudSkuY2xpY2soKTsgLy8gdGhlIGxpbmsgb3BlbnMgYnkgaXRzZWxmCn0pOwplbCgnaW1wb3J0LWdvJykub25jbGljayA9IGFzeW5jICgpID0+IHsKICBlbCgnaW1wb3J0LWdvJykuZGlzYWJsZWQgPSB0cnVlOwogIHRyeSB7IGNvbnN0IHIgPSBhd2FpdCByZXF1ZXN0KCdhcGkvaW1wb3J0L3N0YXJ0Jywge30pOyBzYXkoci5zdGFydGVkID8gJ0ltcG9ydCBzdGFydGVkLicgOiAoci5tZXNzYWdlIHx8ICdOb3Qgc3RhcnRlZC4nKSwgJ25vdGUnKTsgfQogIGNhdGNoIChlKSB7IHNheShlLm1lc3NhZ2UsICdlcnJvcicpOyB9CiAgdHJ5IHsgcmVuZGVySW1wb3J0KGF3YWl0IHJlcXVlc3QoJ2FwaS9pbXBvcnQnKSk7IH0gY2F0Y2ggKGUpIHt9Cn07Ci8vIC0tLS0gTW9udGhseSBwcmludCByZXZpZXcgKGFwcCBzd2l0Y2ggcHJpbnQtcmV2aWV3LCBodW1hbml0eWxhYnMtb3JnL3Bob3RvcyMyKSAtLS0tCi8vIFBob3RvcyBzdWdnZXN0cyBhIGZldyBwaG90b3Mgb2YgbGFzdCBtb250aDsgdGhlIG93bmVyIHRha2VzIHBob3RvcyBvdXQgb3IgcHV0cyBvdGhlcnMgaW4sIHdyaXRlcyBhIG5vdGUgYW5kIHNhdmVzCi8vIHRoZSBhcHByb3ZlZCBzZXQgb24gdGhlIGJveC4gTm90aGluZyBoZXJlIG9yZGVycywgbWFpbHMgb3IgcGF5cyBmb3IgcHJpbnRzLgpjb25zdCBwcmludHMgPSB7IG1vbnRoOiAnJywgZG9jOiBudWxsLCBwaWNrZWQ6IG5ldyBTZXQoKSwgYnVzeTogZmFsc2UgfTsKZnVuY3Rpb24gbW9udGhTaGlmdChtb250aCwgYnkpIHsKICBjb25zdCBbeSwgbV0gPSBtb250aC5zcGxpdCgnLScpLm1hcChOdW1iZXIpOwogIGNvbnN0IGQgPSBuZXcgRGF0ZShEYXRlLlVUQyh5LCBtIC0gMSArIGJ5LCAxKSk7CiAgcmV0dXJuIGQuZ2V0VVRDRnVsbFllYXIoKSArICctJyArIFN0cmluZyhkLmdldFVUQ01vbnRoKCkgKyAxKS5wYWRTdGFydCgyLCAnMCcpOwp9CmZ1bmN0aW9uIHJlbmRlclByaW50cygpIHsKICBjb25zdCBkID0gcHJpbnRzLmRvYzsgaWYgKCFkKSByZXR1cm47CiAgZWwoJ3ByaW50LW1vbnRoJykudGV4dENvbnRlbnQgPSBkLmxhYmVsOwogIGVsKCdwcmludC1uZXh0JykuZGlzYWJsZWQgPSBwcmludHMuYnVzeSB8fCBkLm1vbnRoID49IG1vbnRoU2hpZnQoZC5wcmV2aW91c19tb250aCwgMSk7CiAgZWwoJ3ByaW50LXByZXYnKS5kaXNhYmxlZCA9IHByaW50cy5idXN5OwogIGVsKCdwcmludC1saW5lJykudGV4dENvbnRlbnQgPSBkLm5leHQgfHwgJyc7CiAgZWwoJ3ByaW50LW5ldmVyJykudGV4dENvbnRlbnQgPSBkLm5ldmVyX29yZGVycyB8fCAnJzsKICBjb25zdCBncmlkID0gZWwoJ3ByaW50LWdyaWQnKTsgZ3JpZC5yZXBsYWNlQ2hpbGRyZW4oKTsKICBmb3IgKGNvbnN0IGMgb2YgZC5jYW5kaWRhdGVzIHx8IFtdKSB7CiAgICBjb25zdCBsaSA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2xpJyk7CiAgICBjb25zdCBiID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7IGIudHlwZSA9ICdidXR0b24nOyBiLmNsYXNzTmFtZSA9ICdwaC1wcmludC1waWNrJzsKICAgIGNvbnN0IG9uID0gcHJpbnRzLnBpY2tlZC5oYXMoYy5pZCk7CiAgICBiLnNldEF0dHJpYnV0ZSgnYXJpYS1wcmVzc2VkJywgU3RyaW5nKG9uKSk7CiAgICBjb25zdCB3aGVuID0gYy50YWtlbiA/IG5ldyBEYXRlKGMudGFrZW4uc2xpY2UoMCwgMTApICsgJ1QxMjowMDowMFonKS50b0xvY2FsZURhdGVTdHJpbmcodW5kZWZpbmVkLCB7IGRheTogJ251bWVyaWMnLCBtb250aDogJ3Nob3J0JywgdGltZVpvbmU6ICdVVEMnIH0pIDogJyc7CiAgICBiLnNldEF0dHJpYnV0ZSgnYXJpYS1sYWJlbCcsIChvbiA/ICdQaWNrZWQ6ICcgOiAnTm90IHBpY2tlZDogJykgKyBbd2hlbiwgKGMucGVvcGxlIHx8IFtdKS5qb2luKCcsICcpXS5maWx0ZXIoQm9vbGVhbikuam9pbignLCAnKSk7CiAgICBjb25zdCBpbWcgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdpbWcnKTsgaW1nLmFsdCA9ICcnOyBpbWcubG9hZGluZyA9ICdsYXp5JzsgaW1nLmRlY29kaW5nID0gJ2FzeW5jJzsKICAgIGltZy5zcmMgPSAnYXBpL3ByaW50LXJldmlldy90aHVtYj9pZD0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGMuaWQpOwogICAgY29uc3QgbWFyayA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTsgbWFyay5jbGFzc05hbWUgPSAncGgtcHJpbnQtbWFyayc7IG1hcmsuc2V0QXR0cmlidXRlKCdhcmlhLWhpZGRlbicsICd0cnVlJyk7CiAgICBtYXJrLmlubmVySFRNTCA9ICc8c3ZnIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTUgMTIuNWw0LjUgNC41TDE5IDcuNSIvPjwvc3ZnPic7CiAgICBjb25zdCBjYXAgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7IGNhcC5jbGFzc05hbWUgPSAncGgtcHJpbnQtY2FwJzsgY2FwLnRleHRDb250ZW50ID0gd2hlbjsKICAgIGIuYXBwZW5kKGltZywgbWFyaywgY2FwKTsKICAgIGIub25jbGljayA9ICgpID0+IHsKICAgICAgaWYgKHByaW50cy5waWNrZWQuaGFzKGMuaWQpKSBwcmludHMucGlja2VkLmRlbGV0ZShjLmlkKTsKICAgICAgZWxzZSBpZiAocHJpbnRzLnBpY2tlZC5zaXplIDwgKGQubWF4IHx8IDMwKSkgcHJpbnRzLnBpY2tlZC5hZGQoYy5pZCk7CiAgICAgIHJlbmRlclByaW50cygpOwogICAgfTsKICAgIGxpLmFwcGVuZChiKTsgZ3JpZC5hcHBlbmQobGkpOwogIH0KICBjb25zdCBhbnkgPSAoZC5jYW5kaWRhdGVzIHx8IFtdKS5sZW5ndGggPiAwOwogIGVsKCdwcmludC1mb3JtJykuaGlkZGVuID0gIWFueTsKICBlbCgncHJpbnQtY291bnQnKS50ZXh0Q29udGVudCA9IHByaW50cy5waWNrZWQuc2l6ZSArICcgb2YgJyArIGQuY2FuZGlkYXRlcy5sZW5ndGggKyAnIHBpY2tlZCcgKyAoZC5zYXZlZCA/ICcgwrcgc2F2ZWQgc2V0JyA6ICcgwrcgbm90IHNhdmVkIHlldCcpOwogIGVsKCdwcmludC1zYXZlJykuZGlzYWJsZWQgPSBwcmludHMuYnVzeSB8fCAhcHJpbnRzLnBpY2tlZC5zaXplOwogIGVsKCdwcmludC1zYXZlJykudGV4dENvbnRlbnQgPSBkLnNhdmVkID8gJ1NhdmUgY2hhbmdlcycgOiAnU2F2ZSBhcHByb3ZlZCBzZXQnOwogIGNvbnN0IHNhdmVkID0gZWwoJ3ByaW50LXNhdmVkJyk7IHNhdmVkLnJlcGxhY2VDaGlsZHJlbigpOwogIGZvciAoY29uc3QgcyBvZiBkLnNhdmVkX21vbnRocyB8fCBbXSkgewogICAgY29uc3QgbGkgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdsaScpOwogICAgY29uc3Qgcm93ID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7IHJvdy50eXBlID0gJ2J1dHRvbic7IHJvdy5jbGFzc05hbWUgPSAnd3otcm93JzsKICAgIGNvbnN0IG1haW4gPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7IG1haW4uY2xhc3NOYW1lID0gJ3d6LW1haW4nOwogICAgY29uc3QgYiA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2InKTsgYi50ZXh0Q29udGVudCA9IHMubGFiZWwgKyAnIMK3ICcgKyBzLmNvdW50ICsgJyBwaG90bycgKyAocy5jb3VudCA9PT0gMSA/ICcnIDogJ3MnKTsKICAgIG1haW4uYXBwZW5kKGIpOwogICAgaWYgKHMubm90ZSkgeyBjb25zdCBzbSA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NtYWxsJyk7IHNtLnRleHRDb250ZW50ID0gcy5ub3RlOyBtYWluLmFwcGVuZChzbSk7IH0KICAgIHJvdy5hcHBlbmQobWFpbik7CiAgICByb3cub25jbGljayA9ICgpID0+IGxvYWRQcmludHMocy5tb250aCk7CiAgICBsaS5hcHBlbmQocm93KTsgc2F2ZWQuYXBwZW5kKGxpKTsKICB9Cn0KYXN5bmMgZnVuY3Rpb24gbG9hZFByaW50cyhtb250aCkgewogIHByaW50cy5idXN5ID0gdHJ1ZTsgaWYgKHByaW50cy5kb2MpIHJlbmRlclByaW50cygpOwogIHRyeSB7CiAgICBjb25zdCBkID0gYXdhaXQgcmVxdWVzdCgnYXBpL3ByaW50LXJldmlldycgKyAobW9udGggPyAnP21vbnRoPScgKyBlbmNvZGVVUklDb21wb25lbnQobW9udGgpIDogJycpKTsKICAgIHByaW50cy5kb2MgPSBkOyBwcmludHMubW9udGggPSBkLm1vbnRoOwogICAgcHJpbnRzLnBpY2tlZCA9IG5ldyBTZXQoKGQuY2FuZGlkYXRlcyB8fCBbXSkuZmlsdGVyKGMgPT4gYy5zZWxlY3RlZCkubWFwKGMgPT4gYy5pZCkpOwogICAgZWwoJ3ByaW50LW5vdGUnKS52YWx1ZSA9IChkLnNhdmVkICYmIGQuc2F2ZWQubm90ZSkgfHwgJyc7CiAgfSBjYXRjaCAoZSkgeyBzYXkoZS5tZXNzYWdlLCAnZXJyb3InKTsgZWwoJ3ByaW50LWxpbmUnKS50ZXh0Q29udGVudCA9IGUubWVzc2FnZTsgfQogIHByaW50cy5idXN5ID0gZmFsc2U7IHJlbmRlclByaW50cygpOwp9CmVsKCdvcGVuLXByaW50cycpLm9uY2xpY2sgPSAoKSA9PiB7IFd6U2hlZXQub3BlbihlbCgncHJpbnQtc2hlZXQnKSk7IGxvYWRQcmludHMocHJpbnRzLm1vbnRoKTsgfTsKZWwoJ3ByaW50LXByZXYnKS5vbmNsaWNrID0gKCkgPT4gcHJpbnRzLmRvYyAmJiBsb2FkUHJpbnRzKG1vbnRoU2hpZnQocHJpbnRzLmRvYy5tb250aCwgLTEpKTsKZWwoJ3ByaW50LW5leHQnKS5vbmNsaWNrID0gKCkgPT4gcHJpbnRzLmRvYyAmJiBsb2FkUHJpbnRzKG1vbnRoU2hpZnQocHJpbnRzLmRvYy5tb250aCwgMSkpOwplbCgncHJpbnQtc2F2ZScpLm9uY2xpY2sgPSBhc3luYyAoKSA9PiB7CiAgY29uc3QgZCA9IHByaW50cy5kb2M7IGlmICghZCB8fCAhcHJpbnRzLnBpY2tlZC5zaXplKSByZXR1cm47CiAgcHJpbnRzLmJ1c3kgPSB0cnVlOyByZW5kZXJQcmludHMoKTsKICBjb25zdCBvcmRlciA9IChkLmNhbmRpZGF0ZXMgfHwgW10pLm1hcChjID0+IGMuaWQpLmZpbHRlcihpZCA9PiBwcmludHMucGlja2VkLmhhcyhpZCkpOwogIHRyeSB7CiAgICBjb25zdCByID0gYXdhaXQgcmVxdWVzdCgnYXBpL3ByaW50LXJldmlldycsIHsgbW9udGg6IGQubW9udGgsIGFzc2V0czogb3JkZXIsIG5vdGU6IGVsKCdwcmludC1ub3RlJykudmFsdWUgfSk7CiAgICBwcmludHMuZG9jID0gcjsgcHJpbnRzLnBpY2tlZCA9IG5ldyBTZXQoKHIuY2FuZGlkYXRlcyB8fCBbXSkuZmlsdGVyKGMgPT4gYy5zZWxlY3RlZCkubWFwKGMgPT4gYy5pZCkpOwogICAgc2F5KCdTYXZlZCAnICsgb3JkZXIubGVuZ3RoICsgJyBwaG90bycgKyAob3JkZXIubGVuZ3RoID09PSAxID8gJycgOiAncycpICsgJyBmb3IgJyArIHIubGFiZWwgKyAnLicsICdub3RlJyk7CiAgfSBjYXRjaCAoZSkgeyBzYXkoZS5tZXNzYWdlLCAnZXJyb3InKTsgZWwoJ3ByaW50LWxpbmUnKS50ZXh0Q29udGVudCA9IGUubWVzc2FnZTsgfQogIHByaW50cy5idXN5ID0gZmFsc2U7IHJlbmRlclByaW50cygpOwp9OwoKaWYgKCF3aW5kb3cuV2l6YXJkQXBwIHx8IFdpemFyZEFwcC52ZXJzaW9uICE9PSAxKSB7CiAgc2F5KCdXaXphcmQgc3lzdGVtIGZ1bmN0aW9ucyBhcmUgdW5hdmFpbGFibGUuIE9wZW4gUGhvdG9zIHRocm91Z2ggV2l6YXJkLicsICdlcnJvcicpOwp9IGVsc2UgewogIFdpemFyZEFwcC5yZWFkeSh7IG5hbWU6ICdQaG90b3MnLCB2ZXJzaW9uOiAnMC41LjAnIH0pOwogIFdpemFyZEFwcC5iYWNrLnJlZ2lzdGVyKHsgY2FuR29CYWNrOiAoKSA9PiBmYWxzZSwgYmFjazogKCkgPT4gZmFsc2UgfSk7Cn0KCnJlZnJlc2goKTsKc2V0SW50ZXJ2YWwoKCkgPT4geyBpZiAoIWRvY3VtZW50LmhpZGRlbikgcmVmcmVzaCgpOyB9LCAxNTAwMCk7CgovLyAtLS0tIFBob25lIGJhY2t1cCAoZnJvbSB0aGUgV2l6YXJkIE9TIEFuZHJvaWQgYXBwKSAtLS0tCi8vIFRoZSBiYWNrdXAgd29ya2VyIGxpdmVzIGluIFdpemFyZCBPUyBmb3IgQW5kcm9pZDsgdGhpcyBwYWdlIGRyaXZlcyBpdCB0aHJvdWdoIHRoZSBzaGVsbCdzIFBob3RvcyByZWxheQovLyAocGhvdG9zLmpzLCB1bmNoYW5nZWQgZnJvbSB0aGUgZm9ybWVyIHZpZXctb25seSBQaG90b3MgYXBwKS4gVGhlIHBob3RvIHNlcnZlciBpcyB0aGlzIGFwcCdzIG93biBJbW1pY2gsIHNvIHRoZQovLyBvd25lciBuZXZlciB0eXBlcyBhbiBhZGRyZXNzLiBJbiBhIGJyb3dzZXIgb3IgdGhlIGRlc2t0b3Agc2hlbGwgdGhlcmUgaXMgbm8gcmVsYXkgYW5kIHRoZSBidXR0b24gc3RheXMgaGlkZGVuLgpjb25zdCBiYWNrdXAgPSB7IG1vZGU6ICcnLCBzdGF0dXM6IG51bGwsIGVycm9yOiAnJywgYnVzeTogZmFsc2UsIHNlcnZlcjogJycsIGFsYnVtc09wZW46IGZhbHNlLCBhbGJ1bXM6IG51bGwsIGFsYnVtc0Vycm9yOiAnJywgY29uZmlybURpc2Nvbm5lY3Q6IGZhbHNlIH07CmNvbnN0IFAgPSB3aW5kb3cuUGhvdG9zUGFnZTsKY29uc3QgZnJhbWVkID0gd2luZG93LnBhcmVudCAmJiB3aW5kb3cucGFyZW50ICE9PSB3aW5kb3c7CmNvbnN0IGJhcGkgPSBQLmNsaWVudChtc2cgPT4gewogIGlmICghZnJhbWVkKSB0aHJvdyBPYmplY3QuYXNzaWduKG5ldyBFcnJvcignbm8tYnJpZGdlJyksIHsgY29kZTogJ25vLWJyaWRnZScgfSk7CiAgd2luZG93LnBhcmVudC5wb3N0TWVzc2FnZShtc2csIGxvY2F0aW9uLm9yaWdpbik7Cn0sIHsKICBvblN0YXR1czogcyA9PiB7IGJhY2t1cC5zdGF0dXMgPSBzOyBpZiAoYmFja3VwLm1vZGUgIT09ICd2MScpIHsgYmFja3VwLm1vZGUgPSAndjEnOyBlbCgnb3Blbi1iYWNrdXAnKS5oaWRkZW4gPSBmYWxzZTsgfSByZW5kZXJCYWNrdXAoKTsgfSwKICBvblJlc3VtZTogKCkgPT4geyBpZiAoYmFja3VwLm1vZGUgPT09ICd2MScpIGJhcGkuc3RhdHVzKCkuY2F0Y2goKCkgPT4ge30pOyB9LAp9KTsKd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoJ21lc3NhZ2UnLCBldiA9PiB7IGlmIChldi5vcmlnaW4gPT09IGxvY2F0aW9uLm9yaWdpbiAmJiBldi5zb3VyY2UgPT09IHdpbmRvdy5wYXJlbnQpIGJhcGkub25NZXNzYWdlKGV2LmRhdGEpOyB9KTsKCmZ1bmN0aW9uIHJlbmRlckJhY2t1cCgpIHsKICBjb25zdCByb290ID0gZWwoJ2JhY2t1cC1yb290Jyk7CiAgaWYgKGRvY3VtZW50LmFjdGl2ZUVsZW1lbnQgJiYgZG9jdW1lbnQuYWN0aXZlRWxlbWVudC5tYXRjaGVzICYmIGRvY3VtZW50LmFjdGl2ZUVsZW1lbnQubWF0Y2hlcygnW2RhdGEtcGgtc2VydmVyXScpKSB7CiAgICByb290LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLXBoLXN0YXR1c10nKS5mb3JFYWNoKHggPT4geyB4LnRleHRDb250ZW50ID0gYmFja3VwLmVycm9yIHx8IFAuc3VtbWFyeShiYWNrdXAuc3RhdHVzKTsgfSk7IHJldHVybjsKICB9CiAgcm9vdC5pbm5lckhUTUwgPSBiYWNrdXAubW9kZSA9PT0gJ3YxJyA/IFAuaHRtbChiYWNrdXApIDogJzxwIGNsYXNzPSJwaC1zdGF0dXMiPkNoZWNraW5n4oCmPC9wPic7CiAgaWYgKGJhY2t1cC5zdGF0dXMpIGVsKCdvcGVuLWJhY2t1cCcpLnRleHRDb250ZW50ID0gYmFja3VwLnN0YXR1cy5lbmFibGVkID8gJ1Bob25lIGJhY2t1cCDCtyBvbicgOiAnUGhvbmUgYmFja3VwJzsKfQpmdW5jdGlvbiBiYWNrdXBUYXAod2hhdCwgZXh0cmEgPSB7fSkgewogIGJhY2t1cC5lcnJvciA9ICcnOwogIGNvbnN0IHBlbmRpbmcgPSBQLmFjdChiYWNrdXAsIGJhcGksIHdoYXQsIGV4dHJhKTsKICBpZiAoIXBlbmRpbmcpIHsgcmVuZGVyQmFja3VwKCk7IHJldHVybjsgfQogIGlmICh3aGF0ICE9PSAnYWxidW1zJyAmJiB3aGF0ICE9PSAnYWxidW0nKSBiYWNrdXAuYnVzeSA9IHdoYXQgPT09ICdydW4nID8gJ3J1bicgOiB0cnVlOwogIHJlbmRlckJhY2t1cCgpOwogIHBlbmRpbmcuY2F0Y2goZSA9PiB7IGJhY2t1cC5lcnJvciA9IGUudGltZW91dCA/ICdUaGUgV2l6YXJkIE9TIGFwcCBkaWQgbm90IGFuc3dlciDCtyB0cnkgYWdhaW4nIDogZS5tZXNzYWdlOyB9KQogICAgLmZpbmFsbHkoKCkgPT4geyBiYWNrdXAuYnVzeSA9IGZhbHNlOyByZW5kZXJCYWNrdXAoKTsgfSk7Cn0KZWwoJ2JhY2t1cC1yb290JykuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLCBlID0+IHsKICBjb25zdCBiID0gZS50YXJnZXQuY2xvc2VzdCgnW2RhdGEtcGhdLCBbZGF0YS1waC1hbGJ1bV0nKTsgaWYgKCFiIHx8IGIuZGlzYWJsZWQpIHJldHVybjsKICBpZiAoYi5kYXRhc2V0LnBoQWxidW0gIT0gbnVsbCkgeyBiYWNrdXBUYXAoJ2FsYnVtJywgeyBpZDogYi5kYXRhc2V0LnBoQWxidW0gfSk7IHJldHVybjsgfQogIGNvbnN0IGZpZWxkID0gZWwoJ2JhY2t1cC1yb290JykucXVlcnlTZWxlY3RvcignW2RhdGEtcGgtc2VydmVyXScpOwogIGJhY2t1cFRhcChiLmRhdGFzZXQucGgsIGZpZWxkICYmIGZpZWxkLnZhbHVlID8geyBzZXJ2ZXI6IGZpZWxkLnZhbHVlIH0gOiB7fSk7Cn0pOwplbCgnYmFja3VwLXJvb3QnKS5hZGRFdmVudExpc3RlbmVyKCdpbnB1dCcsIGUgPT4geyBpZiAoZS50YXJnZXQubWF0Y2hlcygnW2RhdGEtcGgtc2VydmVyXScpKSBiYWNrdXAuc2VydmVyID0gZS50YXJnZXQudmFsdWU7IH0pOwplbCgnb3Blbi1iYWNrdXAnKS5vbmNsaWNrID0gKCkgPT4geyBXelNoZWV0Lm9wZW4oZWwoJ2JhY2t1cC1zaGVldCcpKTsgcmVuZGVyQmFja3VwKCk7IGJhcGkuc3RhdHVzKCkuY2F0Y2goKCkgPT4ge30pOyB9OwplbCgnYmFja3VwLXNoZWV0JykuYWRkRXZlbnRMaXN0ZW5lcignY2xvc2UnLCAoKSA9PiB7IGJhY2t1cC5hbGJ1bXNPcGVuID0gZmFsc2U7IGJhY2t1cC5jb25maXJtRGlzY29ubmVjdCA9IGZhbHNlOyB9KTsKUC5kZXRlY3QoYmFwaSkudGhlbihtb2RlID0+IHsKICBpZiAobW9kZSAhPT0gJ3YxJyAmJiBtb2RlICE9PSAndjEtZXJyb3InKSByZXR1cm47IC8vIGJyb3dzZXIsIGRlc2t0b3Agb3IgYW4gb2xkIHBob25lIGFwcDogSW1taWNoIHBob25lIGFwcCBpbnN0cnVjdGlvbnMgc3RheSBpbiBBZ2VudAogIGJhY2t1cC5tb2RlID0gJ3YxJzsKICBpZiAobW9kZSA9PT0gJ3YxLWVycm9yJykgYmFja3VwLmVycm9yID0gJ0NvdWxkIG5vdCByZWFkIGJhY2t1cCBzdGF0dXMgwrcgdHJ5IGFnYWluJzsKICBlbCgnb3Blbi1iYWNrdXAnKS5oaWRkZW4gPSBmYWxzZTsKICByZW5kZXJCYWNrdXAoKTsKfSk7Cg==', 'styles.css': 'LyogUGhvdG9zLW9ubHkgbGF5b3V0LiBFdmVyeXRoaW5nIHNoYXJlZCAodHlwZSwgcm93cywgc2hlZXRzLCBjb2xvdXJzKSBjb21lcyBmcm9tIHdpemFyZC11aS5jc3MuICovCi5waC1hcHAgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBoZWlnaHQ6IDEwMGR2aDsgbWluLWhlaWdodDogMDsgfQoucGgtaGVhZCB7IGZsZXg6IG5vbmU7IH0KLnBoLWhlYWQgcCB7IG1hcmdpbjogMnB4IDAgOHB4OyB9Ci5waC1hY3Rpb25zIHsgZGlzcGxheTogZmxleDsgZmxleC13cmFwOiB3cmFwOyBnYXA6IDhweDsgfQoucGgtYWN0aW9ucyBhLnd6LWNoaXAgeyB0ZXh0LWRlY29yYXRpb246IG5vbmU7IH0KLnBoLWFjdGlvbnMgW2hpZGRlbl0sIC5waC1tZW51IFtoaWRkZW5dIHsgZGlzcGxheTogbm9uZTsgfSAvKiAud3otY2hpcCdzIGRpc3BsYXkgd291bGQgb3RoZXJ3aXNlIGJlYXQgW2hpZGRlbl0gKi8KLnBoLXNldHVwIHsgbWFyZ2luOiB2YXIoLS13ei1zcGFjZS0yKSB2YXIoLS13ei1zcGFjZS0zKTsgfQoucGgtc2V0dXAgaDIgeyBtYXJnaW46IDAgMCA2cHg7IH0KLnBoLWZyYW1lLXdyYXAgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBtYXJnaW46IDhweCAwIDA7IGJvcmRlci10b3A6IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyB9Ci8qIFBob25lIGFuZCBvdGhlciBuYXJyb3cgd2lkdGhzOiBubyBQaG90b3MgaGVhZGVyIG92ZXIgSW1taWNoJ3Mgb3duIHRvcCBiYXIuIEltbWljaCBmaWxscyB0aGUgYXBwIGFyZWEgdW5kZXIgdGhlCiAqIHNoZWxsJ3MgY3V0b3V0OyB0aGUgYWN0aW9ucyBsaXZlIGJlaGluZCBvbmUgcXVpZXQg4ouvIGJ1dHRvbiAoaW4gdGhlIGdhcCBiZXR3ZWVuIEltbWljaCdzIGxvZ28gYW5kIGl0cyBzZWFyY2gKICogYnV0dG9uKSB0aGF0IG9wZW5zIHRoZSBtZW51IHNoZWV0LiBXaWRlIGxheW91dHMga2VlcCB0aGUgaGVhZGVyLiAqLwoucGgtYXBwIHsgcG9zaXRpb246IHJlbGF0aXZlOyB9Ci5waC1tb3JlIHsgZGlzcGxheTogbm9uZTsgfQoucGgtbWVudSBhLnd6LXJvdyB7IHRleHQtZGVjb3JhdGlvbjogbm9uZTsgY29sb3I6IGluaGVyaXQ7IH0KQGNvbnRhaW5lciB3ei1hcHAgKG1heC13aWR0aDogNTk5cHgpIHsKICAucGgtaGVhZCB7IGRpc3BsYXk6IG5vbmU7IH0KICAucGgtZnJhbWUtd3JhcCB7IG1hcmdpbjogMDsgYm9yZGVyLXRvcDogMDsgcGFkZGluZy10b3A6IHZhcigtLXdvcy1jdXRvdXQsIDBweCk7IH0KICAucGgtc2V0dXAgeyBtYXJnaW4tdG9wOiBjYWxjKHZhcigtLXdvcy1jdXRvdXQsIDBweCkgKyA2MHB4KTsgfQogIC5waC1tb3JlIHsgZGlzcGxheTogZ3JpZDsgcGxhY2UtaXRlbXM6IGNlbnRlcjsgcG9zaXRpb246IGFic29sdXRlOyB6LWluZGV4OiAyOyB0b3A6IGNhbGModmFyKC0td29zLWN1dG91dCwgMHB4KSArIDEwcHgpOyBsZWZ0OiAxMTBweDsKICAgIHdpZHRoOiB2YXIoLS13ei10YXJnZXQsIDQ0cHgpOyBoZWlnaHQ6IHZhcigtLXd6LXRhcmdldCwgNDRweCk7IHBhZGRpbmc6IDA7IGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogNTAlOyBiYWNrZ3JvdW5kOiBub25lOwogICAgY29sb3I6IHZhcigtLWluay0zLCBjdXJyZW50Q29sb3IpOyBvcGFjaXR5OiAuNzU7IC13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjogdHJhbnNwYXJlbnQ7IH0KICAucGgtbW9yZTo6YmVmb3JlIHsgY29udGVudDogIiI7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgaW5zZXQ6IDdweDsgYm9yZGVyLXJhZGl1czogNTAlOyBiYWNrZ3JvdW5kOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otYmFzZSkgMzUlLCB0cmFuc3BhcmVudCk7IH0KICAucGgtbW9yZSBzdmcgeyBwb3NpdGlvbjogcmVsYXRpdmU7IHdpZHRoOiAyMHB4OyBoZWlnaHQ6IDIwcHg7IGZpbGw6IGN1cnJlbnRDb2xvcjsgfQogIC5waC1tb3JlOmFjdGl2ZSwgLnBoLW1vcmU6Zm9jdXMtdmlzaWJsZSB7IG9wYWNpdHk6IDE7IH0KICAvKiBObyBsaWJyYXJ5IHlldCAoc2V0dXAgY2FyZHMpOiB0aGUgYnV0dG9uIHNpdHMgdG9wIHJpZ2h0LCBhYm92ZSB0aGUgY2FyZC4gKi8KICAucGgtYXBwOmhhcygjZnJhbWUtd3JhcFtoaWRkZW5dKSAucGgtbW9yZSB7IGxlZnQ6IGF1dG87IHJpZ2h0OiA4cHg7IH0KfQoucGgtZnJhbWUtd3JhcCBpZnJhbWUgeyBkaXNwbGF5OiBibG9jazsgd2lkdGg6IDEwMCU7IGhlaWdodDogMTAwJTsgYm9yZGVyOiAwOyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1jYW52YXMpOyB9Ci5waC1zdGVwcyB7IG1hcmdpbjogMCAwIDEycHg7IHBhZGRpbmctbGVmdDogMS4yZW07IGxpbmUtaGVpZ2h0OiAxLjQ1OyB9Ci5waC1zdGVwcyBsaSB7IG1hcmdpbjogMCAwIDZweDsgfQoucGgtaGVscCB7IGxpbmUtaGVpZ2h0OiAxLjQ1OyBtYXJnaW46IDAgMCAxMHB4OyB9Ci5waC1pbXBvcnQtc3RhdGUgeyBtYXJnaW46IDAgMCAxMHB4OyBmb250LXdlaWdodDogNjAwOyB9Ci5waC1sb2cgeyBtYXgtaGVpZ2h0OiAzMHZoOyBvdmVyZmxvdzogYXV0bzsgZm9udC1mYW1pbHk6IHZhcigtLXd6LWZvbnQtbW9ubyk7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IHdoaXRlLXNwYWNlOiBwcmUtd3JhcDsgbWFyZ2luOiAxMHB4IDAgMDsgfQoKLyogUGhvbmUgYmFja3VwIHNoZWV0IChyb3dzIGFuZCBzd2l0Y2hlcyBmcm9tIHRoZSBmb3JtZXIgdmlldy1vbmx5IFBob3RvcyBhcHA7IGtpdCB2YXJpYWJsZXMgb25seSkuICovCiNiYWNrdXAtcm9vdCAucGggeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9Ci5waC1pbnRybywgLnBoLW5vdGUsIC5waC1zZXJ2ZXIgeyBtYXJnaW46IDAgNHB4IDEwcHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGxpbmUtaGVpZ2h0OiAxLjQ7IGNvbG9yOiB2YXIoLS13ei10ZXh0LXNlY29uZGFyeSk7IH0KLnBoLXNlcnZlciB7IG1hcmdpbi10b3A6IDEycHg7IGNvbG9yOiB2YXIoLS13ei10ZXh0LW11dGVkKTsgb3ZlcmZsb3ctd3JhcDogYW55d2hlcmU7IH0KLnBoLXN0YXR1cyB7IG1hcmdpbjogMTBweCA0cHggOHB4OyBjb2xvcjogdmFyKC0td3otdGV4dCk7IH0KLnBoLXJvdyB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTJweDsgd2lkdGg6IDEwMCU7IG1pbi1oZWlnaHQ6IDU2cHg7IHBhZGRpbmc6IDEwcHggNHB4OyBib3JkZXI6IDA7IGJvcmRlci1ib3R0b206IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyBiYWNrZ3JvdW5kOiB0cmFuc3BhcmVudDsgY29sb3I6IHZhcigtLXd6LXRleHQpOyB0ZXh0LWFsaWduOiBsZWZ0OyBmb250OiBpbmhlcml0OyB9Ci5waC1yb3c6ZGlzYWJsZWQsIC5waC1idG46ZGlzYWJsZWQgeyBvcGFjaXR5OiAuNTU7IH0KLnBoLXR4IHsgZmxleDogMTsgbWluLXdpZHRoOiAwOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9Ci5waC10eCBiIHsgZm9udC13ZWlnaHQ6IDYwMDsgfQoucGgtdHggc21hbGwgeyBtYXJnaW4tdG9wOiAycHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGNvbG9yOiB2YXIoLS13ei10ZXh0LW11dGVkKTsgfQoucGgtZ28geyBjb2xvcjogdmFyKC0td3otdGV4dC1tdXRlZCk7IH0KLnN3IHsgZmxleDogMCAwIDQ0cHg7IGhlaWdodDogMjZweDsgYm9yZGVyLXJhZGl1czogMTNweDsgYmFja2dyb3VuZDogdmFyKC0td3otYm9yZGVyKTsgcG9zaXRpb246IHJlbGF0aXZlOyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIC4xNXM7IH0KLnN3IGkgeyBwb3NpdGlvbjogYWJzb2x1dGU7IHRvcDogM3B4OyBsZWZ0OiAzcHg7IHdpZHRoOiAyMHB4OyBoZWlnaHQ6IDIwcHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogdmFyKC0td3otdGV4dCk7IHRyYW5zaXRpb246IHRyYW5zZm9ybSAuMTVzOyB9Ci5zdy5vbiB7IGJhY2tncm91bmQ6IHZhcigtLXd6LWFjY2VudCk7IH0gLnN3Lm9uIGkgeyB0cmFuc2Zvcm06IHRyYW5zbGF0ZVgoMThweCk7IGJhY2tncm91bmQ6IHZhcigtLXd6LWJhc2UpOyB9Ci5waC1maWVsZCB7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogNnB4OyBwYWRkaW5nOiAxMnB4IDRweDsgYm9yZGVyLWJvdHRvbTogMXB4IHNvbGlkIHZhcigtLXd6LWJvcmRlcik7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGNvbG9yOiB2YXIoLS13ei10ZXh0LXNlY29uZGFyeSk7IH0KLnBoLWZpZWxkIGlucHV0IHsgbWluLWhlaWdodDogNDRweDsgcGFkZGluZzogMCAxMnB4OyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyBib3JkZXItcmFkaXVzOiAxMHB4OyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1zdXJmYWNlKTsgY29sb3I6IHZhcigtLXd6LXRleHQpOyBmb250OiBpbmhlcml0OyB9Ci5waC1hY3RzIHsgZGlzcGxheTogZmxleDsgZmxleC13cmFwOiB3cmFwOyBnYXA6IDhweDsgbWFyZ2luLXRvcDogMTRweDsgfQoucGgtYnRuIHsgcGFkZGluZzogMTBweCAxNnB4OyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyBib3JkZXItcmFkaXVzOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1zdXJmYWNlKTsgY29sb3I6IHZhcigtLXd6LXRleHQpOyBmb250OiBpbmhlcml0OyBmb250LXdlaWdodDogNjAwOyB9Ci5waC1idG4ucHJpbWFyeSB7IGFsaWduLXNlbGY6IGZsZXgtc3RhcnQ7IG1hcmdpbjogMCA0cHggMTBweDsgYmFja2dyb3VuZDogdmFyKC0td3otYWNjZW50KTsgYm9yZGVyLWNvbG9yOiB2YXIoLS13ei1hY2NlbnQpOyBjb2xvcjogdmFyKC0td3otYmFzZSk7IH0KLnBoLWJ0bi5naG9zdCB7IGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OyBjb2xvcjogdmFyKC0td3otdGV4dC1zZWNvbmRhcnkpOyB9Ci5waC1idG4uZGFuZ2VyIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS13ei13YXJuaW5nKTsgY29sb3I6IHZhcigtLXd6LXdhcm5pbmcpOyBiYWNrZ3JvdW5kOiB0cmFuc3BhcmVudDsgfQoucGgtbGluayB7IHBhZGRpbmc6IDA7IGJvcmRlcjogMDsgYmFja2dyb3VuZDogdHJhbnNwYXJlbnQ7IGNvbG9yOiB2YXIoLS13ei1hY2NlbnQpOyB0ZXh0LWRlY29yYXRpb246IHVuZGVybGluZTsgZm9udDogaW5oZXJpdDsgfQoucGgtYWxidW1zIHsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgcGFkZGluZzogMnB4IDAgNnB4IDRweDsgYm9yZGVyLWJvdHRvbTogMXB4IHNvbGlkIHZhcigtLXd6LWJvcmRlcik7IH0KLnBoLWFsYnVtIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBwYWRkaW5nOiA4cHggNHB4OyBib3JkZXI6IDA7IGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OyBjb2xvcjogdmFyKC0td3otdGV4dCk7IHRleHQtYWxpZ246IGxlZnQ7IGZvbnQ6IGluaGVyaXQ7IH0KLnBoLWNoZWNrIHsgZmxleDogMCAwIDIwcHg7IGhlaWdodDogMjBweDsgYm9yZGVyLXJhZGl1czogNnB4OyBib3JkZXI6IDEuNXB4IHNvbGlkIHZhcigtLXd6LXRleHQtbXV0ZWQpOyB9Ci5waC1hbGJ1bVthcmlhLWNoZWNrZWQ9InRydWUiXSAucGgtY2hlY2sgeyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1hY2NlbnQpOyBib3JkZXItY29sb3I6IHZhcigtLXd6LWFjY2VudCk7IGJveC1zaGFkb3c6IGluc2V0IDAgMCAwIDNweCB2YXIoLS13ei1iYXNlKTsgfQoucGgtcXVldWVkIHsgbWFyZ2luLXRvcDogMnB4OyBwYWRkaW5nOiAxMHB4IDEycHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGJhY2tncm91bmQ6IHZhcigtLXd6LXN1cmZhY2UpOyBjb2xvcjogdmFyKC0td3otdGV4dCk7IH0KCi8qIE1vbnRobHkgcHJpbnQgcmV2aWV3IChhcHAgc3dpdGNoIHByaW50LXJldmlldykuIEtpdCB2YXJpYWJsZXMgb25seS4gKi8KLnBoLW1vbnRoIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBzcGFjZS1iZXR3ZWVuOyBnYXA6IDhweDsgbWFyZ2luOiAwIDAgOHB4OyB9Ci5waC1tb250aCBiIHsgZmxleDogMTsgdGV4dC1hbGlnbjogY2VudGVyOyBmb250LXNpemU6IHZhcigtLXd6LXRleHQtbGcpOyB9Ci5waC1tb250aCAud3otYnV0dG9uIHsgbWluLXdpZHRoOiA0NHB4OyBwYWRkaW5nOiAwIDEycHg7IGZvbnQtc2l6ZTogMS4zcmVtOyB9Ci5waC1wcmludC1ncmlkIHsgbGlzdC1zdHlsZTogbm9uZTsgbWFyZ2luOiAwIDAgMTJweDsgcGFkZGluZzogMDsgZGlzcGxheTogZ3JpZDsgZ2FwOiA2cHg7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDMsIG1pbm1heCgwLCAxZnIpKTsgfQpAY29udGFpbmVyIHd6LWFwcCAobWluLXdpZHRoOiA2MDBweCkgeyAucGgtcHJpbnQtZ3JpZCB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDQsIG1pbm1heCgwLCAxZnIpKTsgfSB9Ci5waC1wcmludC1waWNrIHsgcG9zaXRpb246IHJlbGF0aXZlOyBkaXNwbGF5OiBibG9jazsgd2lkdGg6IDEwMCU7IGFzcGVjdC1yYXRpbzogMTsgcGFkZGluZzogMDsgYm9yZGVyOiAycHggc29saWQgdHJhbnNwYXJlbnQ7IGJvcmRlci1yYWRpdXM6IDEwcHg7IG92ZXJmbG93OiBoaWRkZW47IGJhY2tncm91bmQ6IHZhcigtLXd6LXN1cmZhY2UpOyBjdXJzb3I6IHBvaW50ZXI7IH0KLnBoLXByaW50LXBpY2sgaW1nIHsgZGlzcGxheTogYmxvY2s7IHdpZHRoOiAxMDAlOyBoZWlnaHQ6IDEwMCU7IG9iamVjdC1maXQ6IGNvdmVyOyBvcGFjaXR5OiAuNDU7IHRyYW5zaXRpb246IG9wYWNpdHkgLjEyczsgfQoucGgtcHJpbnQtcGlja1thcmlhLXByZXNzZWQ9InRydWUiXSB7IGJvcmRlci1jb2xvcjogdmFyKC0td3otYWNjZW50KTsgfQoucGgtcHJpbnQtcGlja1thcmlhLXByZXNzZWQ9InRydWUiXSBpbWcgeyBvcGFjaXR5OiAxOyB9Ci5waC1wcmludC1tYXJrIHsgcG9zaXRpb246IGFic29sdXRlOyB0b3A6IDZweDsgcmlnaHQ6IDZweDsgd2lkdGg6IDIycHg7IGhlaWdodDogMjJweDsgYm9yZGVyLXJhZGl1czogNTAlOyBib3JkZXI6IDEuNXB4IHNvbGlkIHZhcigtLXd6LXRleHQtbXV0ZWQpOyBiYWNrZ3JvdW5kOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otYmFzZSkgNjAlLCB0cmFuc3BhcmVudCk7IGRpc3BsYXk6IGdyaWQ7IHBsYWNlLWl0ZW1zOiBjZW50ZXI7IH0KLnBoLXByaW50LW1hcmsgc3ZnIHsgd2lkdGg6IDE0cHg7IGhlaWdodDogMTRweDsgZmlsbDogbm9uZTsgc3Ryb2tlOiB2YXIoLS13ei1iYXNlKTsgc3Ryb2tlLXdpZHRoOiAzOyBvcGFjaXR5OiAwOyB9Ci5waC1wcmludC1waWNrW2FyaWEtcHJlc3NlZD0idHJ1ZSJdIC5waC1wcmludC1tYXJrIHsgYmFja2dyb3VuZDogdmFyKC0td3otYWNjZW50KTsgYm9yZGVyLWNvbG9yOiB2YXIoLS13ei1hY2NlbnQpOyB9Ci5waC1wcmludC1waWNrW2FyaWEtcHJlc3NlZD0idHJ1ZSJdIC5waC1wcmludC1tYXJrIHN2ZyB7IG9wYWNpdHk6IDE7IH0KLnBoLXByaW50LWNhcCB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgbGVmdDogMDsgcmlnaHQ6IDA7IGJvdHRvbTogMDsgcGFkZGluZzogMnB4IDZweDsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgdGV4dC1hbGlnbjogbGVmdDsgY29sb3I6IHZhcigtLXd6LXRleHQpOyBiYWNrZ3JvdW5kOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otYmFzZSkgNjUlLCB0cmFuc3BhcmVudCk7IH0KLnBoLXByaW50LW5vdGUgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBnYXA6IDZweDsgbWFyZ2luOiAwIDAgMTBweDsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgY29sb3I6IHZhcigtLXd6LXRleHQtc2Vjb25kYXJ5KTsgfQoucGgtcHJpbnQtbm90ZSB0ZXh0YXJlYSB7IGhlaWdodDogYXV0bzsgbWluLWhlaWdodDogNjRweDsgcGFkZGluZzogMTBweCAxNHB4OyBib3JkZXItcmFkaXVzOiAxMnB4OyByZXNpemU6IHZlcnRpY2FsOyB9Ci5waC1wcmludC1uZXZlciB7IGNvbG9yOiB2YXIoLS13ei10ZXh0LW11dGVkKTsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgfQojcHJpbnQtZm9ybVtoaWRkZW5dIHsgZGlzcGxheTogbm9uZTsgfQo=', 'boot.js': 'LyogUnVucyBiZWZvcmUgZmlyc3QgcGFpbnQ6IGEgaG9zdGVkIGFwcCBuZXZlciBmbGFzaGVzIGEgc3RhbmRhbG9uZSBjYW52YXMgd2hpbGUgdGhlIHNoZWxsJ3MKICogYXBwZWFyYW5jZSBtZXNzYWdlIGlzIG9uIGl0cyB3YXkuICovCnRyeSB7IGlmICh3aW5kb3cucGFyZW50ICE9PSB3aW5kb3cpIGRvY3VtZW50LmRvY3VtZW50RWxlbWVudC5jbGFzc0xpc3QuYWRkKCd3ei1ob3N0ZWQnKTsgfQpjYXRjaCAoZSkgeyBkb2N1bWVudC5kb2N1bWVudEVsZW1lbnQuY2xhc3NMaXN0LmFkZCgnd3otaG9zdGVkJyk7IH0K', 'wizard-tokens.css': 'LyogR2VuZXJhdGVkIGJ5IHN0eWxlL2dlbmVyYXRlX3Rva2Vucy5weSBmcm9tIHRva2Vucy5qc29uLiBEbyBub3QgZWRpdC4gKi8KOnJvb3QgewogIC0td3otZm9udC11aTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LXJlYWRpbmc6IEdlb3JnaWEsIHNlcmlmOwogIC0td3otZm9udC1kaXNwbGF5OiB1aS1zYW5zLXNlcmlmLCBzeXN0ZW0tdWksIC1hcHBsZS1zeXN0ZW0sICJTZWdvZSBVSSIsIFJvYm90bywgIkhlbHZldGljYSBOZXVlIiwgc2Fucy1zZXJpZjsKICAtLXd6LWZvbnQtbW9ubzogdWktbW9ub3NwYWNlLCAiU0YgTW9ubyIsIE1lbmxvLCBtb25vc3BhY2U7CiAgLS13ei1mb250LXNpemU6IDE1cHg7CiAgLS13ei1saW5lLWhlaWdodDogMS40OwogIC0td3otc3BhY2UtMTogNHB4OwogIC0td3otc3BhY2UtMjogOHB4OwogIC0td3otc3BhY2UtMzogMTJweDsKICAtLXd6LXNwYWNlLTQ6IDE2cHg7CiAgLS13ei1zcGFjZS01OiAyNHB4OwogIC0td3otcmFkaXVzOiAxNHB4OwogIC0td3otcmFkaXVzLXRpbGU6IDEycHg7CiAgLS13ei1yYWRpdXMtcGlsbDogMjBweDsKICAtLXd6LW1vdGlvbi1mYXN0OiAxNDBtczsKICAtLXd6LW1vdGlvbi1tZWRpdW06IDIyMG1zOwogIC0td3otZWFzZTogY3ViaWMtYmV6aWVyKC4yLCAuOSwgLjI1LCAxKTsKICAtLXd6LWRhbmdlcjogI2ZmNmI4NjsKICAtLXd6LXN1Y2Nlc3M6ICM0ZmYwYjA7CiAgLS13ei13YXJuaW5nOiAjZmZiZTU1OwogIC0td3otc3VjY2Vzcy1saWdodDogIzEwNmEzZjsKICAtLXd6LXdhcm5pbmctbGlnaHQ6ICM5YTQwMDY7CiAgLS13ei10ZXh0LXNtOiAxM3B4OwogIC0td3otdGV4dC1tZDogMTRweDsKICAtLXd6LXRleHQtbGc6IDE2cHg7CiAgLS13ei10ZXh0LWhlYWRpbmc6IDE3cHg7CiAgLS13ei10ZXh0LXRpdGxlOiAyNnB4OwogIC0td3otdGFyZ2V0OiA0NHB4OwogIC0td3otaWNvbi1zaXplOiAxOHB4OwogIC0td3otcGFuZWw6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei10ZXh0KSAxMCUsIHZhcigtLXd6LWJhc2UpKTsKICAtLXd6LWNhbnZhczogdmFyKC0td3otYmFzZSk7CiAgLS13ei1iYXNlOiAjMDUwMzBjOwogIC0td3otY2hyb21lOiByZ2JhKDEyLDgsMjYsLjgyKTsKICAtLXd6LW92ZXJsYXk6IHJnYmEoMjgsMTksNTgsLjcyKTsKICAtLXd6LXN1cmZhY2U6IGNvbG9yLW1peChpbiBzcmdiLCAjZjNlZmZmIDYlLCB0cmFuc3BhcmVudCk7CiAgLS13ei1zdXJmYWNlLXJhaXNlZDogY29sb3ItbWl4KGluIHNyZ2IsICNmM2VmZmYgMTAlLCB0cmFuc3BhcmVudCk7CiAgLS13ei10ZXh0OiAjZjNlZmZmOwogIC0td3otdGV4dC1zZWNvbmRhcnk6ICNiOWIwZDg7CiAgLS13ei10ZXh0LW11dGVkOiAjN2Q3NGEzOwogIC0td3otYm9yZGVyOiByZ2JhKDIwNSwxOTAsMjU1LC4yNCk7CiAgLS13ei1hY2NlbnQ6ICNiOTliZmY7CiAgLS13ei1hY2NlbnQtc29mdDogY29sb3ItbWl4KGluIHNyZ2IsICNiOTliZmYgMTYlLCB0cmFuc3BhcmVudCk7CiAgLS13ei1vbi1hY2NlbnQ6ICMwNTAzMGM7CiAgLS13ei1yZWFkZXItc3VyZmFjZTogdHJhbnNwYXJlbnQ7Cn0KOnJvb3QgeyBjb2xvci1zY2hlbWU6IGRhcms7IH0KaHRtbFtkYXRhLXNraW49Im5lYnVsYSJdIHsKICAtLXd6LWZvbnQtdWk6IHVpLXNhbnMtc2VyaWYsIHN5c3RlbS11aSwgLWFwcGxlLXN5c3RlbSwgIlNlZ29lIFVJIiwgUm9ib3RvLCAiSGVsdmV0aWNhIE5ldWUiLCBzYW5zLXNlcmlmOwogIC0td3otZm9udC1yZWFkaW5nOiBHZW9yZ2lhLCBzZXJpZjsKICAtLXd6LWZvbnQtZGlzcGxheTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LW1vbm86IHVpLW1vbm9zcGFjZSwgIlNGIE1vbm8iLCBNZW5sbywgbW9ub3NwYWNlOwogIC0td3otZm9udC1zaXplOiAxNXB4OwogIC0td3otbGluZS1oZWlnaHQ6IDEuNDsKICAtLXd6LXNwYWNlLTE6IDRweDsKICAtLXd6LXNwYWNlLTI6IDhweDsKICAtLXd6LXNwYWNlLTM6IDEycHg7CiAgLS13ei1zcGFjZS00OiAxNnB4OwogIC0td3otc3BhY2UtNTogMjRweDsKICAtLXd6LXJhZGl1czogMTRweDsKICAtLXd6LXJhZGl1cy10aWxlOiAxMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDIwcHg7CiAgLS13ei1tb3Rpb24tZmFzdDogMTQwbXM7CiAgLS13ei1tb3Rpb24tbWVkaXVtOiAyMjBtczsKICAtLXd6LWVhc2U6IGN1YmljLWJlemllciguMiwgLjksIC4yNSwgMSk7CiAgLS13ei1kYW5nZXI6ICNmZjZiODY7CiAgLS13ei1zdWNjZXNzOiAjNGZmMGIwOwogIC0td3otd2FybmluZzogI2ZmYmU1NTsKICAtLXd6LXN1Y2Nlc3MtbGlnaHQ6ICMxMDZhM2Y7CiAgLS13ei13YXJuaW5nLWxpZ2h0OiAjOWE0MDA2OwogIC0td3otdGV4dC1zbTogMTNweDsKICAtLXd6LXRleHQtbWQ6IDE0cHg7CiAgLS13ei10ZXh0LWxnOiAxNnB4OwogIC0td3otdGV4dC1oZWFkaW5nOiAxN3B4OwogIC0td3otdGV4dC10aXRsZTogMjZweDsKICAtLXd6LXRhcmdldDogNDRweDsKICAtLXd6LWljb24tc2l6ZTogMThweDsKICAtLXd6LXBhbmVsOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otdGV4dCkgMTAlLCB2YXIoLS13ei1iYXNlKSk7CiAgLS13ei1jYW52YXM6IHZhcigtLXd6LWJhc2UpOwogIC0td3otYmFzZTogIzA1MDMwYzsKICAtLXd6LWNocm9tZTogcmdiYSgxMiw4LDI2LC44Mik7CiAgLS13ei1vdmVybGF5OiByZ2JhKDI4LDE5LDU4LC43Mik7CiAgLS13ei1zdXJmYWNlOiBjb2xvci1taXgoaW4gc3JnYiwgI2YzZWZmZiA2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otc3VyZmFjZS1yYWlzZWQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZjNlZmZmIDEwJSwgdHJhbnNwYXJlbnQpOwogIC0td3otdGV4dDogI2YzZWZmZjsKICAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjYjliMGQ4OwogIC0td3otdGV4dC1tdXRlZDogIzdkNzRhMzsKICAtLXd6LWJvcmRlcjogcmdiYSgyMDUsMTkwLDI1NSwuMjQpOwogIC0td3otYWNjZW50OiAjYjk5YmZmOwogIC0td3otYWNjZW50LXNvZnQ6IGNvbG9yLW1peChpbiBzcmdiLCAjYjk5YmZmIDE2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otb24tYWNjZW50OiAjMDUwMzBjOwogIC0td3otcmVhZGVyLXN1cmZhY2U6IHRyYW5zcGFyZW50Owp9Cmh0bWxbZGF0YS1za2luPSJncm92ZSJdIHsKICAtLXd6LWZvbnQtdWk6IHVpLXNhbnMtc2VyaWYsIHN5c3RlbS11aSwgLWFwcGxlLXN5c3RlbSwgIlNlZ29lIFVJIiwgUm9ib3RvLCAiSGVsdmV0aWNhIE5ldWUiLCBzYW5zLXNlcmlmOwogIC0td3otZm9udC1yZWFkaW5nOiBHZW9yZ2lhLCBzZXJpZjsKICAtLXd6LWZvbnQtZGlzcGxheTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LW1vbm86IHVpLW1vbm9zcGFjZSwgIlNGIE1vbm8iLCBNZW5sbywgbW9ub3NwYWNlOwogIC0td3otZm9udC1zaXplOiAxNXB4OwogIC0td3otbGluZS1oZWlnaHQ6IDEuNDsKICAtLXd6LXNwYWNlLTE6IDRweDsKICAtLXd6LXNwYWNlLTI6IDhweDsKICAtLXd6LXNwYWNlLTM6IDEycHg7CiAgLS13ei1zcGFjZS00OiAxNnB4OwogIC0td3otc3BhY2UtNTogMjRweDsKICAtLXd6LXJhZGl1czogMTRweDsKICAtLXd6LXJhZGl1cy10aWxlOiAxMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDIwcHg7CiAgLS13ei1tb3Rpb24tZmFzdDogMTQwbXM7CiAgLS13ei1tb3Rpb24tbWVkaXVtOiAyMjBtczsKICAtLXd6LWVhc2U6IGN1YmljLWJlemllciguMiwgLjksIC4yNSwgMSk7CiAgLS13ei1kYW5nZXI6ICNmZjZiODY7CiAgLS13ei1zdWNjZXNzOiAjOWJlMzdhOwogIC0td3otd2FybmluZzogI2ZmOWE2YjsKICAtLXd6LXN1Y2Nlc3MtbGlnaHQ6ICMxMDZhM2Y7CiAgLS13ei13YXJuaW5nLWxpZ2h0OiAjOWE0MDA2OwogIC0td3otdGV4dC1zbTogMTNweDsKICAtLXd6LXRleHQtbWQ6IDE0cHg7CiAgLS13ei10ZXh0LWxnOiAxNnB4OwogIC0td3otdGV4dC1oZWFkaW5nOiAxN3B4OwogIC0td3otdGV4dC10aXRsZTogMjZweDsKICAtLXd6LXRhcmdldDogNDRweDsKICAtLXd6LWljb24tc2l6ZTogMThweDsKICAtLXd6LXBhbmVsOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otdGV4dCkgMTAlLCB2YXIoLS13ei1iYXNlKSk7CiAgLS13ei1jYW52YXM6IHZhcigtLXd6LWJhc2UpOwogIC0td3otYmFzZTogIzA1MGIwNzsKICAtLXd6LWNocm9tZTogcmdiYSg4LDE1LDEwLC44NCk7CiAgLS13ei1vdmVybGF5OiByZ2JhKDE2LDMyLDIyLC43OCk7CiAgLS13ei1zdXJmYWNlOiBjb2xvci1taXgoaW4gc3JnYiwgI2VlZjVlOSA2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otc3VyZmFjZS1yYWlzZWQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZWVmNWU5IDEwJSwgdHJhbnNwYXJlbnQpOwogIC0td3otdGV4dDogI2VlZjVlOTsKICAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjYjVjYmI5OwogIC0td3otdGV4dC1tdXRlZDogIzdmOWE4NjsKICAtLXd6LWJvcmRlcjogcmdiYSgyMDcsMjMyLDIwMCwuMjYpOwogIC0td3otYWNjZW50OiAjZTZjNjZiOwogIC0td3otYWNjZW50LXNvZnQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZTZjNjZiIDE2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otb24tYWNjZW50OiAjMDUwYjA3OwogIC0td3otcmVhZGVyLXN1cmZhY2U6IHRyYW5zcGFyZW50Owp9Cmh0bWxbZGF0YS1za2luPSJ0aWRlIl0gewogIC0td3otZm9udC11aTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LXJlYWRpbmc6IEdlb3JnaWEsIHNlcmlmOwogIC0td3otZm9udC1kaXNwbGF5OiB1aS1zYW5zLXNlcmlmLCBzeXN0ZW0tdWksIC1hcHBsZS1zeXN0ZW0sICJTZWdvZSBVSSIsIFJvYm90bywgIkhlbHZldGljYSBOZXVlIiwgc2Fucy1zZXJpZjsKICAtLXd6LWZvbnQtbW9ubzogdWktbW9ub3NwYWNlLCAiU0YgTW9ubyIsIE1lbmxvLCBtb25vc3BhY2U7CiAgLS13ei1mb250LXNpemU6IDE1cHg7CiAgLS13ei1saW5lLWhlaWdodDogMS40OwogIC0td3otc3BhY2UtMTogNHB4OwogIC0td3otc3BhY2UtMjogOHB4OwogIC0td3otc3BhY2UtMzogMTJweDsKICAtLXd6LXNwYWNlLTQ6IDE2cHg7CiAgLS13ei1zcGFjZS01OiAyNHB4OwogIC0td3otcmFkaXVzOiAxNHB4OwogIC0td3otcmFkaXVzLXRpbGU6IDEycHg7CiAgLS13ei1yYWRpdXMtcGlsbDogMjBweDsKICAtLXd6LW1vdGlvbi1mYXN0OiAxNDBtczsKICAtLXd6LW1vdGlvbi1tZWRpdW06IDIyMG1zOwogIC0td3otZWFzZTogY3ViaWMtYmV6aWVyKC4yLCAuOSwgLjI1LCAxKTsKICAtLXd6LWRhbmdlcjogI2ZmNmI4NjsKICAtLXd6LXN1Y2Nlc3M6ICM4ZmUzYTg7CiAgLS13ei13YXJuaW5nOiAjZmZjMDcwOwogIC0td3otc3VjY2Vzcy1saWdodDogIzEwNmEzZjsKICAtLXd6LXdhcm5pbmctbGlnaHQ6ICM5YTQwMDY7CiAgLS13ei10ZXh0LXNtOiAxM3B4OwogIC0td3otdGV4dC1tZDogMTRweDsKICAtLXd6LXRleHQtbGc6IDE2cHg7CiAgLS13ei10ZXh0LWhlYWRpbmc6IDE3cHg7CiAgLS13ei10ZXh0LXRpdGxlOiAyNnB4OwogIC0td3otdGFyZ2V0OiA0NHB4OwogIC0td3otaWNvbi1zaXplOiAxOHB4OwogIC0td3otcGFuZWw6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei10ZXh0KSAxMCUsIHZhcigtLXd6LWJhc2UpKTsKICAtLXd6LWNhbnZhczogdmFyKC0td3otYmFzZSk7CiAgLS13ei1iYXNlOiAjMDUyYTM2OwogIC0td3otY2hyb21lOiByZ2JhKDQsMjYsMzQsLjgyKTsKICAtLXd6LW92ZXJsYXk6IHJnYmEoOCw1Niw3MiwuNyk7CiAgLS13ei1zdXJmYWNlOiBjb2xvci1taXgoaW4gc3JnYiwgI2U4ZjZmOCA2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otc3VyZmFjZS1yYWlzZWQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZThmNmY4IDEwJSwgdHJhbnNwYXJlbnQpOwogIC0td3otdGV4dDogI2U4ZjZmODsKICAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjYTljZGQ0OwogIC0td3otdGV4dC1tdXRlZDogIzZmOThhMjsKICAtLXd6LWJvcmRlcjogcmdiYSgxNjgsMjMwLDIzOCwuMjcpOwogIC0td3otYWNjZW50OiAjN2ZkOGZmOwogIC0td3otYWNjZW50LXNvZnQ6IGNvbG9yLW1peChpbiBzcmdiLCAjN2ZkOGZmIDE2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otb24tYWNjZW50OiAjMDUyYTM2OwogIC0td3otcmVhZGVyLXN1cmZhY2U6IHRyYW5zcGFyZW50Owp9Cmh0bWxbZGF0YS1za2luPSJlbWJlciJdIHsKICAtLXd6LWZvbnQtdWk6IHVpLXNhbnMtc2VyaWYsIHN5c3RlbS11aSwgLWFwcGxlLXN5c3RlbSwgIlNlZ29lIFVJIiwgUm9ib3RvLCAiSGVsdmV0aWNhIE5ldWUiLCBzYW5zLXNlcmlmOwogIC0td3otZm9udC1yZWFkaW5nOiBHZW9yZ2lhLCBzZXJpZjsKICAtLXd6LWZvbnQtZGlzcGxheTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LW1vbm86IHVpLW1vbm9zcGFjZSwgIlNGIE1vbm8iLCBNZW5sbywgbW9ub3NwYWNlOwogIC0td3otZm9udC1zaXplOiAxNXB4OwogIC0td3otbGluZS1oZWlnaHQ6IDEuNDsKICAtLXd6LXNwYWNlLTE6IDRweDsKICAtLXd6LXNwYWNlLTI6IDhweDsKICAtLXd6LXNwYWNlLTM6IDEycHg7CiAgLS13ei1zcGFjZS00OiAxNnB4OwogIC0td3otc3BhY2UtNTogMjRweDsKICAtLXd6LXJhZGl1czogMTRweDsKICAtLXd6LXJhZGl1cy10aWxlOiAxMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDIwcHg7CiAgLS13ei1tb3Rpb24tZmFzdDogMTQwbXM7CiAgLS13ei1tb3Rpb24tbWVkaXVtOiAyMjBtczsKICAtLXd6LWVhc2U6IGN1YmljLWJlemllciguMiwgLjksIC4yNSwgMSk7CiAgLS13ei1kYW5nZXI6ICNmZjZiODY7CiAgLS13ei1zdWNjZXNzOiAjOGZlM2E4OwogIC0td3otd2FybmluZzogI2Y1ZTA0YTsKICAtLXd6LXN1Y2Nlc3MtbGlnaHQ6ICMxMDZhM2Y7CiAgLS13ei13YXJuaW5nLWxpZ2h0OiAjOWE0MDA2OwogIC0td3otdGV4dC1zbTogMTNweDsKICAtLXd6LXRleHQtbWQ6IDE0cHg7CiAgLS13ei10ZXh0LWxnOiAxNnB4OwogIC0td3otdGV4dC1oZWFkaW5nOiAxN3B4OwogIC0td3otdGV4dC10aXRsZTogMjZweDsKICAtLXd6LXRhcmdldDogNDRweDsKICAtLXd6LWljb24tc2l6ZTogMThweDsKICAtLXd6LXBhbmVsOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otdGV4dCkgMTAlLCB2YXIoLS13ei1iYXNlKSk7CiAgLS13ei1jYW52YXM6IHZhcigtLXd6LWJhc2UpOwogIC0td3otYmFzZTogIzA5MDUwNDsKICAtLXd6LWNocm9tZTogcmdiYSgxNiw5LDUsLjg1KTsKICAtLXd6LW92ZXJsYXk6IHJnYmEoMzYsMTksMTEsLjgpOwogIC0td3otc3VyZmFjZTogY29sb3ItbWl4KGluIHNyZ2IsICNmZmU5ZDYgNiUsIHRyYW5zcGFyZW50KTsKICAtLXd6LXN1cmZhY2UtcmFpc2VkOiBjb2xvci1taXgoaW4gc3JnYiwgI2ZmZTlkNiAxMCUsIHRyYW5zcGFyZW50KTsKICAtLXd6LXRleHQ6ICNmZmU5ZDY7CiAgLS13ei10ZXh0LXNlY29uZGFyeTogI2Q0YjQ5YzsKICAtLXd6LXRleHQtbXV0ZWQ6ICNhMDgwNmE7CiAgLS13ei1ib3JkZXI6IHJnYmEoMjU1LDE3NiwxMjIsLjI3KTsKICAtLXd6LWFjY2VudDogI2ZmYjQ3MDsKICAtLXd6LWFjY2VudC1zb2Z0OiBjb2xvci1taXgoaW4gc3JnYiwgI2ZmYjQ3MCAxNiUsIHRyYW5zcGFyZW50KTsKICAtLXd6LW9uLWFjY2VudDogIzA5MDUwNDsKICAtLXd6LXJlYWRlci1zdXJmYWNlOiB0cmFuc3BhcmVudDsKfQpodG1sW2RhdGEtc2tpbj0ic2lnbmFsIl0gewogIC0td3otZm9udC11aTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LXJlYWRpbmc6IEdlb3JnaWEsIHNlcmlmOwogIC0td3otZm9udC1kaXNwbGF5OiB1aS1zYW5zLXNlcmlmLCBzeXN0ZW0tdWksIC1hcHBsZS1zeXN0ZW0sICJTZWdvZSBVSSIsIFJvYm90bywgIkhlbHZldGljYSBOZXVlIiwgc2Fucy1zZXJpZjsKICAtLXd6LWZvbnQtbW9ubzogdWktbW9ub3NwYWNlLCAiU0YgTW9ubyIsIE1lbmxvLCBtb25vc3BhY2U7CiAgLS13ei1mb250LXNpemU6IDE1cHg7CiAgLS13ei1saW5lLWhlaWdodDogMS40OwogIC0td3otc3BhY2UtMTogNHB4OwogIC0td3otc3BhY2UtMjogOHB4OwogIC0td3otc3BhY2UtMzogMTJweDsKICAtLXd6LXNwYWNlLTQ6IDE2cHg7CiAgLS13ei1zcGFjZS01OiAyNHB4OwogIC0td3otcmFkaXVzOiAycHg7CiAgLS13ei1yYWRpdXMtdGlsZTogMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDJweDsKICAtLXd6LW1vdGlvbi1mYXN0OiAxNDBtczsKICAtLXd6LW1vdGlvbi1tZWRpdW06IDIyMG1zOwogIC0td3otZWFzZTogY3ViaWMtYmV6aWVyKC4yLCAuOSwgLjI1LCAxKTsKICAtLXd6LWRhbmdlcjogI2ZmNmI4NjsKICAtLXd6LXN1Y2Nlc3M6ICMzOWZmOGM7CiAgLS13ei13YXJuaW5nOiAjZmZmMDRkOwogIC0td3otc3VjY2Vzcy1saWdodDogIzEwNmEzZjsKICAtLXd6LXdhcm5pbmctbGlnaHQ6ICM5YTQwMDY7CiAgLS13ei10ZXh0LXNtOiAxM3B4OwogIC0td3otdGV4dC1tZDogMTRweDsKICAtLXd6LXRleHQtbGc6IDE2cHg7CiAgLS13ei10ZXh0LWhlYWRpbmc6IDE3cHg7CiAgLS13ei10ZXh0LXRpdGxlOiAyNnB4OwogIC0td3otdGFyZ2V0OiA0NHB4OwogIC0td3otaWNvbi1zaXplOiAxOHB4OwogIC0td3otcGFuZWw6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei10ZXh0KSAxMCUsIHZhcigtLXd6LWJhc2UpKTsKICAtLXd6LWNhbnZhczogdmFyKC0td3otYmFzZSk7CiAgLS13ei1iYXNlOiAjMDAwMDAwOwogIC0td3otY2hyb21lOiByZ2JhKDAsMTAsNCwuODYpOwogIC0td3otb3ZlcmxheTogcmdiYSgwLDIwLDgsLjc2KTsKICAtLXd6LXN1cmZhY2U6IGNvbG9yLW1peChpbiBzcmdiLCAjZDhmZmQ4IDYlLCB0cmFuc3BhcmVudCk7CiAgLS13ei1zdXJmYWNlLXJhaXNlZDogY29sb3ItbWl4KGluIHNyZ2IsICNkOGZmZDggMTAlLCB0cmFuc3BhcmVudCk7CiAgLS13ei10ZXh0OiAjZDhmZmQ4OwogIC0td3otdGV4dC1zZWNvbmRhcnk6ICM4ZmM3OWE7CiAgLS13ei10ZXh0LW11dGVkOiAjNGY4YTVhOwogIC0td3otYm9yZGVyOiByZ2JhKDU3LDI1NSwxNDAsLjMyKTsKICAtLXd6LWFjY2VudDogI2ZmYmU1NTsKICAtLXd6LWFjY2VudC1zb2Z0OiBjb2xvci1taXgoaW4gc3JnYiwgI2ZmYmU1NSAxNiUsIHRyYW5zcGFyZW50KTsKICAtLXd6LW9uLWFjY2VudDogIzAwMDAwMDsKICAtLXd6LXJlYWRlci1zdXJmYWNlOiB0cmFuc3BhcmVudDsKfQpodG1sLnd6LWhvc3RlZCwgaHRtbFtkYXRhLXd6LWhvc3RlZF0geyAtLXd6LWNhbnZhczogdHJhbnNwYXJlbnQ7IH0K', 'wizard-ui.css': 'LyogV2l6YXJkIFVJIGtpdCBmb3IgZ2VuZXJhdGVkIGFwcHMgKHN0eWxlLyBjYW5vbmljYWwgc291cmNlLCB2MSkuCiAqCiAqIFJ1bGUgMTogYSBob3N0ZWQgYXBwIHBhaW50cyBOTyBjYW52YXMuIEluIGEgd2luZG93IG9yIGZ1bGxzY3JlZW4gdGhlIHNoZWxsJ3Mgd2luZG93IGdsYXNzIGlzIHRoZQogKiAgICAgICAgIGNhbnZhcywgZXhhY3RseSBhcyBpdCBpcyBmb3IgQ2hhdHMuIE9ubHkgYSBzdGFuZGFsb25lIHRhYiBwYWludHMgdGhlIHNraW4ncyBiYWNrZ3JvdW5kLgogKiBSdWxlIDI6IHNwZWFrIENoYXRzJyB2aXN1YWwgbGFuZ3VhZ2U6IHN5c3RlbSBVSSB0eXBlIGF0IHRoZSBzaGVsbCdzIHNpemVzLCBpbmsgLyBpbmstMiAvIGluay0zCiAqICAgICAgICAgaGllcmFyY2h5LCBoYWlybGluZSBzZXBhcmF0b3JzLCBmYWludCB0aW50ZWQgZmlsbHMgaW5zdGVhZCBvZiBvcGFxdWUgY2FyZHMsIHBpbGwgaW5wdXRzLAogKiAgICAgICAgIGEgcm91bmQgYWNjZW50IGFjdGlvbi4gRXZlcnkgdmFsdWUgZGVyaXZlcyBmcm9tIHRoZSBhcHBlYXJhbmNlIHYxIHJvbGVzICgtLXd6LSopLCBzbyBhbGwKICogICAgICAgICB0aGUgZml2ZSBhY2NlcHRlZCBza2lucyBhbmQgZnV0dXJlIHNoZWxsLWFwcHJvdmVkIHNraW5zIGZsb3cgdGhyb3VnaCB3aXRob3V0IHBlci1hcHAgY29sb3VyIHdvcmsuCiAqIFJ1bGUgMzogbGF5b3V0IGFuZCBjb250ZW50IHN0YXkgdGhlIGFwcCdzIG93bi4gVXNlIHRoZXNlIGNsYXNzZXM7IGFkZCBhcHAgc3R5bGVzIGluIHN0eWxlcy5jc3MuCiAqIFJ1bGUgNDogbG9vayBvbmx5LiBUYWxraW5nIHRvIGFuIGFnZW50IGlzIHRoZSByZWFsIENoYXRzIHRocmVhZCAoV2l6YXJkQXBwLm5hdi5vcGVuVGhyZWFkKSwgbmV2ZXIgYQogKiAgICAgICAgIGNvbXBvc2VyIG9yIHRyYW5zY3JpcHQgYnVpbHQgZnJvbSB0aGVzZSBjbGFzc2VzICh3aXphcmQtdXggZG9jcy9DT01QT05FTlRTLm1kKS4KICogUnVsZSA1OiBza2lucyBtYXkgcmVzdHlsZSB0aGVzZSBjbGFzc2VzLiBIb3N0ZWQgYXBwcyBsb2FkIHRoZSBsaXZlIGNvcHkgb2YgdGhpcyBraXQgZnJvbSB0aGUgc2hlbGwKICogICAgICAgICAoL3dpemFyZC1vcy1ob21lL2tpdC93aXphcmQtdWkuY3NzKSBhbmQgdGhlIGFjdGl2ZSBza2luJ3Mgc3R5bGVzaGVldHMgYWZ0ZXIgdGhlIGFwcCdzIG93bi4KICoKICogVmFsdWVzIG1pcnJvciB3ZWIvY29yZS9jaGF0cy5jc3MgYW5kIGNvcmUvYmFzZS5jc3MgaW4gV2l6YXJkIFVYIChjaC1yb3csIGNoLXNlYy1oKS4gKi8KCjpyb290IHsKICAtLWluazogdmFyKC0td3otdGV4dCk7IC0taW5rLTI6IHZhcigtLXd6LXRleHQtc2Vjb25kYXJ5KTsgLS1pbmstMzogdmFyKC0td3otdGV4dC1tdXRlZCk7CiAgLS1saW5lLTI6IHZhcigtLXd6LWJvcmRlcik7IC0tbGluZTogY29sb3ItbWl4KGluIHNyZ2IsIHZhcigtLWxpbmUtMikgNTUlLCB0cmFuc3BhcmVudCk7CiAgLS1maWxsOiB2YXIoLS13ei1zdXJmYWNlKTsgLS1maWxsLTI6IHZhcigtLXd6LXN1cmZhY2UtcmFpc2VkKTsKICAtLWFjY2VudDogdmFyKC0td3otYWNjZW50KTsgLS1vbi1hY2NlbnQ6IHZhcigtLXd6LW9uLWFjY2VudCk7IC0tYWNjZW50LXNvZnQ6IHZhcigtLXd6LWFjY2VudC1zb2Z0KTsKICAtLWNhbnZhczogdmFyKC0td3otYmFzZSk7IC0tZGFuZ2VyOiB2YXIoLS13ei1kYW5nZXIpOwogIC0tcjogdmFyKC0td3otcmFkaXVzKTsgLS1yLXRpbGU6IHZhcigtLXd6LXJhZGl1cy10aWxlKTsgLS1yLXBpbGw6IHZhcigtLXd6LXJhZGl1cy1waWxsKTsKICAtLWVhc2U6IHZhcigtLXd6LWVhc2UpOyAtLWZvbnQ6IHZhcigtLXd6LWZvbnQtdWkpOwp9Cmh0bWxbZGF0YS1jb2xvci1zY2hlbWU9ImxpZ2h0Il0geyBjb2xvci1zY2hlbWU6IGxpZ2h0OyAtLWRhbmdlcjogdmFyKC0taW5rKTsgfQpodG1sW2RhdGEtc2tpbj0ic2lnbmFsIl0gLnd6LXNlYy1oIHsgZm9udC1mYW1pbHk6IHZhcigtLXd6LWZvbnQtbW9ubyk7IGxldHRlci1zcGFjaW5nOiAuMDhlbTsgfQpodG1sIHsgYmFja2dyb3VuZDogdmFyKC0td3otYmFzZSk7IH0KaHRtbC53ei1ob3N0ZWQsIGh0bWxbZGF0YS13ei1ob3N0ZWRdIHsgYmFja2dyb3VuZDogdHJhbnNwYXJlbnQ7IH0KCiosICo6OmJlZm9yZSwgKjo6YWZ0ZXIgeyBib3gtc2l6aW5nOiBib3JkZXItYm94OyB9Cmh0bWwsIGJvZHkgeyBoZWlnaHQ6IDEwMCU7IG1hcmdpbjogMDsgfQpodG1sIHsgLXdlYmtpdC10ZXh0LXNpemUtYWRqdXN0OiAxMDAlOyAtd2Via2l0LXRhcC1oaWdobGlnaHQtY29sb3I6IHRyYW5zcGFyZW50OyB9CmJvZHkgeyBmb250OiAxNXB4LzEuNCB2YXIoLS1mb250KTsgY29sb3I6IHZhcigtLWluayk7IGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OyAtd2Via2l0LWZvbnQtc21vb3RoaW5nOiBhbnRpYWxpYXNlZDsgb3ZlcnNjcm9sbC1iZWhhdmlvcjogbm9uZTsgfQpodG1sW2RhdGEtcHJlc2VudGF0aW9uPSJzdGFuZGFsb25lIl06bm90KC53ei1ob3N0ZWQpIGJvZHkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1jYW52YXMpOyB9CmJ1dHRvbiwgaW5wdXQsIHRleHRhcmVhLCBzZWxlY3QgeyBmb250OiBpbmhlcml0OyBjb2xvcjogaW5oZXJpdDsgfQpidXR0b24geyBjdXJzb3I6IHBvaW50ZXI7IGJvcmRlcjogMDsgYmFja2dyb3VuZDogbm9uZTsgcGFkZGluZzogMDsgfQo6Zm9jdXMtdmlzaWJsZSB7IG91dGxpbmU6IDJweCBzb2xpZCB2YXIoLS1hY2NlbnQpOyBvdXRsaW5lLW9mZnNldDogMnB4OyB9Ci53ei1zciB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgd2lkdGg6IDFweDsgaGVpZ2h0OiAxcHg7IG92ZXJmbG93OiBoaWRkZW47IGNsaXAtcGF0aDogaW5zZXQoNTAlKTsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQoKLyogLS0tLS0tLS0tLSBmcmFtZTogaGVhZCAvIHNjcm9sbCAvIGZvb3RlciAtLS0tLS0tLS0tICovCi8qIFRoZSB3aW5kb3cgaXMgdGhlIHBhZ2UgKEFQUC1ERVNJR04tUlVMRVMubWQgcnVsZSAxLTIpOiBubyBtYXgtd2lkdGg7IGNvbHVtbnMgZ3JvdyB3aXRoIHRoZSB3aW5kb3csIG1lYXN1cmVkIG9uIC53ei1hcHAuICovCi53ei1hcHAgeyAtLXd6LWd1dHRlcjogMTJweDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgaGVpZ2h0OiAxMDAlOyBjb250YWluZXI6IHd6LWFwcCAvIGlubGluZS1zaXplOyB9CkBjb250YWluZXIgd3otYXBwIChtaW4td2lkdGg6IDYwMHB4KSB7IC53ei1zY3JvbGwsIC53ei1oZWFkIHsgLS13ei1ndXR0ZXI6IDIwcHg7IH0gfQpAY29udGFpbmVyIHd6LWFwcCAobWluLXdpZHRoOiA5NjBweCkgeyAud3otc2Nyb2xsLCAud3otaGVhZCB7IC0td3otZ3V0dGVyOiAyOHB4OyB9IH0KLnd6LWhlYWQgeyBmbGV4OiBub25lOyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogZmxleC1lbmQ7IGdhcDogMTJweDsgcGFkZGluZzogY2FsYyh2YXIoLS13b3MtY3V0b3V0LCAwcHgpICsgMThweCkgMThweCA2cHg7IH0KLnd6LWhlYWQgaDEgeyBtYXJnaW46IDA7IGZvbnQtc2l6ZTogMjZweDsgbGluZS1oZWlnaHQ6IDEuMTU7IGZvbnQtd2VpZ2h0OiA3NTA7IGxldHRlci1zcGFjaW5nOiAtLjAyZW07IH0KLnd6LWhlYWQgcCB7IG1hcmdpbjogMCAwIDNweCBhdXRvOyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLyogSW4gYSB3aW5kb3cgdGhlIHNoZWxsJ3MgdGl0bGUgYmFyIGFscmVhZHkgbmFtZXMgdGhlIGFwcC4gKi8KaHRtbFtkYXRhLXByZXNlbnRhdGlvbj0id2luZG93Il0gLnd6LWhlYWQgeyBkaXNwbGF5OiBub25lOyB9Ci53ei1zY3JvbGwgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBvdmVyZmxvdy15OiBhdXRvOyBwYWRkaW5nOiA0cHggdmFyKC0td3otZ3V0dGVyLCAxMHB4KSAxMnB4OyBzY3JvbGxiYXItd2lkdGg6IG5vbmU7IH0KLnd6LXNjcm9sbDo6LXdlYmtpdC1zY3JvbGxiYXIgeyBkaXNwbGF5OiBub25lOyB9CgovKiAtLS0tLS0tLS0tIHNlY3Rpb24gbGFiZWw6IC5jaC1zZWMtaCAtLS0tLS0tLS0tICovCi53ei1zZWMgKyAud3otc2VjIHsgYm9yZGVyLXRvcDogMXB4IHNvbGlkIHZhcigtLWxpbmUpOyB9Ci53ei1zZWMtaCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTBweDsgbWFyZ2luOiAwOyBwYWRkaW5nOiAxMnB4IDhweCA4cHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGZvbnQtd2VpZ2h0OiA4MDA7IGxldHRlci1zcGFjaW5nOiAuMTRlbTsgdGV4dC10cmFuc2Zvcm06IHVwcGVyY2FzZTsgY29sb3I6IHZhcigtLWluay0yKTsgfQoud3otc2VjLWggc21hbGwgeyBmb250LXNpemU6IHZhcigtLXd6LXRleHQtc20pOyBmb250LXdlaWdodDogODAwOyBsZXR0ZXItc3BhY2luZzogLjA1ZW07IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LWNvdW50IHsgbWFyZ2luLWxlZnQ6IGF1dG87IHBhZGRpbmc6IDFweCA4cHg7IGJvcmRlci1yYWRpdXM6IDk5cHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGxldHRlci1zcGFjaW5nOiAuMDRlbTsgY29sb3I6IHZhcigtLWluay0yKTsgYmFja2dyb3VuZDogdmFyKC0tZmlsbCk7IH0KLnd6LWNvdW50LmFsbCB7IGNvbG9yOiB2YXIoLS1vbi1hY2NlbnQpOyBiYWNrZ3JvdW5kOiB2YXIoLS1hY2NlbnQpOyB9CgovKiAtLS0tLS0tLS0tIHJvd3M6IC5jaC1yb3cgLS0tLS0tLS0tLSAqLwoud3otbGlzdCB7IGxpc3Qtc3R5bGU6IG5vbmU7IG1hcmdpbjogMDsgcGFkZGluZzogMDsgfQoud3otcm93IHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyB3aWR0aDogMTAwJTsgbWluLWhlaWdodDogNTJweDsgcGFkZGluZzogOHB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yKTsgdGV4dC1hbGlnbjogbGVmdDsgdHJhbnNpdGlvbjogYmFja2dyb3VuZCAxNDBtczsgfQoud3otcm93OmFjdGl2ZSB7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB9CkBtZWRpYSAoaG92ZXI6IGhvdmVyKSB7IC53ei1yb3c6aG92ZXIgeyBiYWNrZ3JvdW5kOiB2YXIoLS1maWxsKTsgfSB9Ci53ei1yb3cgLnd6LW1haW4geyBmbGV4OiAxOyBtaW4td2lkdGg6IDA7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogMnB4OyB9Ci53ei1yb3cgLnd6LW1haW4gYiB7IGZvbnQtc2l6ZTogMTVweDsgZm9udC13ZWlnaHQ6IDYwMDsgY29sb3I6IHZhcigtLWluayk7IG92ZXJmbG93LXdyYXA6IGFueXdoZXJlOyB9Ci53ei1yb3cgLnd6LW1haW4gc21hbGwgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LXRpbGUgeyBmbGV4OiBub25lOyB3aWR0aDogMzZweDsgaGVpZ2h0OiAzNnB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yLXRpbGUpOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyBmb250LXNpemU6IDE4cHg7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB9CgovKiByb3VuZCBjaGVjaywgMjZweCBpbnNpZGUgYSAzNnB4IHRpbGUtc2l6ZWQgc2xvdCAodGhlIHdob2xlIHJvdyBpcyB0aGUgNDRweCsgdGFyZ2V0KSAqLwoud3otY2hlY2sgeyBmbGV4OiBub25lOyB3aWR0aDogMzZweDsgaGVpZ2h0OiAzNnB4OyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyB9Ci53ei1jaGVjayBpIHsgd2lkdGg6IDI2cHg7IGhlaWdodDogMjZweDsgYm9yZGVyLXJhZGl1czogNTAlOyBib3JkZXI6IDJweCBzb2xpZCB2YXIoLS1saW5lLTIpOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIDE2MG1zIHZhcigtLWVhc2UpLCBib3JkZXItY29sb3IgMTYwbXMsIHRyYW5zZm9ybSAyMjBtcyB2YXIoLS1lYXNlKTsgfQoud3otY2hlY2sgc3ZnIHsgd2lkdGg6IDE0cHg7IGhlaWdodDogMTRweDsgZmlsbDogbm9uZTsgc3Ryb2tlOiB2YXIoLS1vbi1hY2NlbnQpOyBzdHJva2Utd2lkdGg6IDM7IHN0cm9rZS1saW5lY2FwOiByb3VuZDsgc3Ryb2tlLWxpbmVqb2luOiByb3VuZDsgb3BhY2l0eTogMDsgdHJhbnNpdGlvbjogb3BhY2l0eSAxMjBtczsgfQpodG1sW2RhdGEtc2tpbj0ic2lnbmFsIl0gLnd6LWNoZWNrIGkgeyBib3JkZXItcmFkaXVzOiAycHg7IH0KW2FyaWEtcHJlc3NlZD0idHJ1ZSJdIC53ei1jaGVjayBpIHsgYmFja2dyb3VuZDogdmFyKC0tYWNjZW50KTsgYm9yZGVyLWNvbG9yOiB2YXIoLS1hY2NlbnQpOyB0cmFuc2Zvcm06IHNjYWxlKDEuMDQpOyB9ClthcmlhLXByZXNzZWQ9InRydWUiXSAud3otY2hlY2sgc3ZnIHsgb3BhY2l0eTogMTsgfQpbYXJpYS1wcmVzc2VkPSJ0cnVlIl0gLnd6LW1haW4gYiB7IGNvbG9yOiB2YXIoLS1pbmstMik7IH0KLnd6LXJvdzpkaXNhYmxlZCB7IG9wYWNpdHk6IC42OyBjdXJzb3I6IHByb2dyZXNzOyB9CgovKiAtLS0tLS0tLS0tIGVtcHR5IHN0YXRlIC0tLS0tLS0tLS0gKi8KLnd6LWVtcHR5IHsgcGFkZGluZzogNDRweCAyNHB4IDI0cHg7IHRleHQtYWxpZ246IGNlbnRlcjsgY29sb3I6IHZhcigtLWluay0zKTsgfQoud3otZW1wdHkgLnd6LXRpbGUgeyBtYXJnaW46IDAgYXV0byAxNHB4OyB3aWR0aDogNTJweDsgaGVpZ2h0OiA1MnB4OyBib3JkZXItcmFkaXVzOiAxOHB4OyBmb250LXNpemU6IDI0cHg7IGNvbG9yOiB2YXIoLS1hY2NlbnQpOyB9Ci53ei1lbXB0eSBzdHJvbmcgeyBkaXNwbGF5OiBibG9jazsgbWFyZ2luLWJvdHRvbTogNHB4OyBmb250LXNpemU6IDE2cHg7IGZvbnQtd2VpZ2h0OiA3MDA7IGNvbG9yOiB2YXIoLS1pbmspOyB9Ci53ei1lbXB0eSBwIHsgbWFyZ2luOiAwIGF1dG87IG1heC13aWR0aDogMzBjaDsgZm9udC1zaXplOiAxNHB4OyBsaW5lLWhlaWdodDogMS40OyB9CgovKiAtLS0tLS0tLS0tIHNpbmdsZS1saW5lIGFkZCBmaWVsZCAoZm9ybXMgc3VjaCBhcyAiTmV3IGhhYml0IjsgbmV2ZXIgYSBjaGF0KSAtLS0tLS0tLS0tCiAqIC53ei1jb21wb3NlIC8gLnd6LWlucHV0IC8gLnd6LXNlbmQgYXJlIHRoZSBwcmUtMjMgT2N0IDIwMjYgbmFtZXMsIGtlcHQgc28gZXhpc3RpbmcgYXBwcyBrZWVwIHRoZWlyIGxvb2suICovCi53ei1maWVsZC1yb3csIC53ei1jb21wb3NlIHsgZmxleDogbm9uZTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA4cHg7IHBhZGRpbmc6IDhweCAxMHB4IGNhbGMoMTBweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tLCAwcHgpKTsgYm9yZGVyLXRvcDogMXB4IHNvbGlkIHZhcigtLWxpbmUpOyB9Ci53ei1maWVsZCwgLnd6LWlucHV0IHsgZmxleDogMTsgbWluLXdpZHRoOiAwOyBoZWlnaHQ6IDQ0cHg7IHBhZGRpbmc6IDAgMTZweDsgYm9yZGVyLXJhZGl1czogdmFyKC0tci1waWxsKTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZS0yKTsgb3V0bGluZTogMDsgYmFja2dyb3VuZDogdmFyKC0tZmlsbCk7IGNvbG9yOiB2YXIoLS1pbmspOyBmb250LXNpemU6IDE1cHg7IH0KLnd6LWZpZWxkOjpwbGFjZWhvbGRlciwgLnd6LWlucHV0OjpwbGFjZWhvbGRlciB7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LWZpZWxkOmZvY3VzLCAud3otaW5wdXQ6Zm9jdXMgeyBib3JkZXItY29sb3I6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS1hY2NlbnQpIDYwJSwgdHJhbnNwYXJlbnQpOyB9Ci53ei1maWVsZC1nbywgLnd6LXNlbmQgeyBmbGV4OiBub25lOyB3aWR0aDogNDRweDsgaGVpZ2h0OiA0NHB4OyBib3JkZXItcmFkaXVzOiA1MCU7IGRpc3BsYXk6IGdyaWQ7IHBsYWNlLWl0ZW1zOiBjZW50ZXI7IGJhY2tncm91bmQ6IHZhcigtLWFjY2VudCk7IGNvbG9yOiB2YXIoLS1vbi1hY2NlbnQpOyB0cmFuc2l0aW9uOiBvcGFjaXR5IDE0MG1zLCB0cmFuc2Zvcm0gMTQwbXM7IH0KaHRtbFtkYXRhLXNraW49InNpZ25hbCJdIDppcygud3otZmllbGQtZ28sIC53ei1zZW5kKSB7IGJvcmRlci1yYWRpdXM6IDJweDsgfQo6aXMoLnd6LWZpZWxkLWdvLCAud3otc2VuZCkgc3ZnIHsgd2lkdGg6IDE4cHg7IGhlaWdodDogMThweDsgZmlsbDogbm9uZTsgc3Ryb2tlOiBjdXJyZW50Q29sb3I7IHN0cm9rZS13aWR0aDogMi42OyBzdHJva2UtbGluZWNhcDogcm91bmQ7IH0KOmlzKC53ei1maWVsZC1nbywgLnd6LXNlbmQpOmRpc2FibGVkIHsgb3BhY2l0eTogLjQ7IGN1cnNvcjogZGVmYXVsdDsgfQo6aXMoLnd6LWZpZWxkLWdvLCAud3otc2VuZCk6bm90KDpkaXNhYmxlZCk6YWN0aXZlIHsgdHJhbnNmb3JtOiBzY2FsZSguOTQpOyB9CgovKiAtLS0tLS0tLS0tIHN0YXR1czogZXJyb3JzIGFyZSB2aXNpYmxlLCBjb25maXJtYXRpb25zIGFyZSBmb3Igc2NyZWVuIHJlYWRlcnMgLS0tLS0tLS0tLSAqLwoud3otc3RhdHVzIHsgZmxleDogbm9uZTsgbWFyZ2luOiAwOyBwYWRkaW5nOiAwIDE4cHggNnB4OyBtaW4taGVpZ2h0OiAwOyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1kYW5nZXIpOyB9Ci53ei1zdGF0dXM6ZW1wdHkgeyBwYWRkaW5nOiAwOyB9Ci53ei1zdGF0dXMubm90ZSB7IGNvbG9yOiB2YXIoLS1pbmstMik7IHRleHQtYWxpZ246IGNlbnRlcjsgfQoKLyogU3RhZ2luZyBob3N0cyBsYWJlbCB0aGUgcGFnZSBvbmNlOyBhIGhvc3RlZCBhcHAgbmV2ZXIgcmVwZWF0cyB0aGF0IGJhZGdlLiAqLwpodG1sLnd6LWhvc3RlZCAjd2l6LXN0YWdpbmctdGFnIHsgZGlzcGxheTogbm9uZSAhaW1wb3J0YW50OyB9CgpAbWVkaWEgKHByZWZlcnMtcmVkdWNlZC1tb3Rpb246IHJlZHVjZSkgeyAqLCAqOjpiZWZvcmUsICo6OmFmdGVyIHsgdHJhbnNpdGlvbjogbm9uZSAhaW1wb3J0YW50OyBhbmltYXRpb246IG5vbmUgIWltcG9ydGFudDsgfSB9CgovKiBTaGFyZWQgc2VtYW50aWMgcHJpbWl0aXZlczogdGludGVkIHN1cmZhY2VzLCBuZXZlciBhIHNlY29uZCBmdWxsLXBhZ2UgY2FudmFzLiAqLwoud3otY2FyZCB7IHBhZGRpbmc6IHZhcigtLXd6LXNwYWNlLTQpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS1saW5lKTsgYm9yZGVyLXJhZGl1czogdmFyKC0tcik7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB9Ci53ei1jYXJkID4gOmZpcnN0LWNoaWxkIHsgbWFyZ2luLXRvcDogMDsgfSAud3otY2FyZCA+IDpsYXN0LWNoaWxkIHsgbWFyZ2luLWJvdHRvbTogMDsgfQoud3otY2hpcCB7IGRpc3BsYXk6IGlubGluZS1mbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IHZhcigtLXd6LXNwYWNlLTIpOyBwYWRkaW5nOiA0cHggMTBweDsgYm9yZGVyLXJhZGl1czogdmFyKC0tci1waWxsKTsgYmFja2dyb3VuZDogdmFyKC0tZmlsbCk7IGNvbG9yOiB2YXIoLS1pbmstMik7IGZvbnQtc2l6ZTogMTNweDsgfQoud3otYmFyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiB2YXIoLS13ei1zcGFjZS0yKTsgcGFkZGluZzogOHB4IDEwcHg7IGJhY2tncm91bmQ6IHZhcigtLXd6LWNocm9tZSk7IGJvcmRlci1ib3R0b206IDFweCBzb2xpZCB2YXIoLS1saW5lKTsgYmFja2Ryb3AtZmlsdGVyOiBibHVyKDE2cHgpOyB9Ci53ei1idXR0b24geyBkaXNwbGF5OiBpbmxpbmUtZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IGJvcmRlcjogMDsgZm9udDogaW5oZXJpdDsgdGV4dC1kZWNvcmF0aW9uOiBub25lOyBjdXJzb3I6IHBvaW50ZXI7IGJveC1zaXppbmc6IGJvcmRlci1ib3g7IG1pbi1oZWlnaHQ6IDQ0cHg7IHBhZGRpbmc6IDhweCAxNnB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yLXRpbGUpOyBiYWNrZ3JvdW5kOiB2YXIoLS1hY2NlbnQpOyBjb2xvcjogdmFyKC0tb24tYWNjZW50KTsgZm9udC13ZWlnaHQ6IDYwMDsgfQoud3otYnV0dG9uLnNlY29uZGFyeSwgLnd6LWJ1dHRvbi5xdWlldCB7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyBjb2xvcjogdmFyKC0taW5rKTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZS0yKTsgfQoud3otYnV0dG9uOmRpc2FibGVkIHsgb3BhY2l0eTogLjQ1OyBjdXJzb3I6IGRlZmF1bHQ7IH0KLyogLS0tLS0tLS0tLSBsYXlvdXQgaGVscGVycyAocnVsZXMgMSwgMiwgNSkgLS0tLS0tLS0tLSAqLwoud3otZ3JpZCB7IGRpc3BsYXk6IGdyaWQ7IGdhcDogdmFyKC0td3otc3BhY2UtMyk7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyOyB9CkBjb250YWluZXIgd3otYXBwIChtaW4td2lkdGg6IDYwMHB4KSB7IC53ei1ncmlkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoMiwgbWlubWF4KDAsIDFmcikpOyB9IH0KQGNvbnRhaW5lciB3ei1hcHAgKG1pbi13aWR0aDogOTYwcHgpIHsgLnd6LWdyaWQgeyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdChhdXRvLWZpbGwsIG1pbm1heCgzMDBweCwgMWZyKSk7IH0gfQoud3otcHJvc2UgeyBtYXgtd2lkdGg6IDcyY2g7IH0KLnd6LXBpbGwgeyBkaXNwbGF5OiBpbmxpbmUtZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IHBhZGRpbmc6IDNweCAxMHB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yLXBpbGwpOyBmb250LXNpemU6IDEzcHg7IGZvbnQtd2VpZ2h0OiA2NTA7IGNvbG9yOiB2YXIoLS1pbmstMik7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB3aGl0ZS1zcGFjZTogbm93cmFwOyB9Ci53ei1waWxsOjpiZWZvcmUgeyBjb250ZW50OiAiIjsgd2lkdGg6IDdweDsgaGVpZ2h0OiA3cHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogY3VycmVudENvbG9yOyB9Ci53ei1waWxsLm9rIHsgY29sb3I6IHZhcigtLXd6LXN1Y2Nlc3MsIHZhcigtLWFjY2VudCkpOyB9Ci53ei1waWxsLndhcm4geyBjb2xvcjogdmFyKC0td3otd2FybmluZywgdmFyKC0tZGFuZ2VyKSk7IH0KLyogTGlnaHQgc2NoZW1lczogZGVlcCBncmVlbiBhbmQgYnVybnQgb3JhbmdlIHRoYXQgcGFzcyA0LjU6MSBvbiBwYWxlIHBhZ2VzICh3aGl0ZSwgY3JlYW0sIHNlcGlhKS4gKi8KaHRtbFtkYXRhLWNvbG9yLXNjaGVtZT0ibGlnaHQiXSAud3otcGlsbC5vayB7IGNvbG9yOiB2YXIoLS13ei1zdWNjZXNzLWxpZ2h0LCB2YXIoLS1pbmspKTsgfQpodG1sW2RhdGEtY29sb3Itc2NoZW1lPSJsaWdodCJdIC53ei1waWxsLndhcm4geyBjb2xvcjogdmFyKC0td3otd2FybmluZy1saWdodCwgdmFyKC0taW5rKSk7IH0KLnd6LXBpbGwub2ZmIHsgY29sb3I6IHZhcigtLWluay0zKTsgfQoud3otbWV0ZXIgeyBkaXNwbGF5OiBncmlkOyBnYXA6IDRweDsgZm9udC1zaXplOiAxM3B4OyBjb2xvcjogdmFyKC0taW5rLTMpOyB9Ci53ei1tZXRlciA+IGkgeyBkaXNwbGF5OiBibG9jazsgaGVpZ2h0OiA2cHg7IGJvcmRlci1yYWRpdXM6IDNweDsgYmFja2dyb3VuZDogdmFyKC0tZmlsbC0yKTsgb3ZlcmZsb3c6IGhpZGRlbjsgfQoud3otbWV0ZXIgPiBpID4gYiB7IGRpc3BsYXk6IGJsb2NrOyBoZWlnaHQ6IDEwMCU7IHdpZHRoOiB2YXIoLS12LCAwJSk7IGJvcmRlci1yYWRpdXM6IGluaGVyaXQ7IGJhY2tncm91bmQ6IHZhcigtLWFjY2VudCk7IH0KLnd6LWNvbm4geyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IHZhcigtLXd6LXNwYWNlLTMpOyBwYWRkaW5nOiB2YXIoLS13ei1zcGFjZS00KTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZSk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXIpOyBiYWNrZ3JvdW5kOiB2YXIoLS1maWxsKTsgfQoud3otY29ubiAud3otdGlsZSB7IHdpZHRoOiA0OHB4OyBoZWlnaHQ6IDQ4cHg7IGJvcmRlci1yYWRpdXM6IDE0cHg7IH0KLnd6LWNvbm4gLnd6LW1haW4geyBmbGV4OiAxOyBtaW4td2lkdGg6IDA7IGRpc3BsYXk6IGdyaWQ7IGdhcDogM3B4OyB9Ci53ei1jb25uIC53ei1tYWluIGIgeyBmb250LXNpemU6IDE3cHg7IGZvbnQtd2VpZ2h0OiA3MDA7IH0KLnd6LWNvbm4gLnd6LW1haW4gc21hbGwgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LWFjdGlvbnMgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LXdyYXA6IHdyYXA7IGdhcDogdmFyKC0td3otc3BhY2UtMik7IHBhZGRpbmc6IHZhcigtLXd6LXNwYWNlLTMpIDA7IH0KLnd6LW1vcmUgeyBtYXJnaW4tdG9wOiB2YXIoLS13ei1zcGFjZS00KTsgY29sb3I6IHZhcigtLWluay0zKTsgZm9udC1zaXplOiAxM3B4OyB9Ci53ei1tb3JlID4gc3VtbWFyeSB7IGN1cnNvcjogcG9pbnRlcjsgcGFkZGluZzogOHB4OyBsaXN0LXN0eWxlOiBub25lOyBmb250LXdlaWdodDogNjUwOyBjb2xvcjogdmFyKC0taW5rLTIpOyB9Ci53ei1tb3JlID4gc3VtbWFyeTo6LXdlYmtpdC1kZXRhaWxzLW1hcmtlciB7IGRpc3BsYXk6IG5vbmU7IH0KCi8qIC0tLS0tLS0tLS0gdGhlIG9uZSBwb3AtdXAgKHJ1bGUgNCk6IGJvdHRvbSBzaGVldCB3aGVuIG5hcnJvdywgY2VudHJlZCBjYXJkIHdoZW4gd2lkZS4gQmVoYXZpb3VyOiBzdHlsZS9zaGVldC5qcyAtLS0tLS0tLS0tICovCi53ei1zaGVldCB7IGNvbG9yOiB2YXIoLS1pbmspOyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1wYW5lbCk7IGJvcmRlcjogMXB4IHNvbGlkIHZhcigtLWxpbmUtMik7IGJvcmRlci1yYWRpdXM6IHZhcigtLXIpOyBwYWRkaW5nOiB2YXIoLS13ei1zcGFjZS00KTsgd2lkdGg6IG1pbig1MjBweCwgY2FsYygxMDAlIC0gMjRweCkpOyBtYXgtd2lkdGg6IG5vbmU7IG1heC1oZWlnaHQ6IDg1JTsgb3ZlcmZsb3c6IGF1dG87IGJhY2tkcm9wLWZpbHRlcjogYmx1cigyMHB4KTsgLXdlYmtpdC1iYWNrZHJvcC1maWx0ZXI6IGJsdXIoMjBweCk7IH0KLnd6LXNoZWV0OjpiYWNrZHJvcCB7IGJhY2tncm91bmQ6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei1iYXNlKSA3NCUsIHRyYW5zcGFyZW50KTsgfQoud3otc2hlZXQteCB7IHBvc2l0aW9uOiBzdGlja3k7IHRvcDogMDsgZmxvYXQ6IHJpZ2h0OyB3aWR0aDogMzZweDsgaGVpZ2h0OiAzNnB4OyBtYXJnaW46IC02cHggLTZweCAwIDhweDsgYm9yZGVyLXJhZGl1czogNTAlOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyBjb2xvcjogdmFyKC0taW5rLTIpOyBiYWNrZ3JvdW5kOiB2YXIoLS1maWxsKTsgZm9udC1zaXplOiAxOHB4OyBsaW5lLWhlaWdodDogMTsgei1pbmRleDogMTsgfQoud3otc2hlZXQteDo6YWZ0ZXIgeyBjb250ZW50OiAiIjsgcG9zaXRpb246IGFic29sdXRlOyBpbnNldDogLTRweDsgYm9yZGVyLXJhZGl1czogNTAlOyB9IC8qIDQ0cHggdG91Y2ggdGFyZ2V0IChBUFAtREVTSUdOLVJVTEVTIHJ1bGUgNikgYXJvdW5kIHRoZSAzNnB4IHZpc3VhbCAqLwoud3otc2hlZXQtZ3JpcCB7IGRpc3BsYXk6IG5vbmU7IH0KLnd6LXNoZWV0ID4gaDIgeyBtYXJnaW46IDAgMCB2YXIoLS13ei1zcGFjZS0zKTsgZm9udC1zaXplOiAxN3B4OyBmb250LXdlaWdodDogNzAwOyB9CkBtZWRpYSAobWF4LXdpZHRoOiA1OTlweCkgewogIC53ei1zaGVldCB7IHdpZHRoOiAxMDAlOyBtYXgtd2lkdGg6IDEwMCU7IG1hcmdpbjogYXV0byAwIDA7IGJvcmRlci1yYWRpdXM6IDIycHggMjJweCAwIDA7IGJvcmRlci1ib3R0b206IDA7IG1heC1oZWlnaHQ6IDg4JTsgcGFkZGluZzogNnB4IHZhcigtLXd6LXNwYWNlLTQpIGNhbGModmFyKC0td3otc3BhY2UtNCkgKyBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSwgMHB4KSk7IH0KICAud3otc2hlZXRbb3Blbl0geyBhbmltYXRpb246IHd6LXNoZWV0LXVwIDIyMG1zIHZhcigtLWVhc2UsIGVhc2Utb3V0KTsgfQogIC53ei1zaGVldC1ncmlwIHsgZGlzcGxheTogYmxvY2s7IHdpZHRoOiA0MHB4OyBoZWlnaHQ6IDVweDsgbWFyZ2luOiA0cHggYXV0byAxMHB4OyBib3JkZXItcmFkaXVzOiAzcHg7IGJhY2tncm91bmQ6IHZhcigtLWxpbmUtMik7IHRvdWNoLWFjdGlvbjogbm9uZTsgfQp9CkBrZXlmcmFtZXMgd3otc2hlZXQtdXAgeyBmcm9tIHsgdHJhbnNmb3JtOiB0cmFuc2xhdGVZKDQwcHgpOyBvcGFjaXR5OiAuNjsgfSB9CgovKiBTaGFyZWQgY29udHJvbCB2YXJpYW50czsgZG8gbm90IHJlZHJhdyBpbiBhcHBzLiAqLwoud3otYnV0dG9uLmRlc3RydWN0aXZlIHsgYmFja2dyb3VuZDogY29sb3ItbWl4KGluIHNyZ2IsIHZhcigtLXd6LWRhbmdlcikgMTYlLCB2YXIoLS13ei1iYXNlKSk7IGNvbG9yOiB2YXIoLS13ei1kYW5nZXIpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS13ei1kYW5nZXIpOyB9Ci53ei1pY29uLWJ1dHRvbiB7IGRpc3BsYXk6IGlubGluZS1ncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyBtaW4td2lkdGg6IHZhcigtLXd6LXRhcmdldCk7IG1pbi1oZWlnaHQ6IHZhcigtLXd6LXRhcmdldCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXd6LXJhZGl1cy10aWxlKTsgYmFja2dyb3VuZDogdmFyKC0td3otc3VyZmFjZSk7IGNvbG9yOiB2YXIoLS13ei10ZXh0KTsgfQoud3otaWNvbi1idXR0b24gc3ZnIHsgd2lkdGg6IHZhcigtLXd6LWljb24tc2l6ZSk7IGhlaWdodDogdmFyKC0td3otaWNvbi1zaXplKTsgfQoud3otcGlsbC5lcnJvciB7IGNvbG9yOiB2YXIoLS13ei1kYW5nZXIpOyB9CgovKiBDYXB0aW9uZWQgbWVkaWEgYWN0aW9ucyByZXVzZSB0aGUgc2FtZSB0YXJnZXQ7IG5vIG9wYXF1ZSB0aWxlIG92ZXIgaW1hZ2VyeS4gKi8KLnd6LWljb24tYnV0dG9uLmdob3N0IHsgYmFja2dyb3VuZDogbm9uZTsgfQoud3otaWNvbi1idXR0b24uY2FwdGlvbiB7IGRpc3BsYXk6IGlubGluZS1mbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgZ2FwOiB2YXIoLS13ei1zcGFjZS0xKTsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgfQoud3otaWNvbi1idXR0b24uY2FwdGlvbiA6aXMoc3Bhbiwgc21hbGwpIHsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQoKLyogTmF0aXZlIGhpZGRlbiBzdGF0ZSBtdXN0IGJlYXQgYXV0aG9yZWQgZGlzcGxheSBydWxlcyBvbiBzaGFyZWQgY29udHJvbHMuICovCi53ei1idXR0b25baGlkZGVuXSwgLnd6LXJvd1toaWRkZW5dLCAud3otaWNvbi1idXR0b25baGlkZGVuXSwgLnd6LWlucHV0W2hpZGRlbl0geyBkaXNwbGF5OiBub25lICFpbXBvcnRhbnQ7IH0K', 'standalone.css': 'LyogQ2Fub25pY2FsIHN0YW5kYWxvbmUgdG9rZW5zIHByZXNlcnZlIHByZWZlcnMtY29sb3Itc2NoZW1lOiBsaWdodC4gKi8KQGltcG9ydCB1cmwoInN0YW5kYWxvbmUtdG9rZW5zLmNzcyIpOwpodG1sOm5vdCgud3otaG9zdGVkKSBib2R5IHsgYmFja2dyb3VuZDogdmFyKC0td3otYmFzZSk7IH0K', 'standalone-tokens.css': 'LyogUHJlc2VydmUgdGhlIHNoYXJlZC1mdW5jdGlvbiB0ZW1wbGF0ZSdzIHByZS1ob3N0IGxpZ2h0IGZhbGxiYWNrIHdpdGhvdXQKICogY2hhbmdpbmcgdGhlIGNhbm9uaWNhbCBzdHlsZSBzbmFwc2hvdCBvciBpbXBsZW1lbnRpbmcgdGhlbWUgc3RhdGUuICovCmh0bWw6bm90KC53ei1ob3N0ZWQpIGJvZHkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1jYW52YXMpOyB9CkBtZWRpYSAocHJlZmVycy1jb2xvci1zY2hlbWU6IGxpZ2h0KSB7CiAgaHRtbDpub3QoW2RhdGEtY29sb3Itc2NoZW1lXSkgewogICAgLS13ei10ZXh0OiAjMWExOTMzOyAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjNDg0NTYwOwogICAgLS13ei10ZXh0LW11dGVkOiAjNjY2MTdhOyAtLXd6LWJvcmRlcjogI2Q0Y2ZkZTsKICAgIC0td3otYWNjZW50OiAjNjg0NGFhOyAtLXd6LW9uLWFjY2VudDogI2ZmZmZmZjsKICAgIC0td3otYmFzZTogI2ZhZjhmZDsgLS13ei1kYW5nZXI6ICNiNDIzMTg7IGNvbG9yLXNjaGVtZTogbGlnaHQ7CiAgfQp9Cg==', 'wizard-sheet.js': 'LyogV2l6YXJkIFVJIGtpdCDCtyB0aGUgb25lIHBvcC11cCAoQVBQLURFU0lHTi1SVUxFUy5tZCBydWxlIDQpLgogKiBVc2U6IDxkaWFsb2cgY2xhc3M9Ind6LXNoZWV0Ij48aDI+VGl0bGU8L2gyPuKApjwvZGlhbG9nPiwgdGhlbiBXelNoZWV0Lm9wZW4oZGlhbG9nKS4KICogQWRkcyB0aGUgZ3JpcCBhbmQgw5cgaWYgbWlzc2luZywgYW5kIGNsb3NlcyBmaXZlIHdheXM6IMOXLCBFc2MgKG5hdGl2ZSksIHRhcCBvdXRzaWRlLCBCYWNrCiAqIGFuZCBhIGRvd253YXJkIHN3aXBlIG9uIHRoZSBncmlwLiBCYWNrOiBpbiBhbiBhcHAgdGhhdCB1c2VzIFdpemFyZEFwcCwgZWFjaCBvcGVuIHNoZWV0IGlzIHB1c2hlZCBvbiBXaXphcmRBcHAuYmFjawogKiAodGhlIHNoZWxsIGFza3MgdGhlIGFwcCBjbGllbnQgZmlyc3QpOyB3aW5kb3cuX193aXphcmRDbG9zZU92ZXJsYXkgc3RheXMgYXMgdGhlIGZhbGxiYWNrIGZvciBhcHBzIHdpdGhvdXQgaXQuIE5vIGRlcGVuZGVuY2llcy4gKi8KKCgpID0+IHsKICAndXNlIHN0cmljdCc7CiAgY29uc3Qgb3BlbiA9IFtdOwogIGZ1bmN0aW9uIHdpcmUoZCkgewogICAgaWYgKGQuX193eikgcmV0dXJuIGQ7IGQuX193eiA9IHRydWU7CiAgICBpZiAoIWQucXVlcnlTZWxlY3RvcignOnNjb3BlID4gLnd6LXNoZWV0LWdyaXAnKSkgZC5pbnNlcnRBZGphY2VudEhUTUwoJ2FmdGVyYmVnaW4nLCAnPGRpdiBjbGFzcz0id3otc2hlZXQtZ3JpcCIgYXJpYS1oaWRkZW49InRydWUiPjwvZGl2PicpOwogICAgaWYgKCFkLnF1ZXJ5U2VsZWN0b3IoJzpzY29wZSA+IC53ei1zaGVldC14JykpIGQucXVlcnlTZWxlY3RvcignOnNjb3BlID4gLnd6LXNoZWV0LWdyaXAnKS5pbnNlcnRBZGphY2VudEhUTUwoJ2FmdGVyZW5kJywgJzxidXR0b24gdHlwZT0iYnV0dG9uIiBjbGFzcz0id3otc2hlZXQteCIgYXJpYS1sYWJlbD0iQ2xvc2UiPsOXPC9idXR0b24+Jyk7CiAgICBkLmFkZEV2ZW50TGlzdGVuZXIoJ2NsaWNrJywgZSA9PiB7CiAgICAgIGlmIChlLnRhcmdldC5jbG9zZXN0KCcud3otc2hlZXQteCcpKSByZXR1cm4gY2xvc2UoZCk7CiAgICAgIGlmIChlLnRhcmdldCA9PT0gZCkgeyBjb25zdCByID0gZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsgaWYgKGUuY2xpZW50WCA8IHIubGVmdCB8fCBlLmNsaWVudFggPiByLnJpZ2h0IHx8IGUuY2xpZW50WSA8IHIudG9wIHx8IGUuY2xpZW50WSA+IHIuYm90dG9tKSBjbG9zZShkKTsgfQogICAgfSk7CiAgICBkLmFkZEV2ZW50TGlzdGVuZXIoJ2Nsb3NlJywgKCkgPT4geyBjb25zdCBpID0gb3Blbi5pbmRleE9mKGQpOyBpZiAoaSA+PSAwKSBvcGVuLnNwbGljZShpLCAxKTsgaWYgKGQuX193ekJhY2spIHsgZC5fX3d6QmFjay5yZWxlYXNlKCk7IGQuX193ekJhY2sgPSBudWxsOyB9IH0pOwogICAgbGV0IHkwID0gbnVsbDsgY29uc3QgZyA9IGQucXVlcnlTZWxlY3RvcignLnd6LXNoZWV0LWdyaXAnKTsKICAgIGcuYWRkRXZlbnRMaXN0ZW5lcigncG9pbnRlcmRvd24nLCBlID0+IHsgeTAgPSBlLmNsaWVudFk7IGcuc2V0UG9pbnRlckNhcHR1cmUoZS5wb2ludGVySWQpOyB9KTsKICAgIGcuYWRkRXZlbnRMaXN0ZW5lcigncG9pbnRlcm1vdmUnLCBlID0+IHsgaWYgKHkwICE9IG51bGwpIGQuc3R5bGUudHJhbnNmb3JtID0gYHRyYW5zbGF0ZVkoJHtNYXRoLm1heCgwLCBlLmNsaWVudFkgLSB5MCl9cHgpYDsgfSk7CiAgICBjb25zdCBlbmQgPSBlID0+IHsgaWYgKHkwID09IG51bGwpIHJldHVybjsgY29uc3QgZHkgPSBlLmNsaWVudFkgLSB5MDsgeTAgPSBudWxsOyBkLnN0eWxlLnRyYW5zZm9ybSA9ICcnOyBpZiAoZHkgPiA3MCkgY2xvc2UoZCk7IH07CiAgICBnLmFkZEV2ZW50TGlzdGVuZXIoJ3BvaW50ZXJ1cCcsIGVuZCk7IGcuYWRkRXZlbnRMaXN0ZW5lcigncG9pbnRlcmNhbmNlbCcsIGVuZCk7CiAgICByZXR1cm4gZDsKICB9CiAgZnVuY3Rpb24gc2hvdyhkKSB7CiAgICB3aXJlKGQpOyBpZiAoIWQub3BlbikgZC5zaG93TW9kYWwoKTsKICAgIGlmICghb3Blbi5pbmNsdWRlcyhkKSkgb3Blbi5wdXNoKGQpOwogICAgY29uc3QgQiA9IHdpbmRvdy5XaXphcmRBcHAgJiYgd2luZG93LldpemFyZEFwcC5iYWNrOwogICAgaWYgKCFkLl9fd3pCYWNrICYmIEIgJiYgdHlwZW9mIEIucHVzaCA9PT0gJ2Z1bmN0aW9uJykgeyB0cnkgeyBkLl9fd3pCYWNrID0gQi5wdXNoKCgpID0+IHsgY2xvc2UoZCk7IHJldHVybiB0cnVlOyB9KTsgfSBjYXRjaCAoXykge30gfQogICAgcmV0dXJuIGQ7CiAgfQogIGZ1bmN0aW9uIGNsb3NlKGQpIHsgaWYgKGQgJiYgZC5vcGVuKSBkLmNsb3NlKCk7IH0KICBjb25zdCBwcmV2ID0gd2luZG93Ll9fd2l6YXJkQ2xvc2VPdmVybGF5OwogIHdpbmRvdy5fX3dpemFyZENsb3NlT3ZlcmxheSA9ICgpID0+IHsgY29uc3QgZCA9IG9wZW4ucG9wKCk7IGlmIChkKSB7IGNsb3NlKGQpOyByZXR1cm4gdHJ1ZTsgfSByZXR1cm4gdHlwZW9mIHByZXYgPT09ICdmdW5jdGlvbicgPyAhIXByZXYoKSA6IGZhbHNlOyB9OwogIHdpbmRvdy5XelNoZWV0ID0geyBvcGVuOiBzaG93LCBjbG9zZSwgd2lyZSB9Owp9KSgpOwo=', 'photos.js': 'LyogUGhvdG9zIGJhY2t1cCBwYWdlIGxvZ2ljLiBQdXJlIChubyBET00sIG5vIGdsb2JhbHMpOiBhcHAuanMgd2lyZXMgaXQgdG8gdGhlIHBhZ2U7IHRlc3RzIGRyaXZlIGl0IGRpcmVjdGx5LgogKiBUaGUgYmFja3VwIHdvcmtlciBsaXZlcyBpbiB0aGUgV2l6YXJkIE9TIEFuZHJvaWQgYXBwLiBUaGlzIGFwcCByZWFjaGVzIGl0IHRocm91Z2ggdGhlIHNoZWxsJ3MgUGhvdG9zIHJlbGF5OgogKiAgIGFwcCDihpIgc2hlbGwge3R5cGU6J3dpemFyZC1vczpwaG90b3MnLCBpZCwgbWV0aG9kLCBwYXJhbXN9OyBzaGVsbCDihpIgYXBwIHt0eXBlOid3aXphcmQtb3M6cGhvdG9zJywgaWQsIG9rLCByZXN1bHR8ZXJyb3J9OwogKiAgIHNoZWxsIHB1c2gge3R5cGU6J3dpemFyZC1vczpwaG90b3MtcHVzaCcsIHB1c2g6e3R5cGU6J3N0YXR1cycsIHN0YXR1c30gfCB7dHlwZToncmVzdW1lJ319LgogKiBLZXlzIGFuZCBzaWduLWluIHN0YXRlIG5ldmVyIHJlYWNoIHRoaXMgcGFnZTsgc3RhdHVzIGlzIGEgZml4ZWQgc2V0IG9mIGZpZWxkcyAoUGhvdG9zQnJpZGdlUG9saWN5LnN0YXR1cykuICovCihmdW5jdGlvbiAocm9vdCkgewogICd1c2Ugc3RyaWN0JzsKICBjb25zdCBWRVJTSU9OID0gMSwgVCA9ICd3aXphcmQtb3M6cGhvdG9zJywgUFVTSCA9ICd3aXphcmQtb3M6cGhvdG9zLXB1c2gnLCBERVRFQ1RfTVMgPSAxNTAwLCBDQUxMX01TID0gMTUwMDA7CiAgY29uc3QgZXNjID0gcyA9PiBTdHJpbmcocyA/PyAnJykucmVwbGFjZSgvWyY8PiInXS9nLCBjID0+ICh7ICcmJzogJyZhbXA7JywgJzwnOiAnJmx0OycsICc+JzogJyZndDsnLCAnIic6ICcmcXVvdDsnLCAiJyI6ICcmIzM5OycgfVtjXSkpOwoKICAvKiogQ2FsbHMgb3ZlciBwb3N0TWVzc2FnZSB0byB0aGUgaG9zdGluZyBzaGVsbC4gYHBvc3QobXNnKWAgc2VuZHM7IGZlZWQgc2hlbGwgbWVzc2FnZXMgdG8gYG9uTWVzc2FnZShkYXRhKWAuICovCiAgZnVuY3Rpb24gY2xpZW50KHBvc3QsIHsgdGltZW91dCA9IENBTExfTVMsIG9uU3RhdHVzID0gKCkgPT4ge30sIG9uUmVzdW1lID0gKCkgPT4ge30sIHNldCA9IHNldFRpbWVvdXQsIGNsZWFyID0gY2xlYXJUaW1lb3V0IH0gPSB7fSkgewogICAgY29uc3Qgd2FpdGluZyA9IG5ldyBNYXAoKTsgbGV0IHNlcSA9IDA7CiAgICBmdW5jdGlvbiBvbk1lc3NhZ2UoZCkgewogICAgICBpZiAoIWQgfHwgdHlwZW9mIGQgIT09ICdvYmplY3QnKSByZXR1cm47CiAgICAgIGlmIChkLnR5cGUgPT09IFBVU0ggJiYgZC5wdXNoKSB7CiAgICAgICAgaWYgKGQucHVzaC50eXBlID09PSAnc3RhdHVzJyAmJiBkLnB1c2guc3RhdHVzICYmIGQucHVzaC5zdGF0dXMudiA9PT0gVkVSU0lPTikgb25TdGF0dXMoZC5wdXNoLnN0YXR1cyk7CiAgICAgICAgZWxzZSBpZiAoZC5wdXNoLnR5cGUgPT09ICdyZXN1bWUnKSBvblJlc3VtZSgpOwogICAgICAgIHJldHVybjsKICAgICAgfQogICAgICBpZiAoZC50eXBlICE9PSBUKSByZXR1cm47CiAgICAgIGNvbnN0IHcgPSB3YWl0aW5nLmdldChkLmlkKTsgaWYgKCF3KSByZXR1cm47CiAgICAgIHdhaXRpbmcuZGVsZXRlKGQuaWQpOyBjbGVhcih3LnRpbWVyKTsKICAgICAgaWYgKGQub2spIHsgaWYgKGQucmVzdWx0ICYmIHR5cGVvZiBkLnJlc3VsdCA9PT0gJ29iamVjdCcgJiYgZC5yZXN1bHQudiA9PT0gVkVSU0lPTikgb25TdGF0dXMoZC5yZXN1bHQpOyB3LnJlc29sdmUoZC5yZXN1bHQpOyB9CiAgICAgIGVsc2Ugdy5yZWplY3QoT2JqZWN0LmFzc2lnbihuZXcgRXJyb3IoU3RyaW5nKGQuZXJyb3IgfHwgJ1NvbWV0aGluZyB3ZW50IHdyb25nJykpLCB7IGNvZGU6IFN0cmluZyhkLmVycm9yIHx8ICcnKSB9KSk7CiAgICB9CiAgICBmdW5jdGlvbiBjYWxsKG1ldGhvZCwgcGFyYW1zID0ge30pIHsKICAgICAgcmV0dXJuIG5ldyBQcm9taXNlKChyZXNvbHZlLCByZWplY3QpID0+IHsKICAgICAgICBjb25zdCBpZCA9ICdwaCcgKyAoKytzZXEpOwogICAgICAgIGNvbnN0IHRpbWVyID0gc2V0KCgpID0+IHsgd2FpdGluZy5kZWxldGUoaWQpOyByZWplY3QoT2JqZWN0LmFzc2lnbihuZXcgRXJyb3IoJ05vIGFuc3dlciBmcm9tIHRoZSBXaXphcmQgT1MgYXBwJyksIHsgdGltZW91dDogdHJ1ZSB9KSk7IH0sIHRpbWVvdXQpOwogICAgICAgIHdhaXRpbmcuc2V0KGlkLCB7IHJlc29sdmUsIHJlamVjdCwgdGltZXIgfSk7CiAgICAgICAgdHJ5IHsgcG9zdCh7IHR5cGU6IFQsIGlkLCBtZXRob2QsIHBhcmFtcyB9KTsgfQogICAgICAgIGNhdGNoIChlKSB7IHdhaXRpbmcuZGVsZXRlKGlkKTsgY2xlYXIodGltZXIpOyByZWplY3QoZSk7IH0KICAgICAgfSk7CiAgICB9CiAgICByZXR1cm4gewogICAgICBjYWxsLCBvbk1lc3NhZ2UsCiAgICAgIHN0YXR1czogKCkgPT4gY2FsbCgnc3RhdHVzJyksCiAgICAgIHNldEVuYWJsZWQ6IChvbiwgc2VydmVyKSA9PiBjYWxsKCdzZXRFbmFibGVkJywgc2VydmVyID8geyBvbjogISFvbiwgc2VydmVyIH0gOiB7IG9uOiAhIW9uIH0pLAogICAgICBzZXRPcHRpb246IChrZXksIG9uKSA9PiBjYWxsKCdzZXRPcHRpb24nLCB7IGtleSwgb246ICEhb24gfSksCiAgICAgIGNvbm5lY3Q6IHNlcnZlciA9PiBjYWxsKCdjb25uZWN0Jywgc2VydmVyID8geyBzZXJ2ZXIgfSA6IHt9KSwKICAgICAgcnVuTm93OiAoKSA9PiBjYWxsKCdydW5Ob3cnKSwKICAgICAgcGVybWlzc2lvbnM6ICgpID0+IGNhbGwoJ3Blcm1pc3Npb25zJyksCiAgICAgIGFsYnVtczogKCkgPT4gY2FsbCgnYWxidW1zJyksCiAgICAgIHNldEFsYnVtczogaWRzID0+IGNhbGwoJ3NldEFsYnVtcycsIHsgaWRzOiBbLi4uaWRzXSB9KSwKICAgICAgZGlzY29ubmVjdDogKCkgPT4gY2FsbCgnZGlzY29ubmVjdCcpLAogICAgICBvcGVuTmF0aXZlOiAoKSA9PiBjYWxsKCdvcGVuJyksCiAgICB9OwogIH0KCiAgLyoqIEZpcnN0IGNvbnRhY3Q6IHYxIHN0YXR1cyDihpIgJ3YxJzsgJ25vLWJyaWRnZScgKGJyb3dzZXIsIGRlc2t0b3ApIOKGkiAnbm9uZSc7IHNpbGVuY2UgKG9sZCBhcHApIOKGkiAnbGVnYWN5Jy4gKi8KICBmdW5jdGlvbiBkZXRlY3QoYXBpLCB7IHdhaXQgPSBERVRFQ1RfTVMsIHNldCA9IHNldFRpbWVvdXQgfSA9IHt9KSB7CiAgICByZXR1cm4gUHJvbWlzZS5yYWNlKFthcGkuc3RhdHVzKCkudGhlbigoKSA9PiAndjEnLCBlID0+IGUuY29kZSA9PT0gJ25vLWJyaWRnZScgPyAnbm9uZScgOiBlLnRpbWVvdXQgPyAnbGVnYWN5JyA6ICd2MS1lcnJvcicpLAogICAgICBuZXcgUHJvbWlzZShyID0+IHNldCgoKSA9PiByKCdsZWdhY3knKSwgd2FpdCkpXSk7CiAgfQoKICBmdW5jdGlvbiBhZ28obXMsIG5vdyA9IERhdGUubm93KCkpIHsKICAgIGlmICghbXMpIHJldHVybiAnbm90IGJhY2tlZCB1cCB5ZXQnOwogICAgY29uc3QgbWluID0gTWF0aC5tYXgoMCwgTWF0aC5mbG9vcigobm93IC0gbXMpIC8gNjAwMDApKTsKICAgIGlmIChtaW4gPCAxKSByZXR1cm4gJ2xhc3QgYmFja3VwIGp1c3Qgbm93JzsKICAgIGlmIChtaW4gPCA2MCkgcmV0dXJuIGBsYXN0IGJhY2t1cCAke21pbn0gbWluIGFnb2A7CiAgICBjb25zdCBoID0gTWF0aC5mbG9vcihtaW4gLyA2MCk7IGlmIChoIDwgMjQpIHJldHVybiBgbGFzdCBiYWNrdXAgJHtofSBoIGFnb2A7CiAgICBjb25zdCBkID0gTWF0aC5mbG9vcihoIC8gMjQpOyByZXR1cm4gYGxhc3QgYmFja3VwICR7ZH0gJHtkID09PSAxID8gJ2RheScgOiAnZGF5cyd9IGFnb2A7CiAgfQogIC8qKiBPbmUgbGluZSBmb3IgdGhlIFNldHRpbmdzIHJvdyBhbmQgdGhlIHBhZ2U6IHdoYXQgaXMgdHJ1ZSByaWdodCBub3cuICovCiAgZnVuY3Rpb24gc3VtbWFyeShzLCBub3cgPSBEYXRlLm5vdygpKSB7CiAgICBpZiAoIXMpIHJldHVybiAnQ2hlY2tpbmfigKYnOwogICAgaWYgKHMuc2lnbmluZ0luKSByZXR1cm4gJ0NvbmZpcm0gd2l0aCB5b3VyIGZpbmdlcnByaW50IGluIFdpemFyZCBTaWduLWluJzsKICAgIGlmICghcy5lbmFibGVkKSByZXR1cm4gcy5jb25uZWN0ZWQgPyAnT2ZmJyA6ICdPZmYgwrcgdHVybmluZyBpdCBvbiBzaWducyB5b3UgaW4gd2l0aCB5b3VyIGZpbmdlcnByaW50JzsKICAgIGlmIChzLm5lZWRzQWNjZXNzKSByZXR1cm4gJ09uIMK3IHdhaXRpbmcgZm9yIHBob3RvIGFjY2Vzcyc7CiAgICBpZiAoIXMudG90YWwgJiYgIXMubGFzdFN5bmMpIHJldHVybiAnT24gwrcgZmlyc3QgYmFja3VwIHdhaXRpbmcnOwogICAgcmV0dXJuIGBPbiDCtyAke3MudG90YWwgPyBgJHtzLmRvbmV9IG9mICR7cy50b3RhbH0gYmFja2VkIHVwYCA6ICdub3RoaW5nIGJhY2tlZCB1cCB5ZXQnfSDCtyAke2FnbyhzLmxhc3RTeW5jLCBub3cpfWA7CiAgfQogIC8qKiBXaHkgYSBxdWV1ZWQgYmFja3VwIGhhcyBub3Qgc3RhcnRlZDogQW5kcm9pZCB3YWl0cyBmb3IgdGhlc2UgYmVmb3JlIGl0IHJ1bnMgdGhlIGpvYi4gKi8KICBmdW5jdGlvbiB3YWl0aW5nRm9yKHMpIHsKICAgIGNvbnN0IHcgPSBbXTsKICAgIGlmIChzLndpZmkpIHcucHVzaCgnV2ktRmknKTsKICAgIGlmIChzLmNoYXJnZSAmJiAhcy5sYXN0U3luYykgdy5wdXNoKCdhIGNoYXJnZXIgKGZpcnN0IGJhY2t1cCBvbmx5IHJ1bnMgd2hpbGUgcGx1Z2dlZCBpbiknKTsKICAgIHJldHVybiB3Lmxlbmd0aCA/IGBCYWNrdXAgcXVldWVkIMK3IEFuZHJvaWQgc3RhcnRzIGl0IG9uY2UgaXQgc2VlcyAke3cuam9pbignIGFuZCAnKX1gIDogJ0JhY2t1cCBxdWV1ZWQgwrcgc3RhcnRpbmfigKYnOwogIH0KICAvKiogVGhlIHNlcnZlciBmaWVsZCBzaG93cyBvbmx5IHdoZW4gbm90aGluZyBpcyBrbm93bjogbm8gYnVpbGQtdGltZSBzZXJ2ZXIgYW5kIG5vdCBzaWduZWQgaW4uICovCiAgY29uc3QgbmVlZHNTZXJ2ZXIgPSBzID0+ICEhcyAmJiAhcy5jb25uZWN0ZWQgJiYgIXMuc2VydmVyOwoKICBjb25zdCBzdyA9IChhdHRyLCBvbiwgdGl0bGUsIHN1YiwgZGlzYWJsZWQpID0+IGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLXJvdyIgJHthdHRyfSByb2xlPSJzd2l0Y2giIGFyaWEtY2hlY2tlZD0iJHshIW9ufSIke2Rpc2FibGVkID8gJyBkaXNhYmxlZCcgOiAnJ30+PHNwYW4gY2xhc3M9InBoLXR4Ij48Yj4ke2VzYyh0aXRsZSl9PC9iPiR7c3ViID8gYDxzbWFsbD4ke2VzYyhzdWIpfTwvc21hbGw+YCA6ICcnfTwvc3Bhbj48c3BhbiBjbGFzcz0ic3cgJHtvbiA/ICdvbicgOiAnJ30iPjxpPjwvaT48L3NwYW4+PC9idXR0b24+YDsKCiAgLyoqIFRoZSB3aG9sZSBwYWdlIGJvZHkgZm9yIG9uZSBzdGF0ZS4gUHVyZTogdGVzdHMgcmVuZGVyIGl0IGRpcmVjdGx5LiAqLwogIGZ1bmN0aW9uIGh0bWwoc3QsIG5vdyA9IERhdGUubm93KCkpIHsKICAgIGlmIChzdC5tb2RlID09PSAnbGVnYWN5JykgcmV0dXJuIGA8ZGl2IGNsYXNzPSJwaCI+PHAgY2xhc3M9InBoLWludHJvIj5UaGlzIFdpemFyZCBPUyBhcHAgcHJlZGF0ZXMgdGhlIFBob3RvcyBhcHAuIEl0cyBiYWNrdXAgc2NyZWVuIHN0aWxsIHdvcmtzLjwvcD48YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLXJvdyIgZGF0YS1waD0ibmF0aXZlIj48c3BhbiBjbGFzcz0icGgtdHgiPjxiPlBob3RvcyBiYWNrdXA8L2I+PHNtYWxsPk9yaWdpbmFscyBiYWNrZWQgdXAgdG8geW91ciBwaG90byBzZXJ2ZXI8L3NtYWxsPjwvc3Bhbj48c3BhbiBjbGFzcz0icGgtZ28iPuKAujwvc3Bhbj48L2J1dHRvbj48L2Rpdj5gOwogICAgaWYgKHN0Lm1vZGUgPT09ICdub25lJykgcmV0dXJuIGA8ZGl2IGNsYXNzPSJwaCI+PHAgY2xhc3M9InBoLXN0YXR1cyIgZGF0YS1waC1zdGF0dXM+UGhvdG8gYmFja3VwIHJ1bnMgb24geW91ciBwaG9uZS48L3A+PHAgY2xhc3M9InBoLWludHJvIj5PcGVuIFBob3RvcyBpbiB0aGUgV2l6YXJkIE9TIEFuZHJvaWQgYXBwIHRvIGJhY2sgdXAgeW91ciBjYW1lcmEgcm9sbCB0byB5b3VyIHBob3RvIHNlcnZlci48L3A+PC9kaXY+YDsKICAgIGNvbnN0IHMgPSBzdC5zdGF0dXM7CiAgICBpZiAoIXMpIHJldHVybiBgPGRpdiBjbGFzcz0icGgiPjxwIGNsYXNzPSJwaC1zdGF0dXMiIGRhdGEtcGgtc3RhdHVzPiR7ZXNjKHN0LmVycm9yIHx8ICdDaGVja2luZ+KApicpfTwvcD48L2Rpdj5gOwogICAgY29uc3QgYnVzeSA9ICEhc3QuYnVzeSB8fCAhIXMuc2lnbmluZ0luOwogICAgY29uc3QgcGFydHMgPSBbYDxkaXYgY2xhc3M9InBoIiBhcmlhLWJ1c3k9IiR7YnVzeX0iPmAsCiAgICAgIGA8cCBjbGFzcz0icGgtaW50cm8iPk9yaWdpbmFsIHBob3RvcyBhbmQgdmlkZW9zIGdvIHN0cmFpZ2h0IHRvIHlvdXIgcGhvdG8gc2VydmVyLiBOb3RoaW5nIGlzIGRlbGV0ZWQgZnJvbSB0aGlzIHBob25lLjwvcD5gLAogICAgICBzdygnZGF0YS1waD0iZW5hYmxlZCInLCBzLmVuYWJsZWQsICdCYWNrIHVwIHBob3RvcycsIG51bGwsIGJ1c3kpXTsKICAgIGlmIChuZWVkc1NlcnZlcihzKSkgcGFydHMucHVzaChgPGxhYmVsIGNsYXNzPSJwaC1maWVsZCI+PHNwYW4+UGhvdG8gc2VydmVyPC9zcGFuPjxpbnB1dCB0eXBlPSJ1cmwiIGlucHV0bW9kZT0idXJsIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im9mZiIgc3BlbGxjaGVjaz0iZmFsc2UiIHBsYWNlaG9sZGVyPSJodHRwczovL3Bob3Rvcy5leGFtcGxlLmNvbSIgZGF0YS1waC1zZXJ2ZXIgdmFsdWU9IiR7ZXNjKHN0LnNlcnZlciB8fCAnJyl9Ij48L2xhYmVsPmApOwogICAgcGFydHMucHVzaChgPHAgY2xhc3M9InBoLXN0YXR1cyIgZGF0YS1waC1zdGF0dXMgcm9sZT0ic3RhdHVzIj4ke2VzYyhzdC5lcnJvciB8fCBzdW1tYXJ5KHMsIG5vdykpfTwvcD5gKTsKICAgIGNvbnN0IGZyZXNoID0gc3QucXVldWVkQXQgJiYgIXN0LmVycm9yICYmICghcy5sYXN0U3luYyB8fCBzLmxhc3RTeW5jIDwgc3QucXVldWVkQXQpOwogICAgaWYgKGZyZXNoKSBwYXJ0cy5wdXNoKGA8cCBjbGFzcz0icGgtbm90ZSBwaC1xdWV1ZWQiIHJvbGU9InN0YXR1cyI+JHtlc2Mod2FpdGluZ0ZvcihzKSl9JHtzLndpZmkgPyAnIMK3IHN0dWNrIHdoaWxlIHBsdWdnZWQgaW4gb24gV2ktRmk/IFR1cm4gb2ZmIOKAnFdpLUZpIG9ubHnigJ0gKHRoZSBwcml2YXRlIG5ldHdvcmsgY2FuIGxvb2sgbGlrZSBtb2JpbGUgZGF0YSB0byBBbmRyb2lkKScgOiAnJ308L3A+YCk7CiAgICBpZiAocy5lbmFibGVkICYmIHMubWVzc2FnZSAmJiAhc3QuZXJyb3IpIHBhcnRzLnB1c2goYDxwIGNsYXNzPSJwaC1ub3RlIj5QaG9uZSBzYXlzOiAke2VzYyhzLm1lc3NhZ2UpfTwvcD5gKTsKICAgIGlmIChzLm5lZWRzQWNjZXNzKSBwYXJ0cy5wdXNoKGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biBwcmltYXJ5IiBkYXRhLXBoPSJwZXJtaXNzaW9ucyI+QWxsb3cgcGhvdG8gYWNjZXNzPC9idXR0b24+YCk7CiAgICBpZiAocy5saW1pdGVkKSBwYXJ0cy5wdXNoKGA8cCBjbGFzcz0icGgtbm90ZSI+T25seSB0aGUgcGhvdG9zIHlvdSBwaWNrZWQgaW4gQW5kcm9pZCBhcmUgdmlzaWJsZS4gPGJ1dHRvbiB0eXBlPSJidXR0b24iIGNsYXNzPSJwaC1saW5rIiBkYXRhLXBoPSJwZXJtaXNzaW9ucyI+QWxsb3cgYWxsIHBob3RvczwvYnV0dG9uPjwvcD5gKTsKICAgIHBhcnRzLnB1c2goc3coJ2RhdGEtcGg9IndpZmkiJywgcy53aWZpLCAnV2ktRmkgb25seScsIHMud2lmaSA/ICdNb2JpbGUgZGF0YSBpcyBuZXZlciB1c2VkJyA6ICdNb2JpbGUgZGF0YSBtYXkgYmUgdXNlZCcsIGJ1c3kpKTsKICAgIHBhcnRzLnB1c2goc3coJ2RhdGEtcGg9ImNoYXJnZSInLCBzLmNoYXJnZSwgJ0ZpcnN0IGJhY2t1cCBvbmx5IHdoaWxlIGNoYXJnaW5nJywgbnVsbCwgYnVzeSkpOwogICAgcGFydHMucHVzaChgPGJ1dHRvbiB0eXBlPSJidXR0b24iIGNsYXNzPSJwaC1yb3ciIGRhdGEtcGg9ImFsYnVtcyIgYXJpYS1leHBhbmRlZD0iJHshIXN0LmFsYnVtc09wZW59Ij48c3BhbiBjbGFzcz0icGgtdHgiPjxiPkFsYnVtczwvYj48c21hbGw+JHtlc2MoYWxidW1TdWIoc3QpKX08L3NtYWxsPjwvc3Bhbj48c3BhbiBjbGFzcz0icGgtZ28iPiR7c3QuYWxidW1zT3BlbiA/ICfijIQnIDogJ+KAuid9PC9zcGFuPjwvYnV0dG9uPmApOwogICAgaWYgKHN0LmFsYnVtc09wZW4pIHBhcnRzLnB1c2goYWxidW1zSHRtbChzdCkpOwogICAgY29uc3QgYWN0cyA9IFtdOwogICAgaWYgKHMuZW5hYmxlZCkgYWN0cy5wdXNoKGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biIgZGF0YS1waD0icnVuIiR7YnVzeSA/ICcgZGlzYWJsZWQnIDogJyd9PiR7c3QuYnVzeSA9PT0gJ3J1bicgPyAnU3RhcnRpbmfigKYnIDogc3QucXVldWVkQXQgJiYgZnJlc2ggPyAnUXVldWVkIOKckyDCtyB0YXAgdG8gcmV0cnknIDogJ0JhY2sgdXAgbm93J308L2J1dHRvbj5gKTsKICAgIGlmIChzLmNvbm5lY3RlZCkgYWN0cy5wdXNoKGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biIgZGF0YS1waD0ic2lnbmluIiR7YnVzeSA/ICcgZGlzYWJsZWQnIDogJyd9PlNpZ24gaW4gYWdhaW48L2J1dHRvbj5gKTsKICAgIGlmIChzLmNvbm5lY3RlZCkgYWN0cy5wdXNoKHN0LmNvbmZpcm1EaXNjb25uZWN0CiAgICAgID8gYDxidXR0b24gdHlwZT0iYnV0dG9uIiBjbGFzcz0icGgtYnRuIGRhbmdlciIgZGF0YS1waD0iZGlzY29ubmVjdC15ZXMiPkRpc2Nvbm5lY3QgdGhpcyBwaG9uZTwvYnV0dG9uPjxidXR0b24gdHlwZT0iYnV0dG9uIiBjbGFzcz0icGgtYnRuIiBkYXRhLXBoPSJkaXNjb25uZWN0LW5vIj5LZWVwPC9idXR0b24+YAogICAgICA6IGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biBnaG9zdCIgZGF0YS1waD0iZGlzY29ubmVjdCI+RGlzY29ubmVjdDwvYnV0dG9uPmApOwogICAgaWYgKGFjdHMubGVuZ3RoKSBwYXJ0cy5wdXNoKGA8ZGl2IGNsYXNzPSJwaC1hY3RzIj4ke2FjdHMuam9pbignJyl9PC9kaXY+YCk7CiAgICBpZiAoc3QuY29uZmlybURpc2Nvbm5lY3QpIHBhcnRzLnB1c2goYDxwIGNsYXNzPSJwaC1ub3RlIj5CYWNrdXAgc3RvcHMgYW5kIHRoaXMgcGhvbmUgZm9yZ2V0cyBpdHMga2V5LiBQaG90b3MgYWxyZWFkeSBvbiB0aGUgc2VydmVyIHN0YXkuPC9wPmApOwogICAgaWYgKHMuc2VydmVyKSBwYXJ0cy5wdXNoKGA8cCBjbGFzcz0icGgtc2VydmVyIj4ke2VzYyhzLnNlcnZlcil9PC9wPmApOwogICAgcGFydHMucHVzaCgnPC9kaXY+Jyk7CiAgICByZXR1cm4gcGFydHMuam9pbignJyk7CiAgfQogIGZ1bmN0aW9uIGFsYnVtU3ViKHN0KSB7CiAgICBjb25zdCBsaXN0ID0gc3QuYWxidW1zPy5hbGJ1bXM7CiAgICBpZiAoIWxpc3QpIHJldHVybiAnQ2FtZXJhIGFuZCBTY3JlZW5zaG90cyB1bmxlc3MgeW91IGNob29zZSc7CiAgICBjb25zdCBuID0gbGlzdC5maWx0ZXIoYSA9PiBhLnNlbGVjdGVkKS5sZW5ndGg7CiAgICByZXR1cm4gbiA/IGAke259IG9mICR7bGlzdC5sZW5ndGh9IGNob3NlbmAgOiAnTm9uZSBjaG9zZW4gwrcgbm90aGluZyBpcyBiYWNrZWQgdXAnOwogIH0KICBmdW5jdGlvbiBhbGJ1bXNIdG1sKHN0KSB7CiAgICBpZiAoc3QuYWxidW1zRXJyb3IpIHJldHVybiBgPHAgY2xhc3M9InBoLW5vdGUiPiR7ZXNjKHN0LmFsYnVtc0Vycm9yKX08L3A+YDsKICAgIGNvbnN0IGxpc3QgPSBzdC5hbGJ1bXM/LmFsYnVtczsgaWYgKCFsaXN0KSByZXR1cm4gJzxwIGNsYXNzPSJwaC1ub3RlIj5Mb2FkaW5nIGFsYnVtc+KApjwvcD4nOwogICAgaWYgKCFsaXN0Lmxlbmd0aCkgcmV0dXJuICc8cCBjbGFzcz0icGgtbm90ZSI+Tm8gYWxidW1zIHZpc2libGUgeWV0LjwvcD4nOwogICAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJwaC1hbGJ1bXMiIHJvbGU9Imdyb3VwIiBhcmlhLWxhYmVsPSJBbGJ1bXMgdG8gYmFjayB1cCI+JHtsaXN0Lm1hcChhID0+IGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWFsYnVtIiByb2xlPSJjaGVja2JveCIgYXJpYS1jaGVja2VkPSIkeyEhYS5zZWxlY3RlZH0iIGRhdGEtcGgtYWxidW09IiR7ZXNjKGEuaWQpfSI+PHNwYW4gY2xhc3M9InBoLWNoZWNrIiBhcmlhLWhpZGRlbj0idHJ1ZSI+PC9zcGFuPiR7ZXNjKGEubmFtZSl9PC9idXR0b24+YCkuam9pbignJyl9PC9kaXY+YDsKICB9CgogIC8qKiBUYXBzIOKGkiBicmlkZ2UgY2FsbHMuIFJldHVybnMgdGhlIHByb21pc2Ugb2YgdGhlIGNhbGwgaXQgbWFkZSAob3IgbnVsbCkuIFB1cmUgb3ZlciAoc3RhdGUsIGNsaWVudCkuICovCiAgZnVuY3Rpb24gYWN0KHN0LCBhcGksIHdoYXQsIGV4dHJhID0ge30pIHsKICAgIGNvbnN0IHMgPSBzdC5zdGF0dXM7IGlmICghcykgcmV0dXJuIG51bGw7CiAgICBjb25zdCBzZXJ2ZXIgPSBuZWVkc1NlcnZlcihzKSA/IFN0cmluZyhleHRyYS5zZXJ2ZXIgPz8gc3Quc2VydmVyID8/ICcnKS50cmltKCkgOiAnJzsKICAgIHN3aXRjaCAod2hhdCkgewogICAgICBjYXNlICdlbmFibGVkJzoKICAgICAgICBpZiAoIXMuZW5hYmxlZCAmJiBuZWVkc1NlcnZlcihzKSAmJiAhc2VydmVyKSB7IHN0LmVycm9yID0gJ0VudGVyIHlvdXIgcGhvdG8gc2VydmVyIGFkZHJlc3MgZmlyc3QnOyByZXR1cm4gbnVsbDsgfQogICAgICAgIHJldHVybiBhcGkuc2V0RW5hYmxlZCghcy5lbmFibGVkLCBzZXJ2ZXIpOwogICAgICBjYXNlICd3aWZpJzogcmV0dXJuIGFwaS5zZXRPcHRpb24oJ3dpZmknLCAhcy53aWZpKTsKICAgICAgY2FzZSAnY2hhcmdlJzogcmV0dXJuIGFwaS5zZXRPcHRpb24oJ2NoYXJnZScsICFzLmNoYXJnZSk7CiAgICAgIGNhc2UgJ3Blcm1pc3Npb25zJzogcmV0dXJuIGFwaS5wZXJtaXNzaW9ucygpOwogICAgICBjYXNlICdydW4nOiByZXR1cm4gYXBpLnJ1bk5vdygpLnRoZW4ociA9PiB7IHN0LnF1ZXVlZEF0ID0gRGF0ZS5ub3coKTsgcmV0dXJuIHI7IH0pOwogICAgICBjYXNlICdzaWduaW4nOiByZXR1cm4gYXBpLmNvbm5lY3Qoc2VydmVyKTsKICAgICAgY2FzZSAnZGlzY29ubmVjdCc6IHN0LmNvbmZpcm1EaXNjb25uZWN0ID0gdHJ1ZTsgcmV0dXJuIG51bGw7CiAgICAgIGNhc2UgJ2Rpc2Nvbm5lY3Qtbm8nOiBzdC5jb25maXJtRGlzY29ubmVjdCA9IGZhbHNlOyByZXR1cm4gbnVsbDsKICAgICAgY2FzZSAnZGlzY29ubmVjdC15ZXMnOiBzdC5jb25maXJtRGlzY29ubmVjdCA9IGZhbHNlOyBzdC5hbGJ1bXMgPSBudWxsOyBzdC5hbGJ1bXNPcGVuID0gZmFsc2U7IHJldHVybiBhcGkuZGlzY29ubmVjdCgpOwogICAgICBjYXNlICdhbGJ1bXMnOgogICAgICAgIHN0LmFsYnVtc09wZW4gPSAhc3QuYWxidW1zT3Blbjsgc3QuYWxidW1zRXJyb3IgPSAnJzsKICAgICAgICBpZiAoIXN0LmFsYnVtc09wZW4pIHJldHVybiBudWxsOwogICAgICAgIHJldHVybiBhcGkuYWxidW1zKCkudGhlbihyID0+IHsgc3QuYWxidW1zID0gcjsgcmV0dXJuIHI7IH0sIGUgPT4geyBzdC5hbGJ1bXNFcnJvciA9IGUubWVzc2FnZTsgdGhyb3cgZTsgfSk7CiAgICAgIGNhc2UgJ2FsYnVtJzogewogICAgICAgIGNvbnN0IGxpc3QgPSBzdC5hbGJ1bXM/LmFsYnVtczsgaWYgKCFsaXN0KSByZXR1cm4gbnVsbDsKICAgICAgICBjb25zdCBhID0gbGlzdC5maW5kKHggPT4geC5pZCA9PT0gZXh0cmEuaWQpOyBpZiAoIWEpIHJldHVybiBudWxsOwogICAgICAgIGEuc2VsZWN0ZWQgPSAhYS5zZWxlY3RlZDsKICAgICAgICByZXR1cm4gYXBpLnNldEFsYnVtcyhsaXN0LmZpbHRlcih4ID0+IHguc2VsZWN0ZWQpLm1hcCh4ID0+IHguaWQpKTsKICAgICAgfQogICAgfQogICAgcmV0dXJuIG51bGw7CiAgfQoKICBjb25zdCBsaWIgPSB7IHdhaXRpbmdGb3IsIFZFUlNJT04sIFQsIFBVU0gsIGNsaWVudCwgZGV0ZWN0LCBzdW1tYXJ5LCBodG1sLCBhY3QsIGFnbywgbmVlZHNTZXJ2ZXIsIGVzYyB9OwogIGlmICh0eXBlb2YgbW9kdWxlID09PSAnb2JqZWN0JyAmJiBtb2R1bGUuZXhwb3J0cykgbW9kdWxlLmV4cG9ydHMgPSBsaWI7IGVsc2Ugcm9vdC5QaG90b3NQYWdlID0gbGliOwp9KSh0eXBlb2Ygd2luZG93ICE9PSAndW5kZWZpbmVkJyA/IHdpbmRvdyA6IGdsb2JhbFRoaXMpOwo='}


if __name__ == "__main__":
    asyncio.run(main())
