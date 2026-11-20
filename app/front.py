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
    "asset.view", "album.create", "album.read", "album.delete", "albumAsset.create", "albumAsset.delete", "server.about", "server.statistics",
    "server.storage", "stack.create", "tag.asset", "tag.create", "tag.read", "user.read", "job.create", "job.read",
    "person.read", "person.statistics", "timeline.read",
]
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


# ---------------- review albums (switch "review-albums", humanitylabs-org/photos#6) ----------------
# The agent fills an album with its picks; the owner scrolls it in Immich and removes what he keeps; "trash the rest"
# trashes exactly what is still in it (same count-and-confirm as move_to_trash), then the album goes. Only albums
# this tool made (review-albums.json) can be trashed this way, never the owner's own albums.

REVIEW_PREFIX = "Cleanup \u00b7 "
REVIEW_CAP = 10000


def _review_on():
    if not switch_on("review-albums"):
        raise RuntimeError("Review albums are switched off on this Wizard (Photos switch review-albums).")


def _review_name(name):
    name = " ".join(str(name or "").split())[:120]
    if name.startswith(REVIEW_PREFIX):
        name = name[len(REVIEW_PREFIX):].strip()
    if not name:
        raise ValueError("Give the album a short name, e.g. 'Screenshots' (it becomes 'Cleanup \u00b7 Screenshots').")
    return REVIEW_PREFIX + name


def _review_albums():
    return {k: v for k, v in (_read_json("review-albums.json", {}) or {}).items() if UUID_RE.match(k)}


def _album_ids(album_id):
    return _matching_ids({"albumIds": [album_id]}, cap=REVIEW_CAP * 5)


def _find_review_album(album_id="", name=""):
    mine = _review_albums()
    if album_id:
        if not UUID_RE.match(str(album_id)):
            raise ValueError("album_id must be an Immich album id")
        if album_id not in mine:
            raise ValueError("That album was not made by photos_review_album, so it is not trashed this way. "
                             "Use photos_move_to_trash with asset_ids instead.")
        return album_id
    full = _review_name(name)
    hits = [a for a in _api("GET", "/albums") or [] if a.get("albumName") == full and a.get("id") in mine]
    if not hits:
        return None
    return hits[0]["id"]


def review_album(name, asset_ids=None, query="", person="", taken_after="", taken_before="", media_type="", city="",
                 favorite=None, limit=1000):
    _review_on()
    full = _review_name(name)
    asset_ids = [str(x) for x in (asset_ids or [])]
    bad = [x for x in asset_ids if not UUID_RE.match(x)]
    if bad:
        raise ValueError("not Immich asset ids: %s" % ", ".join(bad[:5]))
    has_filter = bool(query or person or taken_after or taken_before or media_type or city or favorite is not None)
    if asset_ids and has_filter:
        raise ValueError("Give either asset_ids or a search (query/filters), not both.")
    if asset_ids:
        ids = list(dict.fromkeys(asset_ids))
    elif query:
        f = _filters(person, taken_after, taken_before, media_type, city, favorite)
        size = max(1, min(int(limit or 1000), 1000))
        body = _api("POST", "/search/smart", dict(f, query=str(query)[:500], size=size), timeout=120)
        ids = [a["id"] for a in (body.get("assets") or {}).get("items") or []]
    elif has_filter:
        ids = _matching_ids(_filters(person, taken_after, taken_before, media_type, city, favorite), cap=REVIEW_CAP + 1)
    else:
        raise ValueError("Say which photos: asset_ids, or a search (query and/or person, dates, type, city, favourite).")
    if len(ids) > REVIEW_CAP:
        raise ValueError("That is more than %d photos; narrow it (e.g. by dates) and fill the album in parts." %
                         REVIEW_CAP)
    if not ids:
        return {"album": full, "added": 0, "note": "Nothing matched, so no album was made or changed."}
    album_id = _find_review_album(name=full)
    created = album_id is None
    if created:
        made = _api("POST", "/albums", {"albumName": full, "description": "Made by your Wizard agent for review. "
                    "Remove the photos you want to keep; the agent trashes what is left only when you say so."})
        album_id = made["id"]
        mine = _review_albums()
        mine[album_id] = {"name": full, "created_at": int(time.time())}
        _write_json("review-albums.json", mine)
    added = 0
    for i in range(0, len(ids), 500):
        res = _api("PUT", "/albums/%s/assets" % album_id, {"ids": ids[i:i + 500]}, timeout=300)
        added += sum(1 for r in res or [] if r.get("success"))
    total = len(_album_ids(album_id))
    own = _own_url()
    return {"album": full, "album_id": album_id, "created": created, "added": added,
            "already_in_album": len(ids) - added, "in_album_now": total,
            **({"open": "%s/albums/%s" % (own, album_id)} if own else {}),
            "next": "Tell the owner the album '%s' has %d photos. He scrolls it in Immich (Albums) on his phone and "
                    "removes the ones he wants to keep (select, then Remove from album; that never deletes them). "
                    "When he says to trash the rest, call photos_trash_album." % (full, total)}


def trash_album(album_id="", name="", expected_count=None, confirmed=False):
    _review_on()
    if not (album_id or name):
        raise ValueError("Give the review album's album_id or name.")
    album_id = _find_review_album(album_id, name)
    if not album_id:
        raise ValueError("No review album called %r. photos_review_album makes one." % _review_name(name))
    ids = _album_ids(album_id)
    count = len(ids)
    trash_days = (_immich("GET", "/server/config", timeout=5)[1] or {}).get("trashDays", 30)
    full = _review_albums().get(album_id, {}).get("name")
    if not confirmed:
        sample = []
        if count:
            body = _api("POST", "/search/metadata", {"albumIds": [album_id], "size": 5, "withExif": True,
                                                     "withPeople": True})
            sample = [_asset_row(a, _own_url()) for a in (body.get("assets") or {}).get("items") or []]
        return {"preview": True, "album": full, "would_move_to_trash": count, "sample": sample,
                "next": "Tell the owner exactly how many photos/videos are still in '%s' and get his yes. Then call "
                        "again with confirmed=true and expected_count=%d. They go to Immich's trash (kept %s days, "
                        "restorable) and the album is removed." % (full, count, trash_days)}
    if expected_count is None or int(expected_count) != count:
        raise ValueError("The album now holds %d items, not %s. Preview again and re-confirm with the owner." %
                         (count, expected_count))
    moved = 0
    for i in range(0, len(ids), 500):
        _api("DELETE", "/assets", {"ids": ids[i:i + 500], "force": False}, timeout=300)
        moved += len(ids[i:i + 500])
    _api("DELETE", "/albums/%s" % album_id)
    mine = _review_albums()
    mine.pop(album_id, None)
    _write_json("review-albums.json", mine)
    return {"moved_to_trash": moved, "album_removed": full, "restorable_for_days": trash_days,
            "undo": "photos_restore_from_trash with these asset ids, or Immich > Trash > Restore.",
            "asset_ids": ids if len(ids) <= 500 else None}


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
            # A key made before a scope was added (e.g. the review albums' album.delete) is remade once.
            upgrade = c == 200 and tok and sorted(old.get("permissions") or []) != sorted(KEY_PERMISSIONS)
            if c != 401 and not upgrade:
                return c == 200
        if not tok:
            return False
        if old.get("id"):
            _immich("DELETE", "/api-keys/%s" % old["id"], token=tok)
        c, made = _immich("POST", "/api-keys", {"name": KEY_NAME, "permissions": KEY_PERMISSIONS}, token=tok)
        if c not in (200, 201) or not made.get("secret"):
            return False
        _write_json("agent-key.json", {"id": (made.get("apiKey") or {}).get("id"), "secret": made["secret"],
                                        "user": _owner().get("email"), "permissions": list(KEY_PERMISSIONS),
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


@tool("photos_review_album", "Put a set of photos into an Immich review album named 'Cleanup \u00b7 <name>' (made if "
      "missing, otherwise added to) so the owner can scroll it on his phone and remove the ones he keeps. Pick them "
      "by asset_ids, or by a search like photos_search (query and/or person, dates, type, city, favourite). Nothing "
      "is trashed; when the owner says to trash the rest, use photos_trash_album.",
      {"name": {"type": "string", "description": "Short album name, e.g. 'Screenshots'"},
       "asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10000},
       "query": {"type": "string", "description": "Meaning-based search, best matches first (up to limit)"},
       "person": {"type": "string"}, "taken_after": {"type": "string", "description": "YYYY-MM-DD"},
       "taken_before": {"type": "string", "description": "YYYY-MM-DD"},
       "media_type": {"type": "string", "enum": ["IMAGE", "VIDEO"]}, "city": {"type": "string"},
       "favorite": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
      required=("name",), switch="review-albums")
def photos_review_album(name="", asset_ids=None, query="", person="", taken_after="", taken_before="", media_type="",
                        city="", favorite=None, limit=1000):
    _box_only()
    return review_album(name, asset_ids, query, person, taken_after, taken_before, media_type, city, favorite, limit)


@tool("photos_trash_album", "Trash the rest of a review album: moves exactly the photos still in it to Immich's TRASH "
      "(restorable, never a permanent delete), then removes the album. Only albums made by photos_review_album. First "
      "call without confirmed returns the count and a sample; tell the owner the count, get his yes, then call again "
      "with confirmed=true and expected_count equal to that count.",
      {"album_id": {"type": "string"}, "name": {"type": "string", "description": "The review album's name"},
       "expected_count": {"type": "integer"}, "confirmed": {"type": "boolean"}}, switch="review-albums")
def photos_trash_album(album_id="", name="", expected_count=None, confirmed=False):
    _box_only()
    return trash_album(album_id, name, expected_count, confirmed)


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


# ---- wizard.app-switches/1 (wizard-os docs/APP_SWITCHES.md) ----
# Owner feature switches. New features ship behind one, off by default: append it to SWITCHES
# ({"id", "name", "description", "default": False, "issues": ["humanitylabs-org/photos#N"]}) and gate it on
# switch_on(id). Kept on the box (STATE_DIR/switches.json). Owner only: the box's Tailscale Serve login with
# X-Wizard-Switch: 1; never a bearer or a box-local host.
SWITCHES = [
    {"id": "review-albums", "name": "Review albums for cleanup",
     "description": "The agent can put the photos it thinks are junk (screenshots, receipts, memes) into an Immich "
                    "album such as \"Cleanup \u00b7 Screenshots\". Scroll it on your phone, remove the ones you keep, "
                    "then tell the agent to trash the rest. Albums never trash anything on their own.",
     "default": False, "issues": ["humanitylabs-org/photos#6"]},
]
_SW_LOCAL = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def switches_doc():
    saved = _read_json("switches.json", {}) or {}
    return {"contract": "wizard.app-switches/1", "switches": [
        dict(x, on=saved[x["id"]] if isinstance(saved.get(x["id"]), bool) else bool(x.get("default"))) for x in SWITCHES]}


def switch_on(switch_id):
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
VENDOR = {'index.html': 'PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIiBkYXRhLWFwcD0icGhvdG9zIj4KPGhlYWQ+CiAgPG1ldGEgY2hhcnNldD0idXRmLTgiPgogIDxtZXRhIG5hbWU9InZpZXdwb3J0IiBjb250ZW50PSJ3aWR0aD1kZXZpY2Utd2lkdGgsIGluaXRpYWwtc2NhbGU9MSwgdmlld3BvcnQtZml0PWNvdmVyIj4KICA8bWV0YSBuYW1lPSJjb2xvci1zY2hlbWUiIGNvbnRlbnQ9ImRhcmsgbGlnaHQiPgogIDx0aXRsZT5QaG90b3M8L3RpdGxlPgogIDxzY3JpcHQgc3JjPSJib290LmpzIj48L3NjcmlwdD4KICA8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9IndpemFyZC10b2tlbnMuY3NzIj4KICA8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9IndpemFyZC11aS5jc3MiPgogIDxsaW5rIHJlbD0ic3R5bGVzaGVldCIgaHJlZj0ic3RhbmRhbG9uZS5jc3MiPgogIDxsaW5rIHJlbD0ic3R5bGVzaGVldCIgaHJlZj0ic3R5bGVzLmNzcyI+CiAgPHNjcmlwdCBkZWZlciBzcmM9Ii93aXphcmQtb3MtaG9tZS9jb3JlL2FwcC1jbGllbnQuanMiPjwvc2NyaXB0PgogIDxzY3JpcHQgZGVmZXIgc3JjPSJ3aXphcmQtc2hlZXQuanMiPjwvc2NyaXB0PgogIDxzY3JpcHQgZGVmZXIgc3JjPSJwaG90b3MuanMiPjwvc2NyaXB0PgogIDxzY3JpcHQgZGVmZXIgc3JjPSJhcHAuanMiPjwvc2NyaXB0Pgo8L2hlYWQ+Cjxib2R5PgogIDxkaXYgY2xhc3M9Ind6LWFwcCBwaC1hcHAiPgogICAgPGhlYWRlciBjbGFzcz0id3otaGVhZCBwaC1oZWFkIj4KICAgICAgPGgxPlBob3RvczwvaDE+CiAgICAgIDxwIGlkPSJsaW5lIj5Mb2FkaW5n4oCmPC9wPgogICAgICA8bmF2IGNsYXNzPSJwaC1hY3Rpb25zIiBhcmlhLWxhYmVsPSJQaG90b3MgYWN0aW9ucyI+CiAgICAgICAgPGJ1dHRvbiBpZD0ib3Blbi1iYWNrdXAiIGNsYXNzPSJ3ei1jaGlwIiB0eXBlPSJidXR0b24iIGhpZGRlbj5QaG9uZSBiYWNrdXA8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGlkPSJvcGVuLWltcG9ydCIgY2xhc3M9Ind6LWNoaXAiIHR5cGU9ImJ1dHRvbiI+SW1wb3J0IGZyb20gR29vZ2xlPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBpZD0ib3Blbi1hZ2VudCIgY2xhc3M9Ind6LWNoaXAiIHR5cGU9ImJ1dHRvbiI+QWdlbnQ8L2J1dHRvbj4KICAgICAgICA8YSBpZD0ib3Blbi1mdWxsIiBjbGFzcz0id3otY2hpcCIgdGFyZ2V0PSJfYmxhbmsiIHJlbD0ibm9vcGVuZXIiIGhpZGRlbj5PcGVuIGZ1bGwgc2NyZWVuIOKGlzwvYT4KICAgICAgPC9uYXY+CiAgICA8L2hlYWRlcj4KICAgIDxidXR0b24gaWQ9Im9wZW4tbWVudSIgY2xhc3M9InBoLW1vcmUiIHR5cGU9ImJ1dHRvbiIgYXJpYS1sYWJlbD0iUGhvdG9zIG1lbnUiIGFyaWEtaGFzcG9wdXA9ImRpYWxvZyIgYXJpYS1jb250cm9scz0ibWVudS1zaGVldCI+PHN2ZyB2aWV3Qm94PSIwIDAgMjQgMjQiIGFyaWEtaGlkZGVuPSJ0cnVlIj48Y2lyY2xlIGN4PSI1IiBjeT0iMTIiIHI9IjEuOSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjEuOSIvPjxjaXJjbGUgY3g9IjE5IiBjeT0iMTIiIHI9IjEuOSIvPjwvc3ZnPjwvYnV0dG9uPgogICAgPHNlY3Rpb24gaWQ9InNldHVwIiBjbGFzcz0id3otY2FyZCBwaC1zZXR1cCIgaGlkZGVuPjwvc2VjdGlvbj4KICAgIDxkaXYgaWQ9ImZyYW1lLXdyYXAiIGNsYXNzPSJwaC1mcmFtZS13cmFwIiBoaWRkZW4+CiAgICAgIDxpZnJhbWUgaWQ9ImltbWljaCIgdGl0bGU9IkltbWljaCIgYWxsb3c9ImZ1bGxzY3JlZW47IGNsaXBib2FyZC1yZWFkOyBjbGlwYm9hcmQtd3JpdGUiIHJlZmVycmVycG9saWN5PSJzYW1lLW9yaWdpbiI+PC9pZnJhbWU+CiAgICA8L2Rpdj4KICAgIDxwIGlkPSJzdGF0dXMiIGNsYXNzPSJ3ei1zdGF0dXMiIHJvbGU9InN0YXR1cyIgYXJpYS1saXZlPSJwb2xpdGUiPjwvcD4KICA8L2Rpdj4KCiAgPGRpYWxvZyBpZD0ibWVudS1zaGVldCIgY2xhc3M9Ind6LXNoZWV0IiBhcmlhLWxhYmVsbGVkYnk9Im1lbnUtaCI+CiAgICA8aDIgaWQ9Im1lbnUtaCI+UGhvdG9zPC9oMj4KICAgIDxwIGlkPSJtZW51LWxpbmUiIGNsYXNzPSJwaC1oZWxwIj48L3A+CiAgICA8dWwgY2xhc3M9Ind6LWxpc3QgcGgtbWVudSIgYXJpYS1sYWJlbD0iUGhvdG9zIGFjdGlvbnMiPgogICAgICA8bGkgaGlkZGVuPjxidXR0b24gY2xhc3M9Ind6LXJvdyIgdHlwZT0iYnV0dG9uIiBkYXRhLW1lbnU9Im9wZW4tYmFja3VwIj48c3BhbiBjbGFzcz0id3otbWFpbiI+PGI+UGhvbmUgYmFja3VwPC9iPjwvc3Bhbj48L2J1dHRvbj48L2xpPgogICAgICA8bGk+PGJ1dHRvbiBjbGFzcz0id3otcm93IiB0eXBlPSJidXR0b24iIGRhdGEtbWVudT0ib3Blbi1pbXBvcnQiPjxzcGFuIGNsYXNzPSJ3ei1tYWluIj48Yj5JbXBvcnQgZnJvbSBHb29nbGU8L2I+PC9zcGFuPjwvYnV0dG9uPjwvbGk+CiAgICAgIDxsaT48YnV0dG9uIGNsYXNzPSJ3ei1yb3ciIHR5cGU9ImJ1dHRvbiIgZGF0YS1tZW51PSJvcGVuLWFnZW50Ij48c3BhbiBjbGFzcz0id3otbWFpbiI+PGI+QWdlbnQ8L2I+PC9zcGFuPjwvYnV0dG9uPjwvbGk+CiAgICAgIDxsaSBoaWRkZW4+PGEgY2xhc3M9Ind6LXJvdyIgZGF0YS1tZW51PSJvcGVuLWZ1bGwiIHRhcmdldD0iX2JsYW5rIiByZWw9Im5vb3BlbmVyIj48c3BhbiBjbGFzcz0id3otbWFpbiI+PGI+T3BlbiBmdWxsIHNjcmVlbiDihpc8L2I+PC9zcGFuPjwvYT48L2xpPgogICAgPC91bD4KICA8L2RpYWxvZz4KCiAgPGRpYWxvZyBpZD0iYmFja3VwLXNoZWV0IiBjbGFzcz0id3otc2hlZXQiIGFyaWEtbGFiZWxsZWRieT0iYmFja3VwLWgiPgogICAgPGgyIGlkPSJiYWNrdXAtaCI+QmFjayB1cCB0aGlzIHBob25lPC9oMj4KICAgIDxkaXYgaWQ9ImJhY2t1cC1yb290IiBhcmlhLWxpdmU9InBvbGl0ZSI+PC9kaXY+CiAgPC9kaWFsb2c+CgogIDxkaWFsb2cgaWQ9ImltcG9ydC1zaGVldCIgY2xhc3M9Ind6LXNoZWV0IiBhcmlhLWxhYmVsbGVkYnk9ImltcG9ydC1oIj4KICAgIDxoMiBpZD0iaW1wb3J0LWgiPkltcG9ydCBmcm9tIEdvb2dsZSBQaG90b3M8L2gyPgogICAgPG9sIGNsYXNzPSJwaC1zdGVwcyI+CiAgICAgIDxsaT5PbiBhIGNvbXB1dGVyLCBvcGVuIDxhIGhyZWY9Imh0dHBzOi8vdGFrZW91dC5nb29nbGUuY29tIiB0YXJnZXQ9Il9ibGFuayIgcmVsPSJub29wZW5lciI+dGFrZW91dC5nb29nbGUuY29tPC9hPiwgdGFwIDxiPkRlc2VsZWN0IGFsbDwvYj4sIHRoZW4gdGljayBvbmx5IDxiPkdvb2dsZSBQaG90b3M8L2I+LjwvbGk+CiAgICAgIDxsaT5OZXh0IHN0ZXA6IDxiPlNlbmQgZG93bmxvYWQgbGluayB2aWEgZW1haWw8L2I+LCA8Yj5FeHBvcnQgb25jZTwvYj4sIGZpbGUgdHlwZSA8Yj4uemlwPC9iPiwgc2l6ZSA8Yj41MCBHQjwvYj4uIENyZWF0ZSBleHBvcnQuPC9saT4KICAgICAgPGxpPldoZW4gR29vZ2xlJ3MgZW1haWwgYXJyaXZlcywgZG93bmxvYWQgZXZlcnkgcGFydCBhbmQgcHV0IHRoZW0gYWxsIGluIDxiPkZpbGVzIOKAuiBQaG90b3Mg4oC6IEltcG9ydDwvYj4uPC9saT4KICAgICAgPGxpPlRhcCA8Yj5JbXBvcnQgbm93PC9iPi4gRGF0ZXMsIHBsYWNlcywgZGVzY3JpcHRpb25zIGFuZCBhbGJ1bXMgY29tZSBhY3Jvc3MuIFJ1bm5pbmcgaXQgYWdhaW4gaXMgc2FmZS48L2xpPgogICAgPC9vbD4KICAgIDxkaXYgaWQ9ImltcG9ydC1zdGF0ZSIgY2xhc3M9InBoLWltcG9ydC1zdGF0ZSI+PC9kaXY+CiAgICA8dWwgY2xhc3M9Ind6LWxpc3QiIGlkPSJpbXBvcnQtb3B0aW9ucyIgYXJpYS1sYWJlbD0iSW1wb3J0IG9wdGlvbnMiPjwvdWw+CiAgICA8ZGl2IGNsYXNzPSJ3ei1hY3Rpb25zIj48YnV0dG9uIGlkPSJpbXBvcnQtZ28iIGNsYXNzPSJ3ei1idXR0b24iIHR5cGU9ImJ1dHRvbiIgZGlzYWJsZWQ+SW1wb3J0IG5vdzwvYnV0dG9uPjwvZGl2PgogICAgPHByZSBpZD0iaW1wb3J0LWxvZyIgY2xhc3M9InBoLWxvZyIgaGlkZGVuPjwvcHJlPgogIDwvZGlhbG9nPgoKICA8ZGlhbG9nIGlkPSJhZ2VudC1zaGVldCIgY2xhc3M9Ind6LXNoZWV0IiBhcmlhLWxhYmVsbGVkYnk9ImFnZW50LWgiPgogICAgPGgyIGlkPSJhZ2VudC1oIj5Zb3VyIGFnZW50IGFuZCBQaG90b3M8L2gyPgogICAgPHAgaWQ9ImFnZW50LWxpbmUiIGNsYXNzPSJwaC1oZWxwIj48L3A+CiAgICA8cCBjbGFzcz0icGgtaGVscCI+V2l0aCB5b3VyIHllcywgdGhlIGFnZW50IGNhbiBjb3VudCBhbmQgZmluZCBwaG90b3MgKGJ5IHBlcnNvbiwgZGF0ZSwgcGxhY2Ugb3Igd2hhdCBpcyBpbiB0aGVtKSwgc2VlIGZhY2UgZ3JvdXBzLCBydW4gdGhlIEdvb2dsZSBpbXBvcnQgYW5kIG1vdmUgcGhvdG9zIHlvdSBjaG9vc2UgdG8gSW1taWNoJ3MgdHJhc2guIEl0IGNhbm5vdCBlbXB0eSB0aGUgdHJhc2ggb3IgZGVsZXRlIGFueXRoaW5nIHBlcm1hbmVudGx5LjwvcD4KICAgIDxwIGNsYXNzPSJwaC1oZWxwIj5UaGVyZSBpcyBubyBzaWduLWluOiB5b3VyIFRhaWxzY2FsZSBpcyB0aGUgbG9jay4gT25seSBkZXZpY2VzIG9uIHlvdXIgdGFpbG5ldCBjYW4gb3BlbiBQaG90b3MsIGhlcmUgb3IgaW4gdGhlIEltbWljaCBwaG9uZSBhcHAuPC9wPgogICAgPHAgaWQ9InBob25lLWxpbmUiIGNsYXNzPSJwaC1oZWxwIj48L3A+CiAgPC9kaWFsb2c+CjwvYm9keT4KPC9odG1sPgo=', 'app.js': 'J3VzZSBzdHJpY3QnOwpjb25zdCBlbCA9IGlkID0+IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKTsKbGV0IHN0YXRlID0gbnVsbDsKCmZ1bmN0aW9uIHNheSh0ZXh0LCBraW5kID0gJycpIHsgLy8ga2luZDogJycgKHNjcmVlbiByZWFkZXJzIG9ubHkpLCAnZXJyb3InLCAnbm90ZScKICBjb25zdCBzID0gZWwoJ3N0YXR1cycpOwogIHMuY2xhc3NMaXN0LnRvZ2dsZSgnd3otc3InLCAha2luZCk7CiAgcy5jbGFzc0xpc3QudG9nZ2xlKCdub3RlJywga2luZCA9PT0gJ25vdGUnKTsKICBzLnRleHRDb250ZW50ID0gdGV4dDsKfQoKYXN5bmMgZnVuY3Rpb24gcmVxdWVzdChwYXRoLCBkYXRhKSB7CiAgY29uc3QgaW5pdCA9IGRhdGEgPT09IHVuZGVmaW5lZCA/IHsgY3JlZGVudGlhbHM6ICdzYW1lLW9yaWdpbicgfQogICAgOiB7IG1ldGhvZDogJ1BPU1QnLCBjcmVkZW50aWFsczogJ3NhbWUtb3JpZ2luJywgaGVhZGVyczogeyAnQ29udGVudC1UeXBlJzogJ2FwcGxpY2F0aW9uL2pzb24nIH0sIGJvZHk6IEpTT04uc3RyaW5naWZ5KGRhdGEpIH07CiAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBmZXRjaChwYXRoLCBpbml0KTsKICBsZXQgYm9keSA9IHt9OwogIHRyeSB7IGJvZHkgPSBhd2FpdCByZXNwb25zZS5qc29uKCk7IH0gY2F0Y2ggKGUpIHt9CiAgaWYgKCFyZXNwb25zZS5vaykgeyBjb25zdCBlID0gbmV3IEVycm9yKGJvZHkuZXJyb3IgfHwgJ1Bob3RvcyBjb3VsZCBub3QgZG8gdGhhdC4gVHJ5IGFnYWluLicpOyBlLmNvZGUgPSBib2R5LmNvZGU7IHRocm93IGU7IH0KICByZXR1cm4gYm9keTsKfQoKZnVuY3Rpb24gbih4KSB7IHJldHVybiB0eXBlb2YgeCA9PT0gJ251bWJlcicgPyB4LnRvTG9jYWxlU3RyaW5nKCkgOiAnMCc7IH0KCmZ1bmN0aW9uIHNldHVwQ2FyZCh0aXRsZSwgdGV4dCkgewogIGNvbnN0IGNhcmQgPSBlbCgnc2V0dXAnKTsKICBjYXJkLnJlcGxhY2VDaGlsZHJlbigpOwogIGNvbnN0IGggPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdoMicpOyBoLnRleHRDb250ZW50ID0gdGl0bGU7CiAgY29uc3QgcCA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3AnKTsgcC5jbGFzc05hbWUgPSAncGgtaGVscCc7IHAudGV4dENvbnRlbnQgPSB0ZXh0OwogIGNhcmQuYXBwZW5kKGgsIHApOwogIGNhcmQuaGlkZGVuID0gZmFsc2U7Cn0KCmZ1bmN0aW9uIHJlbmRlcihzKSB7CiAgc3RhdGUgPSBzOwogIGNvbnN0IG93biA9IHMubGlua3MgJiYgcy5saW5rcy5pbW1pY2g7CiAgY29uc3QgaW0gPSBzLmltbWljaCB8fCB7fTsKICBsZXQgbGluZSA9ICcnOwogIGlmICghaW0ucmVhY2hhYmxlKSBsaW5lID0gJ1N0YXJ0aW5nIEltbWljaOKApic7CiAgZWxzZSBpZiAoIWltLmhhc19vd25lcl9hY2NvdW50IHx8ICFzLmFnZW50X2Nvbm5lY3RlZCkgbGluZSA9ICdTZXR0aW5nIHVw4oCmJzsKICBlbHNlIGlmIChzLmxpYnJhcnkpIGxpbmUgPSBuKHMubGlicmFyeS5waG90b3MpICsgJyBwaG90b3MgwrcgJyArIG4ocy5saWJyYXJ5LnZpZGVvcykgKyAnIHZpZGVvcyDCtyAnICsgKHMubGlicmFyeS5zaXplX2diIHx8IDApICsgJyBHQic7CiAgZWxzZSBsaW5lID0gJ0ltbWljaCAnICsgKGltLnZlcnNpb24gfHwgJycpOwogIGNvbnN0IGJ1c3kgPSBzLmJhY2tncm91bmRfd29yayAmJiBPYmplY3QudmFsdWVzKHMuYmFja2dyb3VuZF93b3JrKS5yZWR1Y2UoKGEsIHEpID0+IGEgKyAocS53YWl0aW5nIHx8IDApICsgKHEuYWN0aXZlIHx8IDApLCAwKTsKICBpZiAoYnVzeSkgbGluZSArPSAnIMK3IG9yZ2FuaXNpbmcgJyArIG4oYnVzeSk7CiAgaWYgKHMuaW1wb3J0ICYmIHMuaW1wb3J0LnN0YXRlID09PSAncnVubmluZycpIGxpbmUgKz0gJyDCtyBpbXBvcnRpbmcnOwogIGVsKCdsaW5lJykudGV4dENvbnRlbnQgPSBsaW5lOwogIGVsKCdtZW51LWxpbmUnKS50ZXh0Q29udGVudCA9IGxpbmU7CgogIGVsKCdzZXR1cCcpLmhpZGRlbiA9IHRydWU7CiAgaWYgKCFvd24pIHsKICAgIHNldHVwQ2FyZCgnT3BlbiBQaG90b3MgZnJvbSB5b3VyIFdpemFyZCcsICdUaGlzIHBhZ2UgbmVlZHMgeW91ciBXaXphcmRcdTIwMTlzIHByaXZhdGUgYWRkcmVzcyB0byBzaG93IHlvdXIgbGlicmFyeS4nKTsKICB9IGVsc2UgaWYgKCFpbS5yZWFjaGFibGUpIHsKICAgIHNldHVwQ2FyZCgnUGhvdG9zIGlzIHN0YXJ0aW5nJywgJ1RoZSBmaXJzdCBzdGFydCB0YWtlcyBhIG1pbnV0ZSBvciB0d28uIFRoaXMgcGFnZSByZWZyZXNoZXMgb24gaXRzIG93bi4nKTsKICB9IGVsc2UgaWYgKCEocy5zaWduX2luICYmIHMuc2lnbl9pbi5yZWFkeSkpIHsKICAgIHNldHVwQ2FyZCgnUGhvdG9zIGlzIHNldHRpbmcgdXAnLCAocy5zaWduX2luICYmIHMuc2lnbl9pbi5wcm9ibGVtKSB8fCAnUGhvdG9zIG1ha2VzIGl0cyBvd24gbGlicmFyeSBmb3IgeW91OyB0aGVyZSBpcyBub3RoaW5nIHRvIHNpZ24gaW4gdG8uIFRoaXMgcGFnZSByZWZyZXNoZXMgb24gaXRzIG93bi4nKTsKICB9CiAgZWwoJ29wZW4tZnVsbCcpLmhpZGRlbiA9ICFvd247CiAgaWYgKG93bikgZWwoJ29wZW4tZnVsbCcpLmhyZWYgPSBvd247CiAgY29uc3QgZnJhbWUgPSBlbCgnaW1taWNoJyk7CiAgaWYgKG93biAmJiBpbS5yZWFjaGFibGUgJiYgcy5zaWduX2luICYmIHMuc2lnbl9pbi5yZWFkeSkgewogICAgZWwoJ2ZyYW1lLXdyYXAnKS5oaWRkZW4gPSBmYWxzZTsKICAgIGlmICghZnJhbWUuZ2V0QXR0cmlidXRlKCdzcmMnKSkgZnJhbWUuc3JjID0gb3duOwogIH0KICBjb25zdCBwaG9uZSA9IHMubGlua3MgJiYgcy5saW5rcy5tb2JpbGVfc2VydmVyX3VybDsKICBlbCgncGhvbmUtbGluZScpLnRleHRDb250ZW50ID0gcGhvbmUgPyAnUGhvbmUgYmFja3VwOiBpbnN0YWxsIHRoZSBJbW1pY2ggYXBwLCB0eXBlICcgKyBwaG9uZSArICcgYXMgdGhlIHNlcnZlciwgdGFwIE5leHQsIHRoZW4gQ29udGludWUuIEtlZXAgVGFpbHNjYWxlIG9uLicgOiAnJzsKICBpZiAocGhvbmUpIGJhY2t1cC5zZXJ2ZXIgPSBwaG9uZTsKICBlbCgnYWdlbnQtbGluZScpLnRleHRDb250ZW50ID0gcy5hZ2VudF9jb25uZWN0ZWQgPyAnWW91ciBhZ2VudCBpcyBjb25uZWN0ZWQgdG8gUGhvdG9zLicgOiAnWW91ciBhZ2VudCBjb25uZWN0cyBieSBpdHNlbGYgb25jZSBQaG90b3MgaGFzIGZpbmlzaGVkIHNldHRpbmcgdXAuJzsKfQoKZnVuY3Rpb24gcmVuZGVySW1wb3J0KGQpIHsKICBjb25zdCBzdCA9IGQuc3RhdGU7CiAgY29uc3Qgd29yZHMgPSB7IGlkbGU6ICdObyBpbXBvcnQgeWV0LicsIHJ1bm5pbmc6ICdJbXBvcnRpbmfigKYgeW91IGNhbiBjbG9zZSB0aGlzOyBpdCBrZWVwcyBnb2luZy4nLCBkb25lOiAnTGFzdCBpbXBvcnQgZmluaXNoZWQuJywKICAgIGZhaWxlZDogJ1RoZSBsYXN0IGltcG9ydCBzdG9wcGVkOiAnICsgKGQubWVzc2FnZSB8fCAnc2VlIHRoZSBsb2cgYmVsb3cuJyksIGludGVycnVwdGVkOiAnVGhlIGxhc3QgaW1wb3J0IHdhcyBpbnRlcnJ1cHRlZC4gSW1wb3J0IGFnYWluIHRvIGNvbnRpbnVlOyBub3RoaW5nIGlzIGR1cGxpY2F0ZWQuJyB9OwogIGVsKCdpbXBvcnQtc3RhdGUnKS50ZXh0Q29udGVudCA9ICh3b3Jkc1tzdF0gfHwgc3QpICsgJyAnICsgKGQud2FpdGluZ19maWxlcyA/IGQud2FpdGluZ19maWxlcyArICcgZmlsZShzKSwgJyArIGQud2FpdGluZ19nYiArICcgR0IgaW4gRmlsZXMg4oC6IFBob3RvcyDigLogSW1wb3J0LicgOiAnTm8gVGFrZW91dCBmaWxlcyBpbiBGaWxlcyDigLogUGhvdG9zIOKAuiBJbXBvcnQgeWV0LicpOwogIGVsKCdpbXBvcnQtZ28nKS5kaXNhYmxlZCA9IHN0ID09PSAncnVubmluZycgfHwgIWQud2FpdGluZ19maWxlcyB8fCAhKHN0YXRlICYmIHN0YXRlLmFnZW50X2Nvbm5lY3RlZCk7CiAgaWYgKCEoc3RhdGUgJiYgc3RhdGUuYWdlbnRfY29ubmVjdGVkKSAmJiBkLndhaXRpbmdfZmlsZXMpIGVsKCdpbXBvcnQtc3RhdGUnKS50ZXh0Q29udGVudCArPSAnIFBob3RvcyBpcyBzdGlsbCBzZXR0aW5nIHVwOyBJbXBvcnQgbm93IHdvcmtzIGluIGEgbWludXRlLic7CiAgY29uc3QgbGlzdCA9IGVsKCdpbXBvcnQtb3B0aW9ucycpOyBsaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogIGNvbnN0IGxhYmVscyA9IHsgaW5jbHVkZV9hcmNoaXZlZDogJ0luY2x1ZGUgYXJjaGl2ZWQgcGhvdG9zJywgaW5jbHVkZV9wYXJ0bmVyOiAnSW5jbHVkZSBwYXJ0bmVyLXNoYXJlZCBwaG90b3MnLCBzeW5jX2FsYnVtczogJ1JlY3JlYXRlIG15IEdvb2dsZSBhbGJ1bXMnLCBpbmNsdWRlX3RyYXNoZWQ6ICdJbmNsdWRlIHBob3RvcyBpbiBHb29nbGVcdTIwMTlzIHRyYXNoJyB9OwogIGZvciAoY29uc3QgW2ssIGxhYmVsXSBvZiBPYmplY3QuZW50cmllcyhsYWJlbHMpKSB7CiAgICBjb25zdCBsaSA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2xpJyk7CiAgICBjb25zdCByb3cgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTsgcm93LnR5cGUgPSAnYnV0dG9uJzsgcm93LmNsYXNzTmFtZSA9ICd3ei1yb3cnOwogICAgY29uc3Qgb24gPSAhIShkLm9wdGlvbnMgfHwge30pW2tdOwogICAgcm93LnNldEF0dHJpYnV0ZSgnYXJpYS1wcmVzc2VkJywgU3RyaW5nKG9uKSk7CiAgICBjb25zdCBib3ggPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7IGJveC5jbGFzc05hbWUgPSAnd3otY2hlY2snOyBib3guc2V0QXR0cmlidXRlKCdhcmlhLWhpZGRlbicsICd0cnVlJyk7CiAgICBib3guaW5uZXJIVE1MID0gJzxpPjxzdmcgdmlld0JveD0iMCAwIDI0IDI0Ij48cGF0aCBkPSJNNSAxMi41bDQuNSA0LjVMMTkgNy41Ii8+PC9zdmc+PC9pPic7CiAgICBjb25zdCBtYWluID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpOyBtYWluLmNsYXNzTmFtZSA9ICd3ei1tYWluJzsKICAgIGNvbnN0IGIgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCdiJyk7IGIudGV4dENvbnRlbnQgPSBsYWJlbDsgbWFpbi5hcHBlbmQoYik7CiAgICByb3cuYXBwZW5kKGJveCwgbWFpbik7CiAgICByb3cub25jbGljayA9IGFzeW5jICgpID0+IHsKICAgICAgcm93LmRpc2FibGVkID0gdHJ1ZTsKICAgICAgdHJ5IHsgYXdhaXQgcmVxdWVzdCgnYXBpL2ltcG9ydC9vcHRpb25zJywgeyBba106ICFvbiB9KTsgcmVuZGVySW1wb3J0KGF3YWl0IHJlcXVlc3QoJ2FwaS9pbXBvcnQnKSk7IH0KICAgICAgY2F0Y2ggKGUpIHsgc2F5KGUubWVzc2FnZSwgJ2Vycm9yJyk7IHJvdy5kaXNhYmxlZCA9IGZhbHNlOyB9CiAgICB9OwogICAgbGkuYXBwZW5kKHJvdyk7IGxpc3QuYXBwZW5kKGxpKTsKICB9CiAgY29uc3QgbG9nID0gZWwoJ2ltcG9ydC1sb2cnKTsKICBsb2cuaGlkZGVuID0gIShkLmxvZ190YWlsICYmIGQubG9nX3RhaWwubGVuZ3RoKTsKICBsb2cudGV4dENvbnRlbnQgPSAoZC5sb2dfdGFpbCB8fCBbXSkuam9pbignXG4nKTsKfQoKYXN5bmMgZnVuY3Rpb24gcmVmcmVzaCgpIHsKICB0cnkgeyByZW5kZXIoYXdhaXQgcmVxdWVzdCgnYXBpL3N0YXR1cycpKTsgfQogIGNhdGNoIChlKSB7IHNheShlLm1lc3NhZ2UsICdlcnJvcicpOyB9Cn0KCmVsKCdvcGVuLWltcG9ydCcpLm9uY2xpY2sgPSBhc3luYyAoKSA9PiB7CiAgV3pTaGVldC5vcGVuKGVsKCdpbXBvcnQtc2hlZXQnKSk7CiAgdHJ5IHsgcmVuZGVySW1wb3J0KGF3YWl0IHJlcXVlc3QoJ2FwaS9pbXBvcnQnKSk7IH0gY2F0Y2ggKGUpIHsgc2F5KGUubWVzc2FnZSwgJ2Vycm9yJyk7IH0KfTsKZWwoJ29wZW4tYWdlbnQnKS5vbmNsaWNrID0gKCkgPT4gV3pTaGVldC5vcGVuKGVsKCdhZ2VudC1zaGVldCcpKTsKCi8vIFRoZSDii68gbWVudSAobmFycm93IHdpZHRocywgd2hlcmUgdGhlIGhlYWRlciBpcyBoaWRkZW4pOiB0aGUgc2FtZSBhY3Rpb25zIGFzIHRoZSBoZWFkZXIgY2hpcHMsIGluIG9uZSBzaGVldC4KZnVuY3Rpb24gc3luY01lbnUoKSB7CiAgZm9yIChjb25zdCByb3cgb2YgZWwoJ21lbnUtc2hlZXQnKS5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1tZW51XScpKSB7CiAgICBjb25zdCBjaGlwID0gZWwocm93LmRhdGFzZXQubWVudSk7CiAgICByb3cucGFyZW50RWxlbWVudC5oaWRkZW4gPSBjaGlwLmhpZGRlbjsKICAgIHJvdy5xdWVyeVNlbGVjdG9yKCdiJykudGV4dENvbnRlbnQgPSBjaGlwLnRleHRDb250ZW50OwogICAgaWYgKGNoaXAuaHJlZikgcm93LmhyZWYgPSBjaGlwLmhyZWY7CiAgfQp9CmVsKCdvcGVuLW1lbnUnKS5vbmNsaWNrID0gKCkgPT4geyBzeW5jTWVudSgpOyBXelNoZWV0Lm9wZW4oZWwoJ21lbnUtc2hlZXQnKSk7IH07CmVsKCdtZW51LXNoZWV0JykuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLCBlID0+IHsKICBjb25zdCByb3cgPSBlLnRhcmdldC5jbG9zZXN0KCdbZGF0YS1tZW51XScpOyBpZiAoIXJvdykgcmV0dXJuOwogIFd6U2hlZXQuY2xvc2UoZWwoJ21lbnUtc2hlZXQnKSk7CiAgaWYgKHJvdy5kYXRhc2V0Lm1lbnUgIT09ICdvcGVuLWZ1bGwnKSBlbChyb3cuZGF0YXNldC5tZW51KS5jbGljaygpOyAvLyB0aGUgbGluayBvcGVucyBieSBpdHNlbGYKfSk7CmVsKCdpbXBvcnQtZ28nKS5vbmNsaWNrID0gYXN5bmMgKCkgPT4gewogIGVsKCdpbXBvcnQtZ28nKS5kaXNhYmxlZCA9IHRydWU7CiAgdHJ5IHsgY29uc3QgciA9IGF3YWl0IHJlcXVlc3QoJ2FwaS9pbXBvcnQvc3RhcnQnLCB7fSk7IHNheShyLnN0YXJ0ZWQgPyAnSW1wb3J0IHN0YXJ0ZWQuJyA6IChyLm1lc3NhZ2UgfHwgJ05vdCBzdGFydGVkLicpLCAnbm90ZScpOyB9CiAgY2F0Y2ggKGUpIHsgc2F5KGUubWVzc2FnZSwgJ2Vycm9yJyk7IH0KICB0cnkgeyByZW5kZXJJbXBvcnQoYXdhaXQgcmVxdWVzdCgnYXBpL2ltcG9ydCcpKTsgfSBjYXRjaCAoZSkge30KfTsKaWYgKCF3aW5kb3cuV2l6YXJkQXBwIHx8IFdpemFyZEFwcC52ZXJzaW9uICE9PSAxKSB7CiAgc2F5KCdXaXphcmQgc3lzdGVtIGZ1bmN0aW9ucyBhcmUgdW5hdmFpbGFibGUuIE9wZW4gUGhvdG9zIHRocm91Z2ggV2l6YXJkLicsICdlcnJvcicpOwp9IGVsc2UgewogIFdpemFyZEFwcC5yZWFkeSh7IG5hbWU6ICdQaG90b3MnLCB2ZXJzaW9uOiAnMC40LjEnIH0pOwogIFdpemFyZEFwcC5iYWNrLnJlZ2lzdGVyKHsgY2FuR29CYWNrOiAoKSA9PiBmYWxzZSwgYmFjazogKCkgPT4gZmFsc2UgfSk7Cn0KCnJlZnJlc2goKTsKc2V0SW50ZXJ2YWwoKCkgPT4geyBpZiAoIWRvY3VtZW50LmhpZGRlbikgcmVmcmVzaCgpOyB9LCAxNTAwMCk7CgovLyAtLS0tIFBob25lIGJhY2t1cCAoZnJvbSB0aGUgV2l6YXJkIE9TIEFuZHJvaWQgYXBwKSAtLS0tCi8vIFRoZSBiYWNrdXAgd29ya2VyIGxpdmVzIGluIFdpemFyZCBPUyBmb3IgQW5kcm9pZDsgdGhpcyBwYWdlIGRyaXZlcyBpdCB0aHJvdWdoIHRoZSBzaGVsbCdzIFBob3RvcyByZWxheQovLyAocGhvdG9zLmpzLCB1bmNoYW5nZWQgZnJvbSB0aGUgZm9ybWVyIHZpZXctb25seSBQaG90b3MgYXBwKS4gVGhlIHBob3RvIHNlcnZlciBpcyB0aGlzIGFwcCdzIG93biBJbW1pY2gsIHNvIHRoZQovLyBvd25lciBuZXZlciB0eXBlcyBhbiBhZGRyZXNzLiBJbiBhIGJyb3dzZXIgb3IgdGhlIGRlc2t0b3Agc2hlbGwgdGhlcmUgaXMgbm8gcmVsYXkgYW5kIHRoZSBidXR0b24gc3RheXMgaGlkZGVuLgpjb25zdCBiYWNrdXAgPSB7IG1vZGU6ICcnLCBzdGF0dXM6IG51bGwsIGVycm9yOiAnJywgYnVzeTogZmFsc2UsIHNlcnZlcjogJycsIGFsYnVtc09wZW46IGZhbHNlLCBhbGJ1bXM6IG51bGwsIGFsYnVtc0Vycm9yOiAnJywgY29uZmlybURpc2Nvbm5lY3Q6IGZhbHNlIH07CmNvbnN0IFAgPSB3aW5kb3cuUGhvdG9zUGFnZTsKY29uc3QgZnJhbWVkID0gd2luZG93LnBhcmVudCAmJiB3aW5kb3cucGFyZW50ICE9PSB3aW5kb3c7CmNvbnN0IGJhcGkgPSBQLmNsaWVudChtc2cgPT4gewogIGlmICghZnJhbWVkKSB0aHJvdyBPYmplY3QuYXNzaWduKG5ldyBFcnJvcignbm8tYnJpZGdlJyksIHsgY29kZTogJ25vLWJyaWRnZScgfSk7CiAgd2luZG93LnBhcmVudC5wb3N0TWVzc2FnZShtc2csIGxvY2F0aW9uLm9yaWdpbik7Cn0sIHsKICBvblN0YXR1czogcyA9PiB7IGJhY2t1cC5zdGF0dXMgPSBzOyBpZiAoYmFja3VwLm1vZGUgIT09ICd2MScpIHsgYmFja3VwLm1vZGUgPSAndjEnOyBlbCgnb3Blbi1iYWNrdXAnKS5oaWRkZW4gPSBmYWxzZTsgfSByZW5kZXJCYWNrdXAoKTsgfSwKICBvblJlc3VtZTogKCkgPT4geyBpZiAoYmFja3VwLm1vZGUgPT09ICd2MScpIGJhcGkuc3RhdHVzKCkuY2F0Y2goKCkgPT4ge30pOyB9LAp9KTsKd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoJ21lc3NhZ2UnLCBldiA9PiB7IGlmIChldi5vcmlnaW4gPT09IGxvY2F0aW9uLm9yaWdpbiAmJiBldi5zb3VyY2UgPT09IHdpbmRvdy5wYXJlbnQpIGJhcGkub25NZXNzYWdlKGV2LmRhdGEpOyB9KTsKCmZ1bmN0aW9uIHJlbmRlckJhY2t1cCgpIHsKICBjb25zdCByb290ID0gZWwoJ2JhY2t1cC1yb290Jyk7CiAgaWYgKGRvY3VtZW50LmFjdGl2ZUVsZW1lbnQgJiYgZG9jdW1lbnQuYWN0aXZlRWxlbWVudC5tYXRjaGVzICYmIGRvY3VtZW50LmFjdGl2ZUVsZW1lbnQubWF0Y2hlcygnW2RhdGEtcGgtc2VydmVyXScpKSB7CiAgICByb290LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLXBoLXN0YXR1c10nKS5mb3JFYWNoKHggPT4geyB4LnRleHRDb250ZW50ID0gYmFja3VwLmVycm9yIHx8IFAuc3VtbWFyeShiYWNrdXAuc3RhdHVzKTsgfSk7IHJldHVybjsKICB9CiAgcm9vdC5pbm5lckhUTUwgPSBiYWNrdXAubW9kZSA9PT0gJ3YxJyA/IFAuaHRtbChiYWNrdXApIDogJzxwIGNsYXNzPSJwaC1zdGF0dXMiPkNoZWNraW5n4oCmPC9wPic7CiAgaWYgKGJhY2t1cC5zdGF0dXMpIGVsKCdvcGVuLWJhY2t1cCcpLnRleHRDb250ZW50ID0gYmFja3VwLnN0YXR1cy5lbmFibGVkID8gJ1Bob25lIGJhY2t1cCDCtyBvbicgOiAnUGhvbmUgYmFja3VwJzsKfQpmdW5jdGlvbiBiYWNrdXBUYXAod2hhdCwgZXh0cmEgPSB7fSkgewogIGJhY2t1cC5lcnJvciA9ICcnOwogIGNvbnN0IHBlbmRpbmcgPSBQLmFjdChiYWNrdXAsIGJhcGksIHdoYXQsIGV4dHJhKTsKICBpZiAoIXBlbmRpbmcpIHsgcmVuZGVyQmFja3VwKCk7IHJldHVybjsgfQogIGlmICh3aGF0ICE9PSAnYWxidW1zJyAmJiB3aGF0ICE9PSAnYWxidW0nKSBiYWNrdXAuYnVzeSA9IHdoYXQgPT09ICdydW4nID8gJ3J1bicgOiB0cnVlOwogIHJlbmRlckJhY2t1cCgpOwogIHBlbmRpbmcuY2F0Y2goZSA9PiB7IGJhY2t1cC5lcnJvciA9IGUudGltZW91dCA/ICdUaGUgV2l6YXJkIE9TIGFwcCBkaWQgbm90IGFuc3dlciDCtyB0cnkgYWdhaW4nIDogZS5tZXNzYWdlOyB9KQogICAgLmZpbmFsbHkoKCkgPT4geyBiYWNrdXAuYnVzeSA9IGZhbHNlOyByZW5kZXJCYWNrdXAoKTsgfSk7Cn0KZWwoJ2JhY2t1cC1yb290JykuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLCBlID0+IHsKICBjb25zdCBiID0gZS50YXJnZXQuY2xvc2VzdCgnW2RhdGEtcGhdLCBbZGF0YS1waC1hbGJ1bV0nKTsgaWYgKCFiIHx8IGIuZGlzYWJsZWQpIHJldHVybjsKICBpZiAoYi5kYXRhc2V0LnBoQWxidW0gIT0gbnVsbCkgeyBiYWNrdXBUYXAoJ2FsYnVtJywgeyBpZDogYi5kYXRhc2V0LnBoQWxidW0gfSk7IHJldHVybjsgfQogIGNvbnN0IGZpZWxkID0gZWwoJ2JhY2t1cC1yb290JykucXVlcnlTZWxlY3RvcignW2RhdGEtcGgtc2VydmVyXScpOwogIGJhY2t1cFRhcChiLmRhdGFzZXQucGgsIGZpZWxkICYmIGZpZWxkLnZhbHVlID8geyBzZXJ2ZXI6IGZpZWxkLnZhbHVlIH0gOiB7fSk7Cn0pOwplbCgnYmFja3VwLXJvb3QnKS5hZGRFdmVudExpc3RlbmVyKCdpbnB1dCcsIGUgPT4geyBpZiAoZS50YXJnZXQubWF0Y2hlcygnW2RhdGEtcGgtc2VydmVyXScpKSBiYWNrdXAuc2VydmVyID0gZS50YXJnZXQudmFsdWU7IH0pOwplbCgnb3Blbi1iYWNrdXAnKS5vbmNsaWNrID0gKCkgPT4geyBXelNoZWV0Lm9wZW4oZWwoJ2JhY2t1cC1zaGVldCcpKTsgcmVuZGVyQmFja3VwKCk7IGJhcGkuc3RhdHVzKCkuY2F0Y2goKCkgPT4ge30pOyB9OwplbCgnYmFja3VwLXNoZWV0JykuYWRkRXZlbnRMaXN0ZW5lcignY2xvc2UnLCAoKSA9PiB7IGJhY2t1cC5hbGJ1bXNPcGVuID0gZmFsc2U7IGJhY2t1cC5jb25maXJtRGlzY29ubmVjdCA9IGZhbHNlOyB9KTsKUC5kZXRlY3QoYmFwaSkudGhlbihtb2RlID0+IHsKICBpZiAobW9kZSAhPT0gJ3YxJyAmJiBtb2RlICE9PSAndjEtZXJyb3InKSByZXR1cm47IC8vIGJyb3dzZXIsIGRlc2t0b3Agb3IgYW4gb2xkIHBob25lIGFwcDogSW1taWNoIHBob25lIGFwcCBpbnN0cnVjdGlvbnMgc3RheSBpbiBBZ2VudAogIGJhY2t1cC5tb2RlID0gJ3YxJzsKICBpZiAobW9kZSA9PT0gJ3YxLWVycm9yJykgYmFja3VwLmVycm9yID0gJ0NvdWxkIG5vdCByZWFkIGJhY2t1cCBzdGF0dXMgwrcgdHJ5IGFnYWluJzsKICBlbCgnb3Blbi1iYWNrdXAnKS5oaWRkZW4gPSBmYWxzZTsKICByZW5kZXJCYWNrdXAoKTsKfSk7Cg==', 'styles.css': 'LyogUGhvdG9zLW9ubHkgbGF5b3V0LiBFdmVyeXRoaW5nIHNoYXJlZCAodHlwZSwgcm93cywgc2hlZXRzLCBjb2xvdXJzKSBjb21lcyBmcm9tIHdpemFyZC11aS5jc3MuICovCi5waC1hcHAgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBoZWlnaHQ6IDEwMGR2aDsgbWluLWhlaWdodDogMDsgfQoucGgtaGVhZCB7IGZsZXg6IG5vbmU7IH0KLnBoLWhlYWQgcCB7IG1hcmdpbjogMnB4IDAgOHB4OyB9Ci5waC1hY3Rpb25zIHsgZGlzcGxheTogZmxleDsgZmxleC13cmFwOiB3cmFwOyBnYXA6IDhweDsgfQoucGgtYWN0aW9ucyBhLnd6LWNoaXAgeyB0ZXh0LWRlY29yYXRpb246IG5vbmU7IH0KLnBoLWFjdGlvbnMgW2hpZGRlbl0sIC5waC1tZW51IFtoaWRkZW5dIHsgZGlzcGxheTogbm9uZTsgfSAvKiAud3otY2hpcCdzIGRpc3BsYXkgd291bGQgb3RoZXJ3aXNlIGJlYXQgW2hpZGRlbl0gKi8KLnBoLXNldHVwIHsgbWFyZ2luOiB2YXIoLS13ei1zcGFjZS0yKSB2YXIoLS13ei1zcGFjZS0zKTsgfQoucGgtc2V0dXAgaDIgeyBtYXJnaW46IDAgMCA2cHg7IH0KLnBoLWZyYW1lLXdyYXAgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBtYXJnaW46IDhweCAwIDA7IGJvcmRlci10b3A6IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyB9Ci8qIFBob25lIGFuZCBvdGhlciBuYXJyb3cgd2lkdGhzOiBubyBQaG90b3MgaGVhZGVyIG92ZXIgSW1taWNoJ3Mgb3duIHRvcCBiYXIuIEltbWljaCBmaWxscyB0aGUgYXBwIGFyZWEgdW5kZXIgdGhlCiAqIHNoZWxsJ3MgY3V0b3V0OyB0aGUgYWN0aW9ucyBsaXZlIGJlaGluZCBvbmUgcXVpZXQg4ouvIGJ1dHRvbiAoaW4gdGhlIGdhcCBiZXR3ZWVuIEltbWljaCdzIGxvZ28gYW5kIGl0cyBzZWFyY2gKICogYnV0dG9uKSB0aGF0IG9wZW5zIHRoZSBtZW51IHNoZWV0LiBXaWRlIGxheW91dHMga2VlcCB0aGUgaGVhZGVyLiAqLwoucGgtYXBwIHsgcG9zaXRpb246IHJlbGF0aXZlOyB9Ci5waC1tb3JlIHsgZGlzcGxheTogbm9uZTsgfQoucGgtbWVudSBhLnd6LXJvdyB7IHRleHQtZGVjb3JhdGlvbjogbm9uZTsgY29sb3I6IGluaGVyaXQ7IH0KQGNvbnRhaW5lciB3ei1hcHAgKG1heC13aWR0aDogNTk5cHgpIHsKICAucGgtaGVhZCB7IGRpc3BsYXk6IG5vbmU7IH0KICAucGgtZnJhbWUtd3JhcCB7IG1hcmdpbjogMDsgYm9yZGVyLXRvcDogMDsgcGFkZGluZy10b3A6IHZhcigtLXdvcy1jdXRvdXQsIDBweCk7IH0KICAucGgtc2V0dXAgeyBtYXJnaW4tdG9wOiBjYWxjKHZhcigtLXdvcy1jdXRvdXQsIDBweCkgKyA2MHB4KTsgfQogIC5waC1tb3JlIHsgZGlzcGxheTogZ3JpZDsgcGxhY2UtaXRlbXM6IGNlbnRlcjsgcG9zaXRpb246IGFic29sdXRlOyB6LWluZGV4OiAyOyB0b3A6IGNhbGModmFyKC0td29zLWN1dG91dCwgMHB4KSArIDEwcHgpOyBsZWZ0OiAxMTBweDsKICAgIHdpZHRoOiB2YXIoLS13ei10YXJnZXQsIDQ0cHgpOyBoZWlnaHQ6IHZhcigtLXd6LXRhcmdldCwgNDRweCk7IHBhZGRpbmc6IDA7IGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogNTAlOyBiYWNrZ3JvdW5kOiBub25lOwogICAgY29sb3I6IHZhcigtLWluay0zLCBjdXJyZW50Q29sb3IpOyBvcGFjaXR5OiAuNzU7IC13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjogdHJhbnNwYXJlbnQ7IH0KICAucGgtbW9yZTo6YmVmb3JlIHsgY29udGVudDogIiI7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgaW5zZXQ6IDdweDsgYm9yZGVyLXJhZGl1czogNTAlOyBiYWNrZ3JvdW5kOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otYmFzZSkgMzUlLCB0cmFuc3BhcmVudCk7IH0KICAucGgtbW9yZSBzdmcgeyBwb3NpdGlvbjogcmVsYXRpdmU7IHdpZHRoOiAyMHB4OyBoZWlnaHQ6IDIwcHg7IGZpbGw6IGN1cnJlbnRDb2xvcjsgfQogIC5waC1tb3JlOmFjdGl2ZSwgLnBoLW1vcmU6Zm9jdXMtdmlzaWJsZSB7IG9wYWNpdHk6IDE7IH0KICAvKiBObyBsaWJyYXJ5IHlldCAoc2V0dXAgY2FyZHMpOiB0aGUgYnV0dG9uIHNpdHMgdG9wIHJpZ2h0LCBhYm92ZSB0aGUgY2FyZC4gKi8KICAucGgtYXBwOmhhcygjZnJhbWUtd3JhcFtoaWRkZW5dKSAucGgtbW9yZSB7IGxlZnQ6IGF1dG87IHJpZ2h0OiA4cHg7IH0KfQoucGgtZnJhbWUtd3JhcCBpZnJhbWUgeyBkaXNwbGF5OiBibG9jazsgd2lkdGg6IDEwMCU7IGhlaWdodDogMTAwJTsgYm9yZGVyOiAwOyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1jYW52YXMpOyB9Ci5waC1zdGVwcyB7IG1hcmdpbjogMCAwIDEycHg7IHBhZGRpbmctbGVmdDogMS4yZW07IGxpbmUtaGVpZ2h0OiAxLjQ1OyB9Ci5waC1zdGVwcyBsaSB7IG1hcmdpbjogMCAwIDZweDsgfQoucGgtaGVscCB7IGxpbmUtaGVpZ2h0OiAxLjQ1OyBtYXJnaW46IDAgMCAxMHB4OyB9Ci5waC1pbXBvcnQtc3RhdGUgeyBtYXJnaW46IDAgMCAxMHB4OyBmb250LXdlaWdodDogNjAwOyB9Ci5waC1sb2cgeyBtYXgtaGVpZ2h0OiAzMHZoOyBvdmVyZmxvdzogYXV0bzsgZm9udC1mYW1pbHk6IHZhcigtLXd6LWZvbnQtbW9ubyk7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IHdoaXRlLXNwYWNlOiBwcmUtd3JhcDsgbWFyZ2luOiAxMHB4IDAgMDsgfQoKLyogUGhvbmUgYmFja3VwIHNoZWV0IChyb3dzIGFuZCBzd2l0Y2hlcyBmcm9tIHRoZSBmb3JtZXIgdmlldy1vbmx5IFBob3RvcyBhcHA7IGtpdCB2YXJpYWJsZXMgb25seSkuICovCiNiYWNrdXAtcm9vdCAucGggeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9Ci5waC1pbnRybywgLnBoLW5vdGUsIC5waC1zZXJ2ZXIgeyBtYXJnaW46IDAgNHB4IDEwcHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGxpbmUtaGVpZ2h0OiAxLjQ7IGNvbG9yOiB2YXIoLS13ei10ZXh0LXNlY29uZGFyeSk7IH0KLnBoLXNlcnZlciB7IG1hcmdpbi10b3A6IDEycHg7IGNvbG9yOiB2YXIoLS13ei10ZXh0LW11dGVkKTsgb3ZlcmZsb3ctd3JhcDogYW55d2hlcmU7IH0KLnBoLXN0YXR1cyB7IG1hcmdpbjogMTBweCA0cHggOHB4OyBjb2xvcjogdmFyKC0td3otdGV4dCk7IH0KLnBoLXJvdyB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTJweDsgd2lkdGg6IDEwMCU7IG1pbi1oZWlnaHQ6IDU2cHg7IHBhZGRpbmc6IDEwcHggNHB4OyBib3JkZXI6IDA7IGJvcmRlci1ib3R0b206IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyBiYWNrZ3JvdW5kOiB0cmFuc3BhcmVudDsgY29sb3I6IHZhcigtLXd6LXRleHQpOyB0ZXh0LWFsaWduOiBsZWZ0OyBmb250OiBpbmhlcml0OyB9Ci5waC1yb3c6ZGlzYWJsZWQsIC5waC1idG46ZGlzYWJsZWQgeyBvcGFjaXR5OiAuNTU7IH0KLnBoLXR4IHsgZmxleDogMTsgbWluLXdpZHRoOiAwOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9Ci5waC10eCBiIHsgZm9udC13ZWlnaHQ6IDYwMDsgfQoucGgtdHggc21hbGwgeyBtYXJnaW4tdG9wOiAycHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGNvbG9yOiB2YXIoLS13ei10ZXh0LW11dGVkKTsgfQoucGgtZ28geyBjb2xvcjogdmFyKC0td3otdGV4dC1tdXRlZCk7IH0KLnN3IHsgZmxleDogMCAwIDQ0cHg7IGhlaWdodDogMjZweDsgYm9yZGVyLXJhZGl1czogMTNweDsgYmFja2dyb3VuZDogdmFyKC0td3otYm9yZGVyKTsgcG9zaXRpb246IHJlbGF0aXZlOyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIC4xNXM7IH0KLnN3IGkgeyBwb3NpdGlvbjogYWJzb2x1dGU7IHRvcDogM3B4OyBsZWZ0OiAzcHg7IHdpZHRoOiAyMHB4OyBoZWlnaHQ6IDIwcHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogdmFyKC0td3otdGV4dCk7IHRyYW5zaXRpb246IHRyYW5zZm9ybSAuMTVzOyB9Ci5zdy5vbiB7IGJhY2tncm91bmQ6IHZhcigtLXd6LWFjY2VudCk7IH0gLnN3Lm9uIGkgeyB0cmFuc2Zvcm06IHRyYW5zbGF0ZVgoMThweCk7IGJhY2tncm91bmQ6IHZhcigtLXd6LWJhc2UpOyB9Ci5waC1maWVsZCB7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogNnB4OyBwYWRkaW5nOiAxMnB4IDRweDsgYm9yZGVyLWJvdHRvbTogMXB4IHNvbGlkIHZhcigtLXd6LWJvcmRlcik7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGNvbG9yOiB2YXIoLS13ei10ZXh0LXNlY29uZGFyeSk7IH0KLnBoLWZpZWxkIGlucHV0IHsgbWluLWhlaWdodDogNDRweDsgcGFkZGluZzogMCAxMnB4OyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyBib3JkZXItcmFkaXVzOiAxMHB4OyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1zdXJmYWNlKTsgY29sb3I6IHZhcigtLXd6LXRleHQpOyBmb250OiBpbmhlcml0OyB9Ci5waC1hY3RzIHsgZGlzcGxheTogZmxleDsgZmxleC13cmFwOiB3cmFwOyBnYXA6IDhweDsgbWFyZ2luLXRvcDogMTRweDsgfQoucGgtYnRuIHsgcGFkZGluZzogMTBweCAxNnB4OyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS13ei1ib3JkZXIpOyBib3JkZXItcmFkaXVzOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1zdXJmYWNlKTsgY29sb3I6IHZhcigtLXd6LXRleHQpOyBmb250OiBpbmhlcml0OyBmb250LXdlaWdodDogNjAwOyB9Ci5waC1idG4ucHJpbWFyeSB7IGFsaWduLXNlbGY6IGZsZXgtc3RhcnQ7IG1hcmdpbjogMCA0cHggMTBweDsgYmFja2dyb3VuZDogdmFyKC0td3otYWNjZW50KTsgYm9yZGVyLWNvbG9yOiB2YXIoLS13ei1hY2NlbnQpOyBjb2xvcjogdmFyKC0td3otYmFzZSk7IH0KLnBoLWJ0bi5naG9zdCB7IGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OyBjb2xvcjogdmFyKC0td3otdGV4dC1zZWNvbmRhcnkpOyB9Ci5waC1idG4uZGFuZ2VyIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS13ei13YXJuaW5nKTsgY29sb3I6IHZhcigtLXd6LXdhcm5pbmcpOyBiYWNrZ3JvdW5kOiB0cmFuc3BhcmVudDsgfQoucGgtbGluayB7IHBhZGRpbmc6IDA7IGJvcmRlcjogMDsgYmFja2dyb3VuZDogdHJhbnNwYXJlbnQ7IGNvbG9yOiB2YXIoLS13ei1hY2NlbnQpOyB0ZXh0LWRlY29yYXRpb246IHVuZGVybGluZTsgZm9udDogaW5oZXJpdDsgfQoucGgtYWxidW1zIHsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgcGFkZGluZzogMnB4IDAgNnB4IDRweDsgYm9yZGVyLWJvdHRvbTogMXB4IHNvbGlkIHZhcigtLXd6LWJvcmRlcik7IH0KLnBoLWFsYnVtIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBwYWRkaW5nOiA4cHggNHB4OyBib3JkZXI6IDA7IGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OyBjb2xvcjogdmFyKC0td3otdGV4dCk7IHRleHQtYWxpZ246IGxlZnQ7IGZvbnQ6IGluaGVyaXQ7IH0KLnBoLWNoZWNrIHsgZmxleDogMCAwIDIwcHg7IGhlaWdodDogMjBweDsgYm9yZGVyLXJhZGl1czogNnB4OyBib3JkZXI6IDEuNXB4IHNvbGlkIHZhcigtLXd6LXRleHQtbXV0ZWQpOyB9Ci5waC1hbGJ1bVthcmlhLWNoZWNrZWQ9InRydWUiXSAucGgtY2hlY2sgeyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1hY2NlbnQpOyBib3JkZXItY29sb3I6IHZhcigtLXd6LWFjY2VudCk7IGJveC1zaGFkb3c6IGluc2V0IDAgMCAwIDNweCB2YXIoLS13ei1iYXNlKTsgfQoucGgtcXVldWVkIHsgbWFyZ2luLXRvcDogMnB4OyBwYWRkaW5nOiAxMHB4IDEycHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGJhY2tncm91bmQ6IHZhcigtLXd6LXN1cmZhY2UpOyBjb2xvcjogdmFyKC0td3otdGV4dCk7IH0K', 'boot.js': 'LyogUnVucyBiZWZvcmUgZmlyc3QgcGFpbnQ6IGEgaG9zdGVkIGFwcCBuZXZlciBmbGFzaGVzIGEgc3RhbmRhbG9uZSBjYW52YXMgd2hpbGUgdGhlIHNoZWxsJ3MKICogYXBwZWFyYW5jZSBtZXNzYWdlIGlzIG9uIGl0cyB3YXkuICovCnRyeSB7IGlmICh3aW5kb3cucGFyZW50ICE9PSB3aW5kb3cpIGRvY3VtZW50LmRvY3VtZW50RWxlbWVudC5jbGFzc0xpc3QuYWRkKCd3ei1ob3N0ZWQnKTsgfQpjYXRjaCAoZSkgeyBkb2N1bWVudC5kb2N1bWVudEVsZW1lbnQuY2xhc3NMaXN0LmFkZCgnd3otaG9zdGVkJyk7IH0K', 'wizard-tokens.css': 'LyogR2VuZXJhdGVkIGJ5IHN0eWxlL2dlbmVyYXRlX3Rva2Vucy5weSBmcm9tIHRva2Vucy5qc29uLiBEbyBub3QgZWRpdC4gKi8KOnJvb3QgewogIC0td3otZm9udC11aTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LXJlYWRpbmc6IEdlb3JnaWEsIHNlcmlmOwogIC0td3otZm9udC1kaXNwbGF5OiB1aS1zYW5zLXNlcmlmLCBzeXN0ZW0tdWksIC1hcHBsZS1zeXN0ZW0sICJTZWdvZSBVSSIsIFJvYm90bywgIkhlbHZldGljYSBOZXVlIiwgc2Fucy1zZXJpZjsKICAtLXd6LWZvbnQtbW9ubzogdWktbW9ub3NwYWNlLCAiU0YgTW9ubyIsIE1lbmxvLCBtb25vc3BhY2U7CiAgLS13ei1mb250LXNpemU6IDE1cHg7CiAgLS13ei1saW5lLWhlaWdodDogMS40OwogIC0td3otc3BhY2UtMTogNHB4OwogIC0td3otc3BhY2UtMjogOHB4OwogIC0td3otc3BhY2UtMzogMTJweDsKICAtLXd6LXNwYWNlLTQ6IDE2cHg7CiAgLS13ei1zcGFjZS01OiAyNHB4OwogIC0td3otcmFkaXVzOiAxNHB4OwogIC0td3otcmFkaXVzLXRpbGU6IDEycHg7CiAgLS13ei1yYWRpdXMtcGlsbDogMjBweDsKICAtLXd6LW1vdGlvbi1mYXN0OiAxNDBtczsKICAtLXd6LW1vdGlvbi1tZWRpdW06IDIyMG1zOwogIC0td3otZWFzZTogY3ViaWMtYmV6aWVyKC4yLCAuOSwgLjI1LCAxKTsKICAtLXd6LWRhbmdlcjogI2ZmNmI4NjsKICAtLXd6LXN1Y2Nlc3M6ICM0ZmYwYjA7CiAgLS13ei13YXJuaW5nOiAjZmZiZTU1OwogIC0td3otc3VjY2Vzcy1saWdodDogIzEwNmEzZjsKICAtLXd6LXdhcm5pbmctbGlnaHQ6ICM5YTQwMDY7CiAgLS13ei10ZXh0LXNtOiAxM3B4OwogIC0td3otdGV4dC1tZDogMTRweDsKICAtLXd6LXRleHQtbGc6IDE2cHg7CiAgLS13ei10ZXh0LWhlYWRpbmc6IDE3cHg7CiAgLS13ei10ZXh0LXRpdGxlOiAyNnB4OwogIC0td3otdGFyZ2V0OiA0NHB4OwogIC0td3otaWNvbi1zaXplOiAxOHB4OwogIC0td3otcGFuZWw6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei10ZXh0KSAxMCUsIHZhcigtLXd6LWJhc2UpKTsKICAtLXd6LWNhbnZhczogdmFyKC0td3otYmFzZSk7CiAgLS13ei1iYXNlOiAjMDUwMzBjOwogIC0td3otY2hyb21lOiByZ2JhKDEyLDgsMjYsLjgyKTsKICAtLXd6LW92ZXJsYXk6IHJnYmEoMjgsMTksNTgsLjcyKTsKICAtLXd6LXN1cmZhY2U6IGNvbG9yLW1peChpbiBzcmdiLCAjZjNlZmZmIDYlLCB0cmFuc3BhcmVudCk7CiAgLS13ei1zdXJmYWNlLXJhaXNlZDogY29sb3ItbWl4KGluIHNyZ2IsICNmM2VmZmYgMTAlLCB0cmFuc3BhcmVudCk7CiAgLS13ei10ZXh0OiAjZjNlZmZmOwogIC0td3otdGV4dC1zZWNvbmRhcnk6ICNiOWIwZDg7CiAgLS13ei10ZXh0LW11dGVkOiAjN2Q3NGEzOwogIC0td3otYm9yZGVyOiByZ2JhKDIwNSwxOTAsMjU1LC4yNCk7CiAgLS13ei1hY2NlbnQ6ICNiOTliZmY7CiAgLS13ei1hY2NlbnQtc29mdDogY29sb3ItbWl4KGluIHNyZ2IsICNiOTliZmYgMTYlLCB0cmFuc3BhcmVudCk7CiAgLS13ei1vbi1hY2NlbnQ6ICMwNTAzMGM7CiAgLS13ei1yZWFkZXItc3VyZmFjZTogdHJhbnNwYXJlbnQ7Cn0KOnJvb3QgeyBjb2xvci1zY2hlbWU6IGRhcms7IH0KaHRtbFtkYXRhLXNraW49Im5lYnVsYSJdIHsKICAtLXd6LWZvbnQtdWk6IHVpLXNhbnMtc2VyaWYsIHN5c3RlbS11aSwgLWFwcGxlLXN5c3RlbSwgIlNlZ29lIFVJIiwgUm9ib3RvLCAiSGVsdmV0aWNhIE5ldWUiLCBzYW5zLXNlcmlmOwogIC0td3otZm9udC1yZWFkaW5nOiBHZW9yZ2lhLCBzZXJpZjsKICAtLXd6LWZvbnQtZGlzcGxheTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LW1vbm86IHVpLW1vbm9zcGFjZSwgIlNGIE1vbm8iLCBNZW5sbywgbW9ub3NwYWNlOwogIC0td3otZm9udC1zaXplOiAxNXB4OwogIC0td3otbGluZS1oZWlnaHQ6IDEuNDsKICAtLXd6LXNwYWNlLTE6IDRweDsKICAtLXd6LXNwYWNlLTI6IDhweDsKICAtLXd6LXNwYWNlLTM6IDEycHg7CiAgLS13ei1zcGFjZS00OiAxNnB4OwogIC0td3otc3BhY2UtNTogMjRweDsKICAtLXd6LXJhZGl1czogMTRweDsKICAtLXd6LXJhZGl1cy10aWxlOiAxMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDIwcHg7CiAgLS13ei1tb3Rpb24tZmFzdDogMTQwbXM7CiAgLS13ei1tb3Rpb24tbWVkaXVtOiAyMjBtczsKICAtLXd6LWVhc2U6IGN1YmljLWJlemllciguMiwgLjksIC4yNSwgMSk7CiAgLS13ei1kYW5nZXI6ICNmZjZiODY7CiAgLS13ei1zdWNjZXNzOiAjNGZmMGIwOwogIC0td3otd2FybmluZzogI2ZmYmU1NTsKICAtLXd6LXN1Y2Nlc3MtbGlnaHQ6ICMxMDZhM2Y7CiAgLS13ei13YXJuaW5nLWxpZ2h0OiAjOWE0MDA2OwogIC0td3otdGV4dC1zbTogMTNweDsKICAtLXd6LXRleHQtbWQ6IDE0cHg7CiAgLS13ei10ZXh0LWxnOiAxNnB4OwogIC0td3otdGV4dC1oZWFkaW5nOiAxN3B4OwogIC0td3otdGV4dC10aXRsZTogMjZweDsKICAtLXd6LXRhcmdldDogNDRweDsKICAtLXd6LWljb24tc2l6ZTogMThweDsKICAtLXd6LXBhbmVsOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otdGV4dCkgMTAlLCB2YXIoLS13ei1iYXNlKSk7CiAgLS13ei1jYW52YXM6IHZhcigtLXd6LWJhc2UpOwogIC0td3otYmFzZTogIzA1MDMwYzsKICAtLXd6LWNocm9tZTogcmdiYSgxMiw4LDI2LC44Mik7CiAgLS13ei1vdmVybGF5OiByZ2JhKDI4LDE5LDU4LC43Mik7CiAgLS13ei1zdXJmYWNlOiBjb2xvci1taXgoaW4gc3JnYiwgI2YzZWZmZiA2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otc3VyZmFjZS1yYWlzZWQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZjNlZmZmIDEwJSwgdHJhbnNwYXJlbnQpOwogIC0td3otdGV4dDogI2YzZWZmZjsKICAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjYjliMGQ4OwogIC0td3otdGV4dC1tdXRlZDogIzdkNzRhMzsKICAtLXd6LWJvcmRlcjogcmdiYSgyMDUsMTkwLDI1NSwuMjQpOwogIC0td3otYWNjZW50OiAjYjk5YmZmOwogIC0td3otYWNjZW50LXNvZnQ6IGNvbG9yLW1peChpbiBzcmdiLCAjYjk5YmZmIDE2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otb24tYWNjZW50OiAjMDUwMzBjOwogIC0td3otcmVhZGVyLXN1cmZhY2U6IHRyYW5zcGFyZW50Owp9Cmh0bWxbZGF0YS1za2luPSJncm92ZSJdIHsKICAtLXd6LWZvbnQtdWk6IHVpLXNhbnMtc2VyaWYsIHN5c3RlbS11aSwgLWFwcGxlLXN5c3RlbSwgIlNlZ29lIFVJIiwgUm9ib3RvLCAiSGVsdmV0aWNhIE5ldWUiLCBzYW5zLXNlcmlmOwogIC0td3otZm9udC1yZWFkaW5nOiBHZW9yZ2lhLCBzZXJpZjsKICAtLXd6LWZvbnQtZGlzcGxheTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LW1vbm86IHVpLW1vbm9zcGFjZSwgIlNGIE1vbm8iLCBNZW5sbywgbW9ub3NwYWNlOwogIC0td3otZm9udC1zaXplOiAxNXB4OwogIC0td3otbGluZS1oZWlnaHQ6IDEuNDsKICAtLXd6LXNwYWNlLTE6IDRweDsKICAtLXd6LXNwYWNlLTI6IDhweDsKICAtLXd6LXNwYWNlLTM6IDEycHg7CiAgLS13ei1zcGFjZS00OiAxNnB4OwogIC0td3otc3BhY2UtNTogMjRweDsKICAtLXd6LXJhZGl1czogMTRweDsKICAtLXd6LXJhZGl1cy10aWxlOiAxMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDIwcHg7CiAgLS13ei1tb3Rpb24tZmFzdDogMTQwbXM7CiAgLS13ei1tb3Rpb24tbWVkaXVtOiAyMjBtczsKICAtLXd6LWVhc2U6IGN1YmljLWJlemllciguMiwgLjksIC4yNSwgMSk7CiAgLS13ei1kYW5nZXI6ICNmZjZiODY7CiAgLS13ei1zdWNjZXNzOiAjOWJlMzdhOwogIC0td3otd2FybmluZzogI2ZmOWE2YjsKICAtLXd6LXN1Y2Nlc3MtbGlnaHQ6ICMxMDZhM2Y7CiAgLS13ei13YXJuaW5nLWxpZ2h0OiAjOWE0MDA2OwogIC0td3otdGV4dC1zbTogMTNweDsKICAtLXd6LXRleHQtbWQ6IDE0cHg7CiAgLS13ei10ZXh0LWxnOiAxNnB4OwogIC0td3otdGV4dC1oZWFkaW5nOiAxN3B4OwogIC0td3otdGV4dC10aXRsZTogMjZweDsKICAtLXd6LXRhcmdldDogNDRweDsKICAtLXd6LWljb24tc2l6ZTogMThweDsKICAtLXd6LXBhbmVsOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otdGV4dCkgMTAlLCB2YXIoLS13ei1iYXNlKSk7CiAgLS13ei1jYW52YXM6IHZhcigtLXd6LWJhc2UpOwogIC0td3otYmFzZTogIzA1MGIwNzsKICAtLXd6LWNocm9tZTogcmdiYSg4LDE1LDEwLC44NCk7CiAgLS13ei1vdmVybGF5OiByZ2JhKDE2LDMyLDIyLC43OCk7CiAgLS13ei1zdXJmYWNlOiBjb2xvci1taXgoaW4gc3JnYiwgI2VlZjVlOSA2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otc3VyZmFjZS1yYWlzZWQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZWVmNWU5IDEwJSwgdHJhbnNwYXJlbnQpOwogIC0td3otdGV4dDogI2VlZjVlOTsKICAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjYjVjYmI5OwogIC0td3otdGV4dC1tdXRlZDogIzdmOWE4NjsKICAtLXd6LWJvcmRlcjogcmdiYSgyMDcsMjMyLDIwMCwuMjYpOwogIC0td3otYWNjZW50OiAjZTZjNjZiOwogIC0td3otYWNjZW50LXNvZnQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZTZjNjZiIDE2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otb24tYWNjZW50OiAjMDUwYjA3OwogIC0td3otcmVhZGVyLXN1cmZhY2U6IHRyYW5zcGFyZW50Owp9Cmh0bWxbZGF0YS1za2luPSJ0aWRlIl0gewogIC0td3otZm9udC11aTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LXJlYWRpbmc6IEdlb3JnaWEsIHNlcmlmOwogIC0td3otZm9udC1kaXNwbGF5OiB1aS1zYW5zLXNlcmlmLCBzeXN0ZW0tdWksIC1hcHBsZS1zeXN0ZW0sICJTZWdvZSBVSSIsIFJvYm90bywgIkhlbHZldGljYSBOZXVlIiwgc2Fucy1zZXJpZjsKICAtLXd6LWZvbnQtbW9ubzogdWktbW9ub3NwYWNlLCAiU0YgTW9ubyIsIE1lbmxvLCBtb25vc3BhY2U7CiAgLS13ei1mb250LXNpemU6IDE1cHg7CiAgLS13ei1saW5lLWhlaWdodDogMS40OwogIC0td3otc3BhY2UtMTogNHB4OwogIC0td3otc3BhY2UtMjogOHB4OwogIC0td3otc3BhY2UtMzogMTJweDsKICAtLXd6LXNwYWNlLTQ6IDE2cHg7CiAgLS13ei1zcGFjZS01OiAyNHB4OwogIC0td3otcmFkaXVzOiAxNHB4OwogIC0td3otcmFkaXVzLXRpbGU6IDEycHg7CiAgLS13ei1yYWRpdXMtcGlsbDogMjBweDsKICAtLXd6LW1vdGlvbi1mYXN0OiAxNDBtczsKICAtLXd6LW1vdGlvbi1tZWRpdW06IDIyMG1zOwogIC0td3otZWFzZTogY3ViaWMtYmV6aWVyKC4yLCAuOSwgLjI1LCAxKTsKICAtLXd6LWRhbmdlcjogI2ZmNmI4NjsKICAtLXd6LXN1Y2Nlc3M6ICM4ZmUzYTg7CiAgLS13ei13YXJuaW5nOiAjZmZjMDcwOwogIC0td3otc3VjY2Vzcy1saWdodDogIzEwNmEzZjsKICAtLXd6LXdhcm5pbmctbGlnaHQ6ICM5YTQwMDY7CiAgLS13ei10ZXh0LXNtOiAxM3B4OwogIC0td3otdGV4dC1tZDogMTRweDsKICAtLXd6LXRleHQtbGc6IDE2cHg7CiAgLS13ei10ZXh0LWhlYWRpbmc6IDE3cHg7CiAgLS13ei10ZXh0LXRpdGxlOiAyNnB4OwogIC0td3otdGFyZ2V0OiA0NHB4OwogIC0td3otaWNvbi1zaXplOiAxOHB4OwogIC0td3otcGFuZWw6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei10ZXh0KSAxMCUsIHZhcigtLXd6LWJhc2UpKTsKICAtLXd6LWNhbnZhczogdmFyKC0td3otYmFzZSk7CiAgLS13ei1iYXNlOiAjMDUyYTM2OwogIC0td3otY2hyb21lOiByZ2JhKDQsMjYsMzQsLjgyKTsKICAtLXd6LW92ZXJsYXk6IHJnYmEoOCw1Niw3MiwuNyk7CiAgLS13ei1zdXJmYWNlOiBjb2xvci1taXgoaW4gc3JnYiwgI2U4ZjZmOCA2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otc3VyZmFjZS1yYWlzZWQ6IGNvbG9yLW1peChpbiBzcmdiLCAjZThmNmY4IDEwJSwgdHJhbnNwYXJlbnQpOwogIC0td3otdGV4dDogI2U4ZjZmODsKICAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjYTljZGQ0OwogIC0td3otdGV4dC1tdXRlZDogIzZmOThhMjsKICAtLXd6LWJvcmRlcjogcmdiYSgxNjgsMjMwLDIzOCwuMjcpOwogIC0td3otYWNjZW50OiAjN2ZkOGZmOwogIC0td3otYWNjZW50LXNvZnQ6IGNvbG9yLW1peChpbiBzcmdiLCAjN2ZkOGZmIDE2JSwgdHJhbnNwYXJlbnQpOwogIC0td3otb24tYWNjZW50OiAjMDUyYTM2OwogIC0td3otcmVhZGVyLXN1cmZhY2U6IHRyYW5zcGFyZW50Owp9Cmh0bWxbZGF0YS1za2luPSJlbWJlciJdIHsKICAtLXd6LWZvbnQtdWk6IHVpLXNhbnMtc2VyaWYsIHN5c3RlbS11aSwgLWFwcGxlLXN5c3RlbSwgIlNlZ29lIFVJIiwgUm9ib3RvLCAiSGVsdmV0aWNhIE5ldWUiLCBzYW5zLXNlcmlmOwogIC0td3otZm9udC1yZWFkaW5nOiBHZW9yZ2lhLCBzZXJpZjsKICAtLXd6LWZvbnQtZGlzcGxheTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LW1vbm86IHVpLW1vbm9zcGFjZSwgIlNGIE1vbm8iLCBNZW5sbywgbW9ub3NwYWNlOwogIC0td3otZm9udC1zaXplOiAxNXB4OwogIC0td3otbGluZS1oZWlnaHQ6IDEuNDsKICAtLXd6LXNwYWNlLTE6IDRweDsKICAtLXd6LXNwYWNlLTI6IDhweDsKICAtLXd6LXNwYWNlLTM6IDEycHg7CiAgLS13ei1zcGFjZS00OiAxNnB4OwogIC0td3otc3BhY2UtNTogMjRweDsKICAtLXd6LXJhZGl1czogMTRweDsKICAtLXd6LXJhZGl1cy10aWxlOiAxMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDIwcHg7CiAgLS13ei1tb3Rpb24tZmFzdDogMTQwbXM7CiAgLS13ei1tb3Rpb24tbWVkaXVtOiAyMjBtczsKICAtLXd6LWVhc2U6IGN1YmljLWJlemllciguMiwgLjksIC4yNSwgMSk7CiAgLS13ei1kYW5nZXI6ICNmZjZiODY7CiAgLS13ei1zdWNjZXNzOiAjOGZlM2E4OwogIC0td3otd2FybmluZzogI2Y1ZTA0YTsKICAtLXd6LXN1Y2Nlc3MtbGlnaHQ6ICMxMDZhM2Y7CiAgLS13ei13YXJuaW5nLWxpZ2h0OiAjOWE0MDA2OwogIC0td3otdGV4dC1zbTogMTNweDsKICAtLXd6LXRleHQtbWQ6IDE0cHg7CiAgLS13ei10ZXh0LWxnOiAxNnB4OwogIC0td3otdGV4dC1oZWFkaW5nOiAxN3B4OwogIC0td3otdGV4dC10aXRsZTogMjZweDsKICAtLXd6LXRhcmdldDogNDRweDsKICAtLXd6LWljb24tc2l6ZTogMThweDsKICAtLXd6LXBhbmVsOiBjb2xvci1taXgoaW4gc3JnYiwgdmFyKC0td3otdGV4dCkgMTAlLCB2YXIoLS13ei1iYXNlKSk7CiAgLS13ei1jYW52YXM6IHZhcigtLXd6LWJhc2UpOwogIC0td3otYmFzZTogIzA5MDUwNDsKICAtLXd6LWNocm9tZTogcmdiYSgxNiw5LDUsLjg1KTsKICAtLXd6LW92ZXJsYXk6IHJnYmEoMzYsMTksMTEsLjgpOwogIC0td3otc3VyZmFjZTogY29sb3ItbWl4KGluIHNyZ2IsICNmZmU5ZDYgNiUsIHRyYW5zcGFyZW50KTsKICAtLXd6LXN1cmZhY2UtcmFpc2VkOiBjb2xvci1taXgoaW4gc3JnYiwgI2ZmZTlkNiAxMCUsIHRyYW5zcGFyZW50KTsKICAtLXd6LXRleHQ6ICNmZmU5ZDY7CiAgLS13ei10ZXh0LXNlY29uZGFyeTogI2Q0YjQ5YzsKICAtLXd6LXRleHQtbXV0ZWQ6ICNhMDgwNmE7CiAgLS13ei1ib3JkZXI6IHJnYmEoMjU1LDE3NiwxMjIsLjI3KTsKICAtLXd6LWFjY2VudDogI2ZmYjQ3MDsKICAtLXd6LWFjY2VudC1zb2Z0OiBjb2xvci1taXgoaW4gc3JnYiwgI2ZmYjQ3MCAxNiUsIHRyYW5zcGFyZW50KTsKICAtLXd6LW9uLWFjY2VudDogIzA5MDUwNDsKICAtLXd6LXJlYWRlci1zdXJmYWNlOiB0cmFuc3BhcmVudDsKfQpodG1sW2RhdGEtc2tpbj0ic2lnbmFsIl0gewogIC0td3otZm9udC11aTogdWktc2Fucy1zZXJpZiwgc3lzdGVtLXVpLCAtYXBwbGUtc3lzdGVtLCAiU2Vnb2UgVUkiLCBSb2JvdG8sICJIZWx2ZXRpY2EgTmV1ZSIsIHNhbnMtc2VyaWY7CiAgLS13ei1mb250LXJlYWRpbmc6IEdlb3JnaWEsIHNlcmlmOwogIC0td3otZm9udC1kaXNwbGF5OiB1aS1zYW5zLXNlcmlmLCBzeXN0ZW0tdWksIC1hcHBsZS1zeXN0ZW0sICJTZWdvZSBVSSIsIFJvYm90bywgIkhlbHZldGljYSBOZXVlIiwgc2Fucy1zZXJpZjsKICAtLXd6LWZvbnQtbW9ubzogdWktbW9ub3NwYWNlLCAiU0YgTW9ubyIsIE1lbmxvLCBtb25vc3BhY2U7CiAgLS13ei1mb250LXNpemU6IDE1cHg7CiAgLS13ei1saW5lLWhlaWdodDogMS40OwogIC0td3otc3BhY2UtMTogNHB4OwogIC0td3otc3BhY2UtMjogOHB4OwogIC0td3otc3BhY2UtMzogMTJweDsKICAtLXd6LXNwYWNlLTQ6IDE2cHg7CiAgLS13ei1zcGFjZS01OiAyNHB4OwogIC0td3otcmFkaXVzOiAycHg7CiAgLS13ei1yYWRpdXMtdGlsZTogMnB4OwogIC0td3otcmFkaXVzLXBpbGw6IDJweDsKICAtLXd6LW1vdGlvbi1mYXN0OiAxNDBtczsKICAtLXd6LW1vdGlvbi1tZWRpdW06IDIyMG1zOwogIC0td3otZWFzZTogY3ViaWMtYmV6aWVyKC4yLCAuOSwgLjI1LCAxKTsKICAtLXd6LWRhbmdlcjogI2ZmNmI4NjsKICAtLXd6LXN1Y2Nlc3M6ICMzOWZmOGM7CiAgLS13ei13YXJuaW5nOiAjZmZmMDRkOwogIC0td3otc3VjY2Vzcy1saWdodDogIzEwNmEzZjsKICAtLXd6LXdhcm5pbmctbGlnaHQ6ICM5YTQwMDY7CiAgLS13ei10ZXh0LXNtOiAxM3B4OwogIC0td3otdGV4dC1tZDogMTRweDsKICAtLXd6LXRleHQtbGc6IDE2cHg7CiAgLS13ei10ZXh0LWhlYWRpbmc6IDE3cHg7CiAgLS13ei10ZXh0LXRpdGxlOiAyNnB4OwogIC0td3otdGFyZ2V0OiA0NHB4OwogIC0td3otaWNvbi1zaXplOiAxOHB4OwogIC0td3otcGFuZWw6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei10ZXh0KSAxMCUsIHZhcigtLXd6LWJhc2UpKTsKICAtLXd6LWNhbnZhczogdmFyKC0td3otYmFzZSk7CiAgLS13ei1iYXNlOiAjMDAwMDAwOwogIC0td3otY2hyb21lOiByZ2JhKDAsMTAsNCwuODYpOwogIC0td3otb3ZlcmxheTogcmdiYSgwLDIwLDgsLjc2KTsKICAtLXd6LXN1cmZhY2U6IGNvbG9yLW1peChpbiBzcmdiLCAjZDhmZmQ4IDYlLCB0cmFuc3BhcmVudCk7CiAgLS13ei1zdXJmYWNlLXJhaXNlZDogY29sb3ItbWl4KGluIHNyZ2IsICNkOGZmZDggMTAlLCB0cmFuc3BhcmVudCk7CiAgLS13ei10ZXh0OiAjZDhmZmQ4OwogIC0td3otdGV4dC1zZWNvbmRhcnk6ICM4ZmM3OWE7CiAgLS13ei10ZXh0LW11dGVkOiAjNGY4YTVhOwogIC0td3otYm9yZGVyOiByZ2JhKDU3LDI1NSwxNDAsLjMyKTsKICAtLXd6LWFjY2VudDogI2ZmYmU1NTsKICAtLXd6LWFjY2VudC1zb2Z0OiBjb2xvci1taXgoaW4gc3JnYiwgI2ZmYmU1NSAxNiUsIHRyYW5zcGFyZW50KTsKICAtLXd6LW9uLWFjY2VudDogIzAwMDAwMDsKICAtLXd6LXJlYWRlci1zdXJmYWNlOiB0cmFuc3BhcmVudDsKfQpodG1sLnd6LWhvc3RlZCwgaHRtbFtkYXRhLXd6LWhvc3RlZF0geyAtLXd6LWNhbnZhczogdHJhbnNwYXJlbnQ7IH0K', 'wizard-ui.css': 'LyogV2l6YXJkIFVJIGtpdCBmb3IgZ2VuZXJhdGVkIGFwcHMgKHN0eWxlLyBjYW5vbmljYWwgc291cmNlLCB2MSkuCiAqCiAqIFJ1bGUgMTogYSBob3N0ZWQgYXBwIHBhaW50cyBOTyBjYW52YXMuIEluIGEgd2luZG93IG9yIGZ1bGxzY3JlZW4gdGhlIHNoZWxsJ3Mgd2luZG93IGdsYXNzIGlzIHRoZQogKiAgICAgICAgIGNhbnZhcywgZXhhY3RseSBhcyBpdCBpcyBmb3IgQ2hhdHMuIE9ubHkgYSBzdGFuZGFsb25lIHRhYiBwYWludHMgdGhlIHNraW4ncyBiYWNrZ3JvdW5kLgogKiBSdWxlIDI6IHNwZWFrIENoYXRzJyB2aXN1YWwgbGFuZ3VhZ2U6IHN5c3RlbSBVSSB0eXBlIGF0IHRoZSBzaGVsbCdzIHNpemVzLCBpbmsgLyBpbmstMiAvIGluay0zCiAqICAgICAgICAgaGllcmFyY2h5LCBoYWlybGluZSBzZXBhcmF0b3JzLCBmYWludCB0aW50ZWQgZmlsbHMgaW5zdGVhZCBvZiBvcGFxdWUgY2FyZHMsIHBpbGwgaW5wdXRzLAogKiAgICAgICAgIGEgcm91bmQgYWNjZW50IGFjdGlvbi4gRXZlcnkgdmFsdWUgZGVyaXZlcyBmcm9tIHRoZSBhcHBlYXJhbmNlIHYxIHJvbGVzICgtLXd6LSopLCBzbyBhbGwKICogICAgICAgICB0aGUgZml2ZSBhY2NlcHRlZCBza2lucyBhbmQgZnV0dXJlIHNoZWxsLWFwcHJvdmVkIHNraW5zIGZsb3cgdGhyb3VnaCB3aXRob3V0IHBlci1hcHAgY29sb3VyIHdvcmsuCiAqIFJ1bGUgMzogbGF5b3V0IGFuZCBjb250ZW50IHN0YXkgdGhlIGFwcCdzIG93bi4gVXNlIHRoZXNlIGNsYXNzZXM7IGFkZCBhcHAgc3R5bGVzIGluIHN0eWxlcy5jc3MuCiAqIFJ1bGUgNDogbG9vayBvbmx5LiBUYWxraW5nIHRvIGFuIGFnZW50IGlzIHRoZSByZWFsIENoYXRzIHRocmVhZCAoV2l6YXJkQXBwLm5hdi5vcGVuVGhyZWFkKSwgbmV2ZXIgYQogKiAgICAgICAgIGNvbXBvc2VyIG9yIHRyYW5zY3JpcHQgYnVpbHQgZnJvbSB0aGVzZSBjbGFzc2VzICh3aXphcmQtdXggZG9jcy9DT01QT05FTlRTLm1kKS4KICogUnVsZSA1OiBza2lucyBtYXkgcmVzdHlsZSB0aGVzZSBjbGFzc2VzLiBIb3N0ZWQgYXBwcyBsb2FkIHRoZSBsaXZlIGNvcHkgb2YgdGhpcyBraXQgZnJvbSB0aGUgc2hlbGwKICogICAgICAgICAoL3dpemFyZC1vcy1ob21lL2tpdC93aXphcmQtdWkuY3NzKSBhbmQgdGhlIGFjdGl2ZSBza2luJ3Mgc3R5bGVzaGVldHMgYWZ0ZXIgdGhlIGFwcCdzIG93bi4KICoKICogVmFsdWVzIG1pcnJvciB3ZWIvY29yZS9jaGF0cy5jc3MgYW5kIGNvcmUvYmFzZS5jc3MgaW4gV2l6YXJkIFVYIChjaC1yb3csIGNoLXNlYy1oKS4gKi8KCjpyb290IHsKICAtLWluazogdmFyKC0td3otdGV4dCk7IC0taW5rLTI6IHZhcigtLXd6LXRleHQtc2Vjb25kYXJ5KTsgLS1pbmstMzogdmFyKC0td3otdGV4dC1tdXRlZCk7CiAgLS1saW5lLTI6IHZhcigtLXd6LWJvcmRlcik7IC0tbGluZTogY29sb3ItbWl4KGluIHNyZ2IsIHZhcigtLWxpbmUtMikgNTUlLCB0cmFuc3BhcmVudCk7CiAgLS1maWxsOiB2YXIoLS13ei1zdXJmYWNlKTsgLS1maWxsLTI6IHZhcigtLXd6LXN1cmZhY2UtcmFpc2VkKTsKICAtLWFjY2VudDogdmFyKC0td3otYWNjZW50KTsgLS1vbi1hY2NlbnQ6IHZhcigtLXd6LW9uLWFjY2VudCk7IC0tYWNjZW50LXNvZnQ6IHZhcigtLXd6LWFjY2VudC1zb2Z0KTsKICAtLWNhbnZhczogdmFyKC0td3otYmFzZSk7IC0tZGFuZ2VyOiB2YXIoLS13ei1kYW5nZXIpOwogIC0tcjogdmFyKC0td3otcmFkaXVzKTsgLS1yLXRpbGU6IHZhcigtLXd6LXJhZGl1cy10aWxlKTsgLS1yLXBpbGw6IHZhcigtLXd6LXJhZGl1cy1waWxsKTsKICAtLWVhc2U6IHZhcigtLXd6LWVhc2UpOyAtLWZvbnQ6IHZhcigtLXd6LWZvbnQtdWkpOwp9Cmh0bWxbZGF0YS1jb2xvci1zY2hlbWU9ImxpZ2h0Il0geyBjb2xvci1zY2hlbWU6IGxpZ2h0OyAtLWRhbmdlcjogdmFyKC0taW5rKTsgfQpodG1sW2RhdGEtc2tpbj0ic2lnbmFsIl0gLnd6LXNlYy1oIHsgZm9udC1mYW1pbHk6IHZhcigtLXd6LWZvbnQtbW9ubyk7IGxldHRlci1zcGFjaW5nOiAuMDhlbTsgfQpodG1sIHsgYmFja2dyb3VuZDogdmFyKC0td3otYmFzZSk7IH0KaHRtbC53ei1ob3N0ZWQsIGh0bWxbZGF0YS13ei1ob3N0ZWRdIHsgYmFja2dyb3VuZDogdHJhbnNwYXJlbnQ7IH0KCiosICo6OmJlZm9yZSwgKjo6YWZ0ZXIgeyBib3gtc2l6aW5nOiBib3JkZXItYm94OyB9Cmh0bWwsIGJvZHkgeyBoZWlnaHQ6IDEwMCU7IG1hcmdpbjogMDsgfQpodG1sIHsgLXdlYmtpdC10ZXh0LXNpemUtYWRqdXN0OiAxMDAlOyAtd2Via2l0LXRhcC1oaWdobGlnaHQtY29sb3I6IHRyYW5zcGFyZW50OyB9CmJvZHkgeyBmb250OiAxNXB4LzEuNCB2YXIoLS1mb250KTsgY29sb3I6IHZhcigtLWluayk7IGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OyAtd2Via2l0LWZvbnQtc21vb3RoaW5nOiBhbnRpYWxpYXNlZDsgb3ZlcnNjcm9sbC1iZWhhdmlvcjogbm9uZTsgfQpodG1sW2RhdGEtcHJlc2VudGF0aW9uPSJzdGFuZGFsb25lIl06bm90KC53ei1ob3N0ZWQpIGJvZHkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1jYW52YXMpOyB9CmJ1dHRvbiwgaW5wdXQsIHRleHRhcmVhLCBzZWxlY3QgeyBmb250OiBpbmhlcml0OyBjb2xvcjogaW5oZXJpdDsgfQpidXR0b24geyBjdXJzb3I6IHBvaW50ZXI7IGJvcmRlcjogMDsgYmFja2dyb3VuZDogbm9uZTsgcGFkZGluZzogMDsgfQo6Zm9jdXMtdmlzaWJsZSB7IG91dGxpbmU6IDJweCBzb2xpZCB2YXIoLS1hY2NlbnQpOyBvdXRsaW5lLW9mZnNldDogMnB4OyB9Ci53ei1zciB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgd2lkdGg6IDFweDsgaGVpZ2h0OiAxcHg7IG92ZXJmbG93OiBoaWRkZW47IGNsaXAtcGF0aDogaW5zZXQoNTAlKTsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQoKLyogLS0tLS0tLS0tLSBmcmFtZTogaGVhZCAvIHNjcm9sbCAvIGZvb3RlciAtLS0tLS0tLS0tICovCi8qIFRoZSB3aW5kb3cgaXMgdGhlIHBhZ2UgKEFQUC1ERVNJR04tUlVMRVMubWQgcnVsZSAxLTIpOiBubyBtYXgtd2lkdGg7IGNvbHVtbnMgZ3JvdyB3aXRoIHRoZSB3aW5kb3csIG1lYXN1cmVkIG9uIC53ei1hcHAuICovCi53ei1hcHAgeyAtLXd6LWd1dHRlcjogMTJweDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgaGVpZ2h0OiAxMDAlOyBjb250YWluZXI6IHd6LWFwcCAvIGlubGluZS1zaXplOyB9CkBjb250YWluZXIgd3otYXBwIChtaW4td2lkdGg6IDYwMHB4KSB7IC53ei1zY3JvbGwsIC53ei1oZWFkIHsgLS13ei1ndXR0ZXI6IDIwcHg7IH0gfQpAY29udGFpbmVyIHd6LWFwcCAobWluLXdpZHRoOiA5NjBweCkgeyAud3otc2Nyb2xsLCAud3otaGVhZCB7IC0td3otZ3V0dGVyOiAyOHB4OyB9IH0KLnd6LWhlYWQgeyBmbGV4OiBub25lOyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogZmxleC1lbmQ7IGdhcDogMTJweDsgcGFkZGluZzogY2FsYyh2YXIoLS13b3MtY3V0b3V0LCAwcHgpICsgMThweCkgMThweCA2cHg7IH0KLnd6LWhlYWQgaDEgeyBtYXJnaW46IDA7IGZvbnQtc2l6ZTogMjZweDsgbGluZS1oZWlnaHQ6IDEuMTU7IGZvbnQtd2VpZ2h0OiA3NTA7IGxldHRlci1zcGFjaW5nOiAtLjAyZW07IH0KLnd6LWhlYWQgcCB7IG1hcmdpbjogMCAwIDNweCBhdXRvOyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLyogSW4gYSB3aW5kb3cgdGhlIHNoZWxsJ3MgdGl0bGUgYmFyIGFscmVhZHkgbmFtZXMgdGhlIGFwcC4gKi8KaHRtbFtkYXRhLXByZXNlbnRhdGlvbj0id2luZG93Il0gLnd6LWhlYWQgeyBkaXNwbGF5OiBub25lOyB9Ci53ei1zY3JvbGwgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBvdmVyZmxvdy15OiBhdXRvOyBwYWRkaW5nOiA0cHggdmFyKC0td3otZ3V0dGVyLCAxMHB4KSAxMnB4OyBzY3JvbGxiYXItd2lkdGg6IG5vbmU7IH0KLnd6LXNjcm9sbDo6LXdlYmtpdC1zY3JvbGxiYXIgeyBkaXNwbGF5OiBub25lOyB9CgovKiAtLS0tLS0tLS0tIHNlY3Rpb24gbGFiZWw6IC5jaC1zZWMtaCAtLS0tLS0tLS0tICovCi53ei1zZWMgKyAud3otc2VjIHsgYm9yZGVyLXRvcDogMXB4IHNvbGlkIHZhcigtLWxpbmUpOyB9Ci53ei1zZWMtaCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTBweDsgbWFyZ2luOiAwOyBwYWRkaW5nOiAxMnB4IDhweCA4cHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGZvbnQtd2VpZ2h0OiA4MDA7IGxldHRlci1zcGFjaW5nOiAuMTRlbTsgdGV4dC10cmFuc2Zvcm06IHVwcGVyY2FzZTsgY29sb3I6IHZhcigtLWluay0yKTsgfQoud3otc2VjLWggc21hbGwgeyBmb250LXNpemU6IHZhcigtLXd6LXRleHQtc20pOyBmb250LXdlaWdodDogODAwOyBsZXR0ZXItc3BhY2luZzogLjA1ZW07IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LWNvdW50IHsgbWFyZ2luLWxlZnQ6IGF1dG87IHBhZGRpbmc6IDFweCA4cHg7IGJvcmRlci1yYWRpdXM6IDk5cHg7IGZvbnQtc2l6ZTogdmFyKC0td3otdGV4dC1zbSk7IGxldHRlci1zcGFjaW5nOiAuMDRlbTsgY29sb3I6IHZhcigtLWluay0yKTsgYmFja2dyb3VuZDogdmFyKC0tZmlsbCk7IH0KLnd6LWNvdW50LmFsbCB7IGNvbG9yOiB2YXIoLS1vbi1hY2NlbnQpOyBiYWNrZ3JvdW5kOiB2YXIoLS1hY2NlbnQpOyB9CgovKiAtLS0tLS0tLS0tIHJvd3M6IC5jaC1yb3cgLS0tLS0tLS0tLSAqLwoud3otbGlzdCB7IGxpc3Qtc3R5bGU6IG5vbmU7IG1hcmdpbjogMDsgcGFkZGluZzogMDsgfQoud3otcm93IHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyB3aWR0aDogMTAwJTsgbWluLWhlaWdodDogNTJweDsgcGFkZGluZzogOHB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yKTsgdGV4dC1hbGlnbjogbGVmdDsgdHJhbnNpdGlvbjogYmFja2dyb3VuZCAxNDBtczsgfQoud3otcm93OmFjdGl2ZSB7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB9CkBtZWRpYSAoaG92ZXI6IGhvdmVyKSB7IC53ei1yb3c6aG92ZXIgeyBiYWNrZ3JvdW5kOiB2YXIoLS1maWxsKTsgfSB9Ci53ei1yb3cgLnd6LW1haW4geyBmbGV4OiAxOyBtaW4td2lkdGg6IDA7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogMnB4OyB9Ci53ei1yb3cgLnd6LW1haW4gYiB7IGZvbnQtc2l6ZTogMTVweDsgZm9udC13ZWlnaHQ6IDYwMDsgY29sb3I6IHZhcigtLWluayk7IG92ZXJmbG93LXdyYXA6IGFueXdoZXJlOyB9Ci53ei1yb3cgLnd6LW1haW4gc21hbGwgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LXRpbGUgeyBmbGV4OiBub25lOyB3aWR0aDogMzZweDsgaGVpZ2h0OiAzNnB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yLXRpbGUpOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyBmb250LXNpemU6IDE4cHg7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB9CgovKiByb3VuZCBjaGVjaywgMjZweCBpbnNpZGUgYSAzNnB4IHRpbGUtc2l6ZWQgc2xvdCAodGhlIHdob2xlIHJvdyBpcyB0aGUgNDRweCsgdGFyZ2V0KSAqLwoud3otY2hlY2sgeyBmbGV4OiBub25lOyB3aWR0aDogMzZweDsgaGVpZ2h0OiAzNnB4OyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyB9Ci53ei1jaGVjayBpIHsgd2lkdGg6IDI2cHg7IGhlaWdodDogMjZweDsgYm9yZGVyLXJhZGl1czogNTAlOyBib3JkZXI6IDJweCBzb2xpZCB2YXIoLS1saW5lLTIpOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIDE2MG1zIHZhcigtLWVhc2UpLCBib3JkZXItY29sb3IgMTYwbXMsIHRyYW5zZm9ybSAyMjBtcyB2YXIoLS1lYXNlKTsgfQoud3otY2hlY2sgc3ZnIHsgd2lkdGg6IDE0cHg7IGhlaWdodDogMTRweDsgZmlsbDogbm9uZTsgc3Ryb2tlOiB2YXIoLS1vbi1hY2NlbnQpOyBzdHJva2Utd2lkdGg6IDM7IHN0cm9rZS1saW5lY2FwOiByb3VuZDsgc3Ryb2tlLWxpbmVqb2luOiByb3VuZDsgb3BhY2l0eTogMDsgdHJhbnNpdGlvbjogb3BhY2l0eSAxMjBtczsgfQpodG1sW2RhdGEtc2tpbj0ic2lnbmFsIl0gLnd6LWNoZWNrIGkgeyBib3JkZXItcmFkaXVzOiAycHg7IH0KW2FyaWEtcHJlc3NlZD0idHJ1ZSJdIC53ei1jaGVjayBpIHsgYmFja2dyb3VuZDogdmFyKC0tYWNjZW50KTsgYm9yZGVyLWNvbG9yOiB2YXIoLS1hY2NlbnQpOyB0cmFuc2Zvcm06IHNjYWxlKDEuMDQpOyB9ClthcmlhLXByZXNzZWQ9InRydWUiXSAud3otY2hlY2sgc3ZnIHsgb3BhY2l0eTogMTsgfQpbYXJpYS1wcmVzc2VkPSJ0cnVlIl0gLnd6LW1haW4gYiB7IGNvbG9yOiB2YXIoLS1pbmstMik7IH0KLnd6LXJvdzpkaXNhYmxlZCB7IG9wYWNpdHk6IC42OyBjdXJzb3I6IHByb2dyZXNzOyB9CgovKiAtLS0tLS0tLS0tIGVtcHR5IHN0YXRlIC0tLS0tLS0tLS0gKi8KLnd6LWVtcHR5IHsgcGFkZGluZzogNDRweCAyNHB4IDI0cHg7IHRleHQtYWxpZ246IGNlbnRlcjsgY29sb3I6IHZhcigtLWluay0zKTsgfQoud3otZW1wdHkgLnd6LXRpbGUgeyBtYXJnaW46IDAgYXV0byAxNHB4OyB3aWR0aDogNTJweDsgaGVpZ2h0OiA1MnB4OyBib3JkZXItcmFkaXVzOiAxOHB4OyBmb250LXNpemU6IDI0cHg7IGNvbG9yOiB2YXIoLS1hY2NlbnQpOyB9Ci53ei1lbXB0eSBzdHJvbmcgeyBkaXNwbGF5OiBibG9jazsgbWFyZ2luLWJvdHRvbTogNHB4OyBmb250LXNpemU6IDE2cHg7IGZvbnQtd2VpZ2h0OiA3MDA7IGNvbG9yOiB2YXIoLS1pbmspOyB9Ci53ei1lbXB0eSBwIHsgbWFyZ2luOiAwIGF1dG87IG1heC13aWR0aDogMzBjaDsgZm9udC1zaXplOiAxNHB4OyBsaW5lLWhlaWdodDogMS40OyB9CgovKiAtLS0tLS0tLS0tIHNpbmdsZS1saW5lIGFkZCBmaWVsZCAoZm9ybXMgc3VjaCBhcyAiTmV3IGhhYml0IjsgbmV2ZXIgYSBjaGF0KSAtLS0tLS0tLS0tCiAqIC53ei1jb21wb3NlIC8gLnd6LWlucHV0IC8gLnd6LXNlbmQgYXJlIHRoZSBwcmUtMjMgT2N0IDIwMjYgbmFtZXMsIGtlcHQgc28gZXhpc3RpbmcgYXBwcyBrZWVwIHRoZWlyIGxvb2suICovCi53ei1maWVsZC1yb3csIC53ei1jb21wb3NlIHsgZmxleDogbm9uZTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA4cHg7IHBhZGRpbmc6IDhweCAxMHB4IGNhbGMoMTBweCArIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tLCAwcHgpKTsgYm9yZGVyLXRvcDogMXB4IHNvbGlkIHZhcigtLWxpbmUpOyB9Ci53ei1maWVsZCwgLnd6LWlucHV0IHsgZmxleDogMTsgbWluLXdpZHRoOiAwOyBoZWlnaHQ6IDQ0cHg7IHBhZGRpbmc6IDAgMTZweDsgYm9yZGVyLXJhZGl1czogdmFyKC0tci1waWxsKTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZS0yKTsgb3V0bGluZTogMDsgYmFja2dyb3VuZDogdmFyKC0tZmlsbCk7IGNvbG9yOiB2YXIoLS1pbmspOyBmb250LXNpemU6IDE1cHg7IH0KLnd6LWZpZWxkOjpwbGFjZWhvbGRlciwgLnd6LWlucHV0OjpwbGFjZWhvbGRlciB7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LWZpZWxkOmZvY3VzLCAud3otaW5wdXQ6Zm9jdXMgeyBib3JkZXItY29sb3I6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS1hY2NlbnQpIDYwJSwgdHJhbnNwYXJlbnQpOyB9Ci53ei1maWVsZC1nbywgLnd6LXNlbmQgeyBmbGV4OiBub25lOyB3aWR0aDogNDRweDsgaGVpZ2h0OiA0NHB4OyBib3JkZXItcmFkaXVzOiA1MCU7IGRpc3BsYXk6IGdyaWQ7IHBsYWNlLWl0ZW1zOiBjZW50ZXI7IGJhY2tncm91bmQ6IHZhcigtLWFjY2VudCk7IGNvbG9yOiB2YXIoLS1vbi1hY2NlbnQpOyB0cmFuc2l0aW9uOiBvcGFjaXR5IDE0MG1zLCB0cmFuc2Zvcm0gMTQwbXM7IH0KaHRtbFtkYXRhLXNraW49InNpZ25hbCJdIDppcygud3otZmllbGQtZ28sIC53ei1zZW5kKSB7IGJvcmRlci1yYWRpdXM6IDJweDsgfQo6aXMoLnd6LWZpZWxkLWdvLCAud3otc2VuZCkgc3ZnIHsgd2lkdGg6IDE4cHg7IGhlaWdodDogMThweDsgZmlsbDogbm9uZTsgc3Ryb2tlOiBjdXJyZW50Q29sb3I7IHN0cm9rZS13aWR0aDogMi42OyBzdHJva2UtbGluZWNhcDogcm91bmQ7IH0KOmlzKC53ei1maWVsZC1nbywgLnd6LXNlbmQpOmRpc2FibGVkIHsgb3BhY2l0eTogLjQ7IGN1cnNvcjogZGVmYXVsdDsgfQo6aXMoLnd6LWZpZWxkLWdvLCAud3otc2VuZCk6bm90KDpkaXNhYmxlZCk6YWN0aXZlIHsgdHJhbnNmb3JtOiBzY2FsZSguOTQpOyB9CgovKiAtLS0tLS0tLS0tIHN0YXR1czogZXJyb3JzIGFyZSB2aXNpYmxlLCBjb25maXJtYXRpb25zIGFyZSBmb3Igc2NyZWVuIHJlYWRlcnMgLS0tLS0tLS0tLSAqLwoud3otc3RhdHVzIHsgZmxleDogbm9uZTsgbWFyZ2luOiAwOyBwYWRkaW5nOiAwIDE4cHggNnB4OyBtaW4taGVpZ2h0OiAwOyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1kYW5nZXIpOyB9Ci53ei1zdGF0dXM6ZW1wdHkgeyBwYWRkaW5nOiAwOyB9Ci53ei1zdGF0dXMubm90ZSB7IGNvbG9yOiB2YXIoLS1pbmstMik7IHRleHQtYWxpZ246IGNlbnRlcjsgfQoKLyogU3RhZ2luZyBob3N0cyBsYWJlbCB0aGUgcGFnZSBvbmNlOyBhIGhvc3RlZCBhcHAgbmV2ZXIgcmVwZWF0cyB0aGF0IGJhZGdlLiAqLwpodG1sLnd6LWhvc3RlZCAjd2l6LXN0YWdpbmctdGFnIHsgZGlzcGxheTogbm9uZSAhaW1wb3J0YW50OyB9CgpAbWVkaWEgKHByZWZlcnMtcmVkdWNlZC1tb3Rpb246IHJlZHVjZSkgeyAqLCAqOjpiZWZvcmUsICo6OmFmdGVyIHsgdHJhbnNpdGlvbjogbm9uZSAhaW1wb3J0YW50OyBhbmltYXRpb246IG5vbmUgIWltcG9ydGFudDsgfSB9CgovKiBTaGFyZWQgc2VtYW50aWMgcHJpbWl0aXZlczogdGludGVkIHN1cmZhY2VzLCBuZXZlciBhIHNlY29uZCBmdWxsLXBhZ2UgY2FudmFzLiAqLwoud3otY2FyZCB7IHBhZGRpbmc6IHZhcigtLXd6LXNwYWNlLTQpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS1saW5lKTsgYm9yZGVyLXJhZGl1czogdmFyKC0tcik7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB9Ci53ei1jYXJkID4gOmZpcnN0LWNoaWxkIHsgbWFyZ2luLXRvcDogMDsgfSAud3otY2FyZCA+IDpsYXN0LWNoaWxkIHsgbWFyZ2luLWJvdHRvbTogMDsgfQoud3otY2hpcCB7IGRpc3BsYXk6IGlubGluZS1mbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IHZhcigtLXd6LXNwYWNlLTIpOyBwYWRkaW5nOiA0cHggMTBweDsgYm9yZGVyLXJhZGl1czogdmFyKC0tci1waWxsKTsgYmFja2dyb3VuZDogdmFyKC0tZmlsbCk7IGNvbG9yOiB2YXIoLS1pbmstMik7IGZvbnQtc2l6ZTogMTNweDsgfQoud3otYmFyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiB2YXIoLS13ei1zcGFjZS0yKTsgcGFkZGluZzogOHB4IDEwcHg7IGJhY2tncm91bmQ6IHZhcigtLXd6LWNocm9tZSk7IGJvcmRlci1ib3R0b206IDFweCBzb2xpZCB2YXIoLS1saW5lKTsgYmFja2Ryb3AtZmlsdGVyOiBibHVyKDE2cHgpOyB9Ci53ei1idXR0b24geyBkaXNwbGF5OiBpbmxpbmUtZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IGJvcmRlcjogMDsgZm9udDogaW5oZXJpdDsgdGV4dC1kZWNvcmF0aW9uOiBub25lOyBjdXJzb3I6IHBvaW50ZXI7IGJveC1zaXppbmc6IGJvcmRlci1ib3g7IG1pbi1oZWlnaHQ6IDQ0cHg7IHBhZGRpbmc6IDhweCAxNnB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yLXRpbGUpOyBiYWNrZ3JvdW5kOiB2YXIoLS1hY2NlbnQpOyBjb2xvcjogdmFyKC0tb24tYWNjZW50KTsgZm9udC13ZWlnaHQ6IDYwMDsgfQoud3otYnV0dG9uLnNlY29uZGFyeSwgLnd6LWJ1dHRvbi5xdWlldCB7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyBjb2xvcjogdmFyKC0taW5rKTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZS0yKTsgfQoud3otYnV0dG9uOmRpc2FibGVkIHsgb3BhY2l0eTogLjQ1OyBjdXJzb3I6IGRlZmF1bHQ7IH0KLyogLS0tLS0tLS0tLSBsYXlvdXQgaGVscGVycyAocnVsZXMgMSwgMiwgNSkgLS0tLS0tLS0tLSAqLwoud3otZ3JpZCB7IGRpc3BsYXk6IGdyaWQ7IGdhcDogdmFyKC0td3otc3BhY2UtMyk7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyOyB9CkBjb250YWluZXIgd3otYXBwIChtaW4td2lkdGg6IDYwMHB4KSB7IC53ei1ncmlkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoMiwgbWlubWF4KDAsIDFmcikpOyB9IH0KQGNvbnRhaW5lciB3ei1hcHAgKG1pbi13aWR0aDogOTYwcHgpIHsgLnd6LWdyaWQgeyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdChhdXRvLWZpbGwsIG1pbm1heCgzMDBweCwgMWZyKSk7IH0gfQoud3otcHJvc2UgeyBtYXgtd2lkdGg6IDcyY2g7IH0KLnd6LXBpbGwgeyBkaXNwbGF5OiBpbmxpbmUtZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IHBhZGRpbmc6IDNweCAxMHB4OyBib3JkZXItcmFkaXVzOiB2YXIoLS1yLXBpbGwpOyBmb250LXNpemU6IDEzcHg7IGZvbnQtd2VpZ2h0OiA2NTA7IGNvbG9yOiB2YXIoLS1pbmstMik7IGJhY2tncm91bmQ6IHZhcigtLWZpbGwpOyB3aGl0ZS1zcGFjZTogbm93cmFwOyB9Ci53ei1waWxsOjpiZWZvcmUgeyBjb250ZW50OiAiIjsgd2lkdGg6IDdweDsgaGVpZ2h0OiA3cHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogY3VycmVudENvbG9yOyB9Ci53ei1waWxsLm9rIHsgY29sb3I6IHZhcigtLXd6LXN1Y2Nlc3MsIHZhcigtLWFjY2VudCkpOyB9Ci53ei1waWxsLndhcm4geyBjb2xvcjogdmFyKC0td3otd2FybmluZywgdmFyKC0tZGFuZ2VyKSk7IH0KLyogTGlnaHQgc2NoZW1lczogZGVlcCBncmVlbiBhbmQgYnVybnQgb3JhbmdlIHRoYXQgcGFzcyA0LjU6MSBvbiBwYWxlIHBhZ2VzICh3aGl0ZSwgY3JlYW0sIHNlcGlhKS4gKi8KaHRtbFtkYXRhLWNvbG9yLXNjaGVtZT0ibGlnaHQiXSAud3otcGlsbC5vayB7IGNvbG9yOiB2YXIoLS13ei1zdWNjZXNzLWxpZ2h0LCB2YXIoLS1pbmspKTsgfQpodG1sW2RhdGEtY29sb3Itc2NoZW1lPSJsaWdodCJdIC53ei1waWxsLndhcm4geyBjb2xvcjogdmFyKC0td3otd2FybmluZy1saWdodCwgdmFyKC0taW5rKSk7IH0KLnd6LXBpbGwub2ZmIHsgY29sb3I6IHZhcigtLWluay0zKTsgfQoud3otbWV0ZXIgeyBkaXNwbGF5OiBncmlkOyBnYXA6IDRweDsgZm9udC1zaXplOiAxM3B4OyBjb2xvcjogdmFyKC0taW5rLTMpOyB9Ci53ei1tZXRlciA+IGkgeyBkaXNwbGF5OiBibG9jazsgaGVpZ2h0OiA2cHg7IGJvcmRlci1yYWRpdXM6IDNweDsgYmFja2dyb3VuZDogdmFyKC0tZmlsbC0yKTsgb3ZlcmZsb3c6IGhpZGRlbjsgfQoud3otbWV0ZXIgPiBpID4gYiB7IGRpc3BsYXk6IGJsb2NrOyBoZWlnaHQ6IDEwMCU7IHdpZHRoOiB2YXIoLS12LCAwJSk7IGJvcmRlci1yYWRpdXM6IGluaGVyaXQ7IGJhY2tncm91bmQ6IHZhcigtLWFjY2VudCk7IH0KLnd6LWNvbm4geyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IHZhcigtLXd6LXNwYWNlLTMpOyBwYWRkaW5nOiB2YXIoLS13ei1zcGFjZS00KTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZSk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXIpOyBiYWNrZ3JvdW5kOiB2YXIoLS1maWxsKTsgfQoud3otY29ubiAud3otdGlsZSB7IHdpZHRoOiA0OHB4OyBoZWlnaHQ6IDQ4cHg7IGJvcmRlci1yYWRpdXM6IDE0cHg7IH0KLnd6LWNvbm4gLnd6LW1haW4geyBmbGV4OiAxOyBtaW4td2lkdGg6IDA7IGRpc3BsYXk6IGdyaWQ7IGdhcDogM3B4OyB9Ci53ei1jb25uIC53ei1tYWluIGIgeyBmb250LXNpemU6IDE3cHg7IGZvbnQtd2VpZ2h0OiA3MDA7IH0KLnd6LWNvbm4gLnd6LW1haW4gc21hbGwgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1pbmstMyk7IH0KLnd6LWFjdGlvbnMgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LXdyYXA6IHdyYXA7IGdhcDogdmFyKC0td3otc3BhY2UtMik7IHBhZGRpbmc6IHZhcigtLXd6LXNwYWNlLTMpIDA7IH0KLnd6LW1vcmUgeyBtYXJnaW4tdG9wOiB2YXIoLS13ei1zcGFjZS00KTsgY29sb3I6IHZhcigtLWluay0zKTsgZm9udC1zaXplOiAxM3B4OyB9Ci53ei1tb3JlID4gc3VtbWFyeSB7IGN1cnNvcjogcG9pbnRlcjsgcGFkZGluZzogOHB4OyBsaXN0LXN0eWxlOiBub25lOyBmb250LXdlaWdodDogNjUwOyBjb2xvcjogdmFyKC0taW5rLTIpOyB9Ci53ei1tb3JlID4gc3VtbWFyeTo6LXdlYmtpdC1kZXRhaWxzLW1hcmtlciB7IGRpc3BsYXk6IG5vbmU7IH0KCi8qIC0tLS0tLS0tLS0gdGhlIG9uZSBwb3AtdXAgKHJ1bGUgNCk6IGJvdHRvbSBzaGVldCB3aGVuIG5hcnJvdywgY2VudHJlZCBjYXJkIHdoZW4gd2lkZS4gQmVoYXZpb3VyOiBzdHlsZS9zaGVldC5qcyAtLS0tLS0tLS0tICovCi53ei1zaGVldCB7IGNvbG9yOiB2YXIoLS1pbmspOyBiYWNrZ3JvdW5kOiB2YXIoLS13ei1wYW5lbCk7IGJvcmRlcjogMXB4IHNvbGlkIHZhcigtLWxpbmUtMik7IGJvcmRlci1yYWRpdXM6IHZhcigtLXIpOyBwYWRkaW5nOiB2YXIoLS13ei1zcGFjZS00KTsgd2lkdGg6IG1pbig1MjBweCwgY2FsYygxMDAlIC0gMjRweCkpOyBtYXgtd2lkdGg6IG5vbmU7IG1heC1oZWlnaHQ6IDg1JTsgb3ZlcmZsb3c6IGF1dG87IGJhY2tkcm9wLWZpbHRlcjogYmx1cigyMHB4KTsgLXdlYmtpdC1iYWNrZHJvcC1maWx0ZXI6IGJsdXIoMjBweCk7IH0KLnd6LXNoZWV0OjpiYWNrZHJvcCB7IGJhY2tncm91bmQ6IGNvbG9yLW1peChpbiBzcmdiLCB2YXIoLS13ei1iYXNlKSA3NCUsIHRyYW5zcGFyZW50KTsgfQoud3otc2hlZXQteCB7IHBvc2l0aW9uOiBzdGlja3k7IHRvcDogMDsgZmxvYXQ6IHJpZ2h0OyB3aWR0aDogMzZweDsgaGVpZ2h0OiAzNnB4OyBtYXJnaW46IC02cHggLTZweCAwIDhweDsgYm9yZGVyLXJhZGl1czogNTAlOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyBjb2xvcjogdmFyKC0taW5rLTIpOyBiYWNrZ3JvdW5kOiB2YXIoLS1maWxsKTsgZm9udC1zaXplOiAxOHB4OyBsaW5lLWhlaWdodDogMTsgei1pbmRleDogMTsgfQoud3otc2hlZXQteDo6YWZ0ZXIgeyBjb250ZW50OiAiIjsgcG9zaXRpb246IGFic29sdXRlOyBpbnNldDogLTRweDsgYm9yZGVyLXJhZGl1czogNTAlOyB9IC8qIDQ0cHggdG91Y2ggdGFyZ2V0IChBUFAtREVTSUdOLVJVTEVTIHJ1bGUgNikgYXJvdW5kIHRoZSAzNnB4IHZpc3VhbCAqLwoud3otc2hlZXQtZ3JpcCB7IGRpc3BsYXk6IG5vbmU7IH0KLnd6LXNoZWV0ID4gaDIgeyBtYXJnaW46IDAgMCB2YXIoLS13ei1zcGFjZS0zKTsgZm9udC1zaXplOiAxN3B4OyBmb250LXdlaWdodDogNzAwOyB9CkBtZWRpYSAobWF4LXdpZHRoOiA1OTlweCkgewogIC53ei1zaGVldCB7IHdpZHRoOiAxMDAlOyBtYXgtd2lkdGg6IDEwMCU7IG1hcmdpbjogYXV0byAwIDA7IGJvcmRlci1yYWRpdXM6IDIycHggMjJweCAwIDA7IGJvcmRlci1ib3R0b206IDA7IG1heC1oZWlnaHQ6IDg4JTsgcGFkZGluZzogNnB4IHZhcigtLXd6LXNwYWNlLTQpIGNhbGModmFyKC0td3otc3BhY2UtNCkgKyBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSwgMHB4KSk7IH0KICAud3otc2hlZXRbb3Blbl0geyBhbmltYXRpb246IHd6LXNoZWV0LXVwIDIyMG1zIHZhcigtLWVhc2UsIGVhc2Utb3V0KTsgfQogIC53ei1zaGVldC1ncmlwIHsgZGlzcGxheTogYmxvY2s7IHdpZHRoOiA0MHB4OyBoZWlnaHQ6IDVweDsgbWFyZ2luOiA0cHggYXV0byAxMHB4OyBib3JkZXItcmFkaXVzOiAzcHg7IGJhY2tncm91bmQ6IHZhcigtLWxpbmUtMik7IHRvdWNoLWFjdGlvbjogbm9uZTsgfQp9CkBrZXlmcmFtZXMgd3otc2hlZXQtdXAgeyBmcm9tIHsgdHJhbnNmb3JtOiB0cmFuc2xhdGVZKDQwcHgpOyBvcGFjaXR5OiAuNjsgfSB9CgovKiBTaGFyZWQgY29udHJvbCB2YXJpYW50czsgZG8gbm90IHJlZHJhdyBpbiBhcHBzLiAqLwoud3otYnV0dG9uLmRlc3RydWN0aXZlIHsgYmFja2dyb3VuZDogY29sb3ItbWl4KGluIHNyZ2IsIHZhcigtLXd6LWRhbmdlcikgMTYlLCB2YXIoLS13ei1iYXNlKSk7IGNvbG9yOiB2YXIoLS13ei1kYW5nZXIpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS13ei1kYW5nZXIpOyB9Ci53ei1pY29uLWJ1dHRvbiB7IGRpc3BsYXk6IGlubGluZS1ncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOyBtaW4td2lkdGg6IHZhcigtLXd6LXRhcmdldCk7IG1pbi1oZWlnaHQ6IHZhcigtLXd6LXRhcmdldCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXd6LXJhZGl1cy10aWxlKTsgYmFja2dyb3VuZDogdmFyKC0td3otc3VyZmFjZSk7IGNvbG9yOiB2YXIoLS13ei10ZXh0KTsgfQoud3otaWNvbi1idXR0b24gc3ZnIHsgd2lkdGg6IHZhcigtLXd6LWljb24tc2l6ZSk7IGhlaWdodDogdmFyKC0td3otaWNvbi1zaXplKTsgfQoud3otcGlsbC5lcnJvciB7IGNvbG9yOiB2YXIoLS13ei1kYW5nZXIpOyB9CgovKiBDYXB0aW9uZWQgbWVkaWEgYWN0aW9ucyByZXVzZSB0aGUgc2FtZSB0YXJnZXQ7IG5vIG9wYXF1ZSB0aWxlIG92ZXIgaW1hZ2VyeS4gKi8KLnd6LWljb24tYnV0dG9uLmdob3N0IHsgYmFja2dyb3VuZDogbm9uZTsgfQoud3otaWNvbi1idXR0b24uY2FwdGlvbiB7IGRpc3BsYXk6IGlubGluZS1mbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgZ2FwOiB2YXIoLS13ei1zcGFjZS0xKTsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgfQoud3otaWNvbi1idXR0b24uY2FwdGlvbiA6aXMoc3Bhbiwgc21hbGwpIHsgZm9udC1zaXplOiB2YXIoLS13ei10ZXh0LXNtKTsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQoKLyogTmF0aXZlIGhpZGRlbiBzdGF0ZSBtdXN0IGJlYXQgYXV0aG9yZWQgZGlzcGxheSBydWxlcyBvbiBzaGFyZWQgY29udHJvbHMuICovCi53ei1idXR0b25baGlkZGVuXSwgLnd6LXJvd1toaWRkZW5dLCAud3otaWNvbi1idXR0b25baGlkZGVuXSwgLnd6LWlucHV0W2hpZGRlbl0geyBkaXNwbGF5OiBub25lICFpbXBvcnRhbnQ7IH0K', 'standalone.css': 'LyogQ2Fub25pY2FsIHN0YW5kYWxvbmUgdG9rZW5zIHByZXNlcnZlIHByZWZlcnMtY29sb3Itc2NoZW1lOiBsaWdodC4gKi8KQGltcG9ydCB1cmwoInN0YW5kYWxvbmUtdG9rZW5zLmNzcyIpOwpodG1sOm5vdCgud3otaG9zdGVkKSBib2R5IHsgYmFja2dyb3VuZDogdmFyKC0td3otYmFzZSk7IH0K', 'standalone-tokens.css': 'LyogUHJlc2VydmUgdGhlIHNoYXJlZC1mdW5jdGlvbiB0ZW1wbGF0ZSdzIHByZS1ob3N0IGxpZ2h0IGZhbGxiYWNrIHdpdGhvdXQKICogY2hhbmdpbmcgdGhlIGNhbm9uaWNhbCBzdHlsZSBzbmFwc2hvdCBvciBpbXBsZW1lbnRpbmcgdGhlbWUgc3RhdGUuICovCmh0bWw6bm90KC53ei1ob3N0ZWQpIGJvZHkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1jYW52YXMpOyB9CkBtZWRpYSAocHJlZmVycy1jb2xvci1zY2hlbWU6IGxpZ2h0KSB7CiAgaHRtbDpub3QoW2RhdGEtY29sb3Itc2NoZW1lXSkgewogICAgLS13ei10ZXh0OiAjMWExOTMzOyAtLXd6LXRleHQtc2Vjb25kYXJ5OiAjNDg0NTYwOwogICAgLS13ei10ZXh0LW11dGVkOiAjNjY2MTdhOyAtLXd6LWJvcmRlcjogI2Q0Y2ZkZTsKICAgIC0td3otYWNjZW50OiAjNjg0NGFhOyAtLXd6LW9uLWFjY2VudDogI2ZmZmZmZjsKICAgIC0td3otYmFzZTogI2ZhZjhmZDsgLS13ei1kYW5nZXI6ICNiNDIzMTg7IGNvbG9yLXNjaGVtZTogbGlnaHQ7CiAgfQp9Cg==', 'wizard-sheet.js': 'LyogV2l6YXJkIFVJIGtpdCDCtyB0aGUgb25lIHBvcC11cCAoQVBQLURFU0lHTi1SVUxFUy5tZCBydWxlIDQpLgogKiBVc2U6IDxkaWFsb2cgY2xhc3M9Ind6LXNoZWV0Ij48aDI+VGl0bGU8L2gyPuKApjwvZGlhbG9nPiwgdGhlbiBXelNoZWV0Lm9wZW4oZGlhbG9nKS4KICogQWRkcyB0aGUgZ3JpcCBhbmQgw5cgaWYgbWlzc2luZywgYW5kIGNsb3NlcyBmaXZlIHdheXM6IMOXLCBFc2MgKG5hdGl2ZSksIHRhcCBvdXRzaWRlLCBCYWNrCiAqIGFuZCBhIGRvd253YXJkIHN3aXBlIG9uIHRoZSBncmlwLiBCYWNrOiBpbiBhbiBhcHAgdGhhdCB1c2VzIFdpemFyZEFwcCwgZWFjaCBvcGVuIHNoZWV0IGlzIHB1c2hlZCBvbiBXaXphcmRBcHAuYmFjawogKiAodGhlIHNoZWxsIGFza3MgdGhlIGFwcCBjbGllbnQgZmlyc3QpOyB3aW5kb3cuX193aXphcmRDbG9zZU92ZXJsYXkgc3RheXMgYXMgdGhlIGZhbGxiYWNrIGZvciBhcHBzIHdpdGhvdXQgaXQuIE5vIGRlcGVuZGVuY2llcy4gKi8KKCgpID0+IHsKICAndXNlIHN0cmljdCc7CiAgY29uc3Qgb3BlbiA9IFtdOwogIGZ1bmN0aW9uIHdpcmUoZCkgewogICAgaWYgKGQuX193eikgcmV0dXJuIGQ7IGQuX193eiA9IHRydWU7CiAgICBpZiAoIWQucXVlcnlTZWxlY3RvcignOnNjb3BlID4gLnd6LXNoZWV0LWdyaXAnKSkgZC5pbnNlcnRBZGphY2VudEhUTUwoJ2FmdGVyYmVnaW4nLCAnPGRpdiBjbGFzcz0id3otc2hlZXQtZ3JpcCIgYXJpYS1oaWRkZW49InRydWUiPjwvZGl2PicpOwogICAgaWYgKCFkLnF1ZXJ5U2VsZWN0b3IoJzpzY29wZSA+IC53ei1zaGVldC14JykpIGQucXVlcnlTZWxlY3RvcignOnNjb3BlID4gLnd6LXNoZWV0LWdyaXAnKS5pbnNlcnRBZGphY2VudEhUTUwoJ2FmdGVyZW5kJywgJzxidXR0b24gdHlwZT0iYnV0dG9uIiBjbGFzcz0id3otc2hlZXQteCIgYXJpYS1sYWJlbD0iQ2xvc2UiPsOXPC9idXR0b24+Jyk7CiAgICBkLmFkZEV2ZW50TGlzdGVuZXIoJ2NsaWNrJywgZSA9PiB7CiAgICAgIGlmIChlLnRhcmdldC5jbG9zZXN0KCcud3otc2hlZXQteCcpKSByZXR1cm4gY2xvc2UoZCk7CiAgICAgIGlmIChlLnRhcmdldCA9PT0gZCkgeyBjb25zdCByID0gZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsgaWYgKGUuY2xpZW50WCA8IHIubGVmdCB8fCBlLmNsaWVudFggPiByLnJpZ2h0IHx8IGUuY2xpZW50WSA8IHIudG9wIHx8IGUuY2xpZW50WSA+IHIuYm90dG9tKSBjbG9zZShkKTsgfQogICAgfSk7CiAgICBkLmFkZEV2ZW50TGlzdGVuZXIoJ2Nsb3NlJywgKCkgPT4geyBjb25zdCBpID0gb3Blbi5pbmRleE9mKGQpOyBpZiAoaSA+PSAwKSBvcGVuLnNwbGljZShpLCAxKTsgaWYgKGQuX193ekJhY2spIHsgZC5fX3d6QmFjay5yZWxlYXNlKCk7IGQuX193ekJhY2sgPSBudWxsOyB9IH0pOwogICAgbGV0IHkwID0gbnVsbDsgY29uc3QgZyA9IGQucXVlcnlTZWxlY3RvcignLnd6LXNoZWV0LWdyaXAnKTsKICAgIGcuYWRkRXZlbnRMaXN0ZW5lcigncG9pbnRlcmRvd24nLCBlID0+IHsgeTAgPSBlLmNsaWVudFk7IGcuc2V0UG9pbnRlckNhcHR1cmUoZS5wb2ludGVySWQpOyB9KTsKICAgIGcuYWRkRXZlbnRMaXN0ZW5lcigncG9pbnRlcm1vdmUnLCBlID0+IHsgaWYgKHkwICE9IG51bGwpIGQuc3R5bGUudHJhbnNmb3JtID0gYHRyYW5zbGF0ZVkoJHtNYXRoLm1heCgwLCBlLmNsaWVudFkgLSB5MCl9cHgpYDsgfSk7CiAgICBjb25zdCBlbmQgPSBlID0+IHsgaWYgKHkwID09IG51bGwpIHJldHVybjsgY29uc3QgZHkgPSBlLmNsaWVudFkgLSB5MDsgeTAgPSBudWxsOyBkLnN0eWxlLnRyYW5zZm9ybSA9ICcnOyBpZiAoZHkgPiA3MCkgY2xvc2UoZCk7IH07CiAgICBnLmFkZEV2ZW50TGlzdGVuZXIoJ3BvaW50ZXJ1cCcsIGVuZCk7IGcuYWRkRXZlbnRMaXN0ZW5lcigncG9pbnRlcmNhbmNlbCcsIGVuZCk7CiAgICByZXR1cm4gZDsKICB9CiAgZnVuY3Rpb24gc2hvdyhkKSB7CiAgICB3aXJlKGQpOyBpZiAoIWQub3BlbikgZC5zaG93TW9kYWwoKTsKICAgIGlmICghb3Blbi5pbmNsdWRlcyhkKSkgb3Blbi5wdXNoKGQpOwogICAgY29uc3QgQiA9IHdpbmRvdy5XaXphcmRBcHAgJiYgd2luZG93LldpemFyZEFwcC5iYWNrOwogICAgaWYgKCFkLl9fd3pCYWNrICYmIEIgJiYgdHlwZW9mIEIucHVzaCA9PT0gJ2Z1bmN0aW9uJykgeyB0cnkgeyBkLl9fd3pCYWNrID0gQi5wdXNoKCgpID0+IHsgY2xvc2UoZCk7IHJldHVybiB0cnVlOyB9KTsgfSBjYXRjaCAoXykge30gfQogICAgcmV0dXJuIGQ7CiAgfQogIGZ1bmN0aW9uIGNsb3NlKGQpIHsgaWYgKGQgJiYgZC5vcGVuKSBkLmNsb3NlKCk7IH0KICBjb25zdCBwcmV2ID0gd2luZG93Ll9fd2l6YXJkQ2xvc2VPdmVybGF5OwogIHdpbmRvdy5fX3dpemFyZENsb3NlT3ZlcmxheSA9ICgpID0+IHsgY29uc3QgZCA9IG9wZW4ucG9wKCk7IGlmIChkKSB7IGNsb3NlKGQpOyByZXR1cm4gdHJ1ZTsgfSByZXR1cm4gdHlwZW9mIHByZXYgPT09ICdmdW5jdGlvbicgPyAhIXByZXYoKSA6IGZhbHNlOyB9OwogIHdpbmRvdy5XelNoZWV0ID0geyBvcGVuOiBzaG93LCBjbG9zZSwgd2lyZSB9Owp9KSgpOwo=', 'photos.js': 'LyogUGhvdG9zIGJhY2t1cCBwYWdlIGxvZ2ljLiBQdXJlIChubyBET00sIG5vIGdsb2JhbHMpOiBhcHAuanMgd2lyZXMgaXQgdG8gdGhlIHBhZ2U7IHRlc3RzIGRyaXZlIGl0IGRpcmVjdGx5LgogKiBUaGUgYmFja3VwIHdvcmtlciBsaXZlcyBpbiB0aGUgV2l6YXJkIE9TIEFuZHJvaWQgYXBwLiBUaGlzIGFwcCByZWFjaGVzIGl0IHRocm91Z2ggdGhlIHNoZWxsJ3MgUGhvdG9zIHJlbGF5OgogKiAgIGFwcCDihpIgc2hlbGwge3R5cGU6J3dpemFyZC1vczpwaG90b3MnLCBpZCwgbWV0aG9kLCBwYXJhbXN9OyBzaGVsbCDihpIgYXBwIHt0eXBlOid3aXphcmQtb3M6cGhvdG9zJywgaWQsIG9rLCByZXN1bHR8ZXJyb3J9OwogKiAgIHNoZWxsIHB1c2gge3R5cGU6J3dpemFyZC1vczpwaG90b3MtcHVzaCcsIHB1c2g6e3R5cGU6J3N0YXR1cycsIHN0YXR1c30gfCB7dHlwZToncmVzdW1lJ319LgogKiBLZXlzIGFuZCBzaWduLWluIHN0YXRlIG5ldmVyIHJlYWNoIHRoaXMgcGFnZTsgc3RhdHVzIGlzIGEgZml4ZWQgc2V0IG9mIGZpZWxkcyAoUGhvdG9zQnJpZGdlUG9saWN5LnN0YXR1cykuICovCihmdW5jdGlvbiAocm9vdCkgewogICd1c2Ugc3RyaWN0JzsKICBjb25zdCBWRVJTSU9OID0gMSwgVCA9ICd3aXphcmQtb3M6cGhvdG9zJywgUFVTSCA9ICd3aXphcmQtb3M6cGhvdG9zLXB1c2gnLCBERVRFQ1RfTVMgPSAxNTAwLCBDQUxMX01TID0gMTUwMDA7CiAgY29uc3QgZXNjID0gcyA9PiBTdHJpbmcocyA/PyAnJykucmVwbGFjZSgvWyY8PiInXS9nLCBjID0+ICh7ICcmJzogJyZhbXA7JywgJzwnOiAnJmx0OycsICc+JzogJyZndDsnLCAnIic6ICcmcXVvdDsnLCAiJyI6ICcmIzM5OycgfVtjXSkpOwoKICAvKiogQ2FsbHMgb3ZlciBwb3N0TWVzc2FnZSB0byB0aGUgaG9zdGluZyBzaGVsbC4gYHBvc3QobXNnKWAgc2VuZHM7IGZlZWQgc2hlbGwgbWVzc2FnZXMgdG8gYG9uTWVzc2FnZShkYXRhKWAuICovCiAgZnVuY3Rpb24gY2xpZW50KHBvc3QsIHsgdGltZW91dCA9IENBTExfTVMsIG9uU3RhdHVzID0gKCkgPT4ge30sIG9uUmVzdW1lID0gKCkgPT4ge30sIHNldCA9IHNldFRpbWVvdXQsIGNsZWFyID0gY2xlYXJUaW1lb3V0IH0gPSB7fSkgewogICAgY29uc3Qgd2FpdGluZyA9IG5ldyBNYXAoKTsgbGV0IHNlcSA9IDA7CiAgICBmdW5jdGlvbiBvbk1lc3NhZ2UoZCkgewogICAgICBpZiAoIWQgfHwgdHlwZW9mIGQgIT09ICdvYmplY3QnKSByZXR1cm47CiAgICAgIGlmIChkLnR5cGUgPT09IFBVU0ggJiYgZC5wdXNoKSB7CiAgICAgICAgaWYgKGQucHVzaC50eXBlID09PSAnc3RhdHVzJyAmJiBkLnB1c2guc3RhdHVzICYmIGQucHVzaC5zdGF0dXMudiA9PT0gVkVSU0lPTikgb25TdGF0dXMoZC5wdXNoLnN0YXR1cyk7CiAgICAgICAgZWxzZSBpZiAoZC5wdXNoLnR5cGUgPT09ICdyZXN1bWUnKSBvblJlc3VtZSgpOwogICAgICAgIHJldHVybjsKICAgICAgfQogICAgICBpZiAoZC50eXBlICE9PSBUKSByZXR1cm47CiAgICAgIGNvbnN0IHcgPSB3YWl0aW5nLmdldChkLmlkKTsgaWYgKCF3KSByZXR1cm47CiAgICAgIHdhaXRpbmcuZGVsZXRlKGQuaWQpOyBjbGVhcih3LnRpbWVyKTsKICAgICAgaWYgKGQub2spIHsgaWYgKGQucmVzdWx0ICYmIHR5cGVvZiBkLnJlc3VsdCA9PT0gJ29iamVjdCcgJiYgZC5yZXN1bHQudiA9PT0gVkVSU0lPTikgb25TdGF0dXMoZC5yZXN1bHQpOyB3LnJlc29sdmUoZC5yZXN1bHQpOyB9CiAgICAgIGVsc2Ugdy5yZWplY3QoT2JqZWN0LmFzc2lnbihuZXcgRXJyb3IoU3RyaW5nKGQuZXJyb3IgfHwgJ1NvbWV0aGluZyB3ZW50IHdyb25nJykpLCB7IGNvZGU6IFN0cmluZyhkLmVycm9yIHx8ICcnKSB9KSk7CiAgICB9CiAgICBmdW5jdGlvbiBjYWxsKG1ldGhvZCwgcGFyYW1zID0ge30pIHsKICAgICAgcmV0dXJuIG5ldyBQcm9taXNlKChyZXNvbHZlLCByZWplY3QpID0+IHsKICAgICAgICBjb25zdCBpZCA9ICdwaCcgKyAoKytzZXEpOwogICAgICAgIGNvbnN0IHRpbWVyID0gc2V0KCgpID0+IHsgd2FpdGluZy5kZWxldGUoaWQpOyByZWplY3QoT2JqZWN0LmFzc2lnbihuZXcgRXJyb3IoJ05vIGFuc3dlciBmcm9tIHRoZSBXaXphcmQgT1MgYXBwJyksIHsgdGltZW91dDogdHJ1ZSB9KSk7IH0sIHRpbWVvdXQpOwogICAgICAgIHdhaXRpbmcuc2V0KGlkLCB7IHJlc29sdmUsIHJlamVjdCwgdGltZXIgfSk7CiAgICAgICAgdHJ5IHsgcG9zdCh7IHR5cGU6IFQsIGlkLCBtZXRob2QsIHBhcmFtcyB9KTsgfQogICAgICAgIGNhdGNoIChlKSB7IHdhaXRpbmcuZGVsZXRlKGlkKTsgY2xlYXIodGltZXIpOyByZWplY3QoZSk7IH0KICAgICAgfSk7CiAgICB9CiAgICByZXR1cm4gewogICAgICBjYWxsLCBvbk1lc3NhZ2UsCiAgICAgIHN0YXR1czogKCkgPT4gY2FsbCgnc3RhdHVzJyksCiAgICAgIHNldEVuYWJsZWQ6IChvbiwgc2VydmVyKSA9PiBjYWxsKCdzZXRFbmFibGVkJywgc2VydmVyID8geyBvbjogISFvbiwgc2VydmVyIH0gOiB7IG9uOiAhIW9uIH0pLAogICAgICBzZXRPcHRpb246IChrZXksIG9uKSA9PiBjYWxsKCdzZXRPcHRpb24nLCB7IGtleSwgb246ICEhb24gfSksCiAgICAgIGNvbm5lY3Q6IHNlcnZlciA9PiBjYWxsKCdjb25uZWN0Jywgc2VydmVyID8geyBzZXJ2ZXIgfSA6IHt9KSwKICAgICAgcnVuTm93OiAoKSA9PiBjYWxsKCdydW5Ob3cnKSwKICAgICAgcGVybWlzc2lvbnM6ICgpID0+IGNhbGwoJ3Blcm1pc3Npb25zJyksCiAgICAgIGFsYnVtczogKCkgPT4gY2FsbCgnYWxidW1zJyksCiAgICAgIHNldEFsYnVtczogaWRzID0+IGNhbGwoJ3NldEFsYnVtcycsIHsgaWRzOiBbLi4uaWRzXSB9KSwKICAgICAgZGlzY29ubmVjdDogKCkgPT4gY2FsbCgnZGlzY29ubmVjdCcpLAogICAgICBvcGVuTmF0aXZlOiAoKSA9PiBjYWxsKCdvcGVuJyksCiAgICB9OwogIH0KCiAgLyoqIEZpcnN0IGNvbnRhY3Q6IHYxIHN0YXR1cyDihpIgJ3YxJzsgJ25vLWJyaWRnZScgKGJyb3dzZXIsIGRlc2t0b3ApIOKGkiAnbm9uZSc7IHNpbGVuY2UgKG9sZCBhcHApIOKGkiAnbGVnYWN5Jy4gKi8KICBmdW5jdGlvbiBkZXRlY3QoYXBpLCB7IHdhaXQgPSBERVRFQ1RfTVMsIHNldCA9IHNldFRpbWVvdXQgfSA9IHt9KSB7CiAgICByZXR1cm4gUHJvbWlzZS5yYWNlKFthcGkuc3RhdHVzKCkudGhlbigoKSA9PiAndjEnLCBlID0+IGUuY29kZSA9PT0gJ25vLWJyaWRnZScgPyAnbm9uZScgOiBlLnRpbWVvdXQgPyAnbGVnYWN5JyA6ICd2MS1lcnJvcicpLAogICAgICBuZXcgUHJvbWlzZShyID0+IHNldCgoKSA9PiByKCdsZWdhY3knKSwgd2FpdCkpXSk7CiAgfQoKICBmdW5jdGlvbiBhZ28obXMsIG5vdyA9IERhdGUubm93KCkpIHsKICAgIGlmICghbXMpIHJldHVybiAnbm90IGJhY2tlZCB1cCB5ZXQnOwogICAgY29uc3QgbWluID0gTWF0aC5tYXgoMCwgTWF0aC5mbG9vcigobm93IC0gbXMpIC8gNjAwMDApKTsKICAgIGlmIChtaW4gPCAxKSByZXR1cm4gJ2xhc3QgYmFja3VwIGp1c3Qgbm93JzsKICAgIGlmIChtaW4gPCA2MCkgcmV0dXJuIGBsYXN0IGJhY2t1cCAke21pbn0gbWluIGFnb2A7CiAgICBjb25zdCBoID0gTWF0aC5mbG9vcihtaW4gLyA2MCk7IGlmIChoIDwgMjQpIHJldHVybiBgbGFzdCBiYWNrdXAgJHtofSBoIGFnb2A7CiAgICBjb25zdCBkID0gTWF0aC5mbG9vcihoIC8gMjQpOyByZXR1cm4gYGxhc3QgYmFja3VwICR7ZH0gJHtkID09PSAxID8gJ2RheScgOiAnZGF5cyd9IGFnb2A7CiAgfQogIC8qKiBPbmUgbGluZSBmb3IgdGhlIFNldHRpbmdzIHJvdyBhbmQgdGhlIHBhZ2U6IHdoYXQgaXMgdHJ1ZSByaWdodCBub3cuICovCiAgZnVuY3Rpb24gc3VtbWFyeShzLCBub3cgPSBEYXRlLm5vdygpKSB7CiAgICBpZiAoIXMpIHJldHVybiAnQ2hlY2tpbmfigKYnOwogICAgaWYgKHMuc2lnbmluZ0luKSByZXR1cm4gJ0NvbmZpcm0gd2l0aCB5b3VyIGZpbmdlcnByaW50IGluIFdpemFyZCBTaWduLWluJzsKICAgIGlmICghcy5lbmFibGVkKSByZXR1cm4gcy5jb25uZWN0ZWQgPyAnT2ZmJyA6ICdPZmYgwrcgdHVybmluZyBpdCBvbiBzaWducyB5b3UgaW4gd2l0aCB5b3VyIGZpbmdlcnByaW50JzsKICAgIGlmIChzLm5lZWRzQWNjZXNzKSByZXR1cm4gJ09uIMK3IHdhaXRpbmcgZm9yIHBob3RvIGFjY2Vzcyc7CiAgICBpZiAoIXMudG90YWwgJiYgIXMubGFzdFN5bmMpIHJldHVybiAnT24gwrcgZmlyc3QgYmFja3VwIHdhaXRpbmcnOwogICAgcmV0dXJuIGBPbiDCtyAke3MudG90YWwgPyBgJHtzLmRvbmV9IG9mICR7cy50b3RhbH0gYmFja2VkIHVwYCA6ICdub3RoaW5nIGJhY2tlZCB1cCB5ZXQnfSDCtyAke2FnbyhzLmxhc3RTeW5jLCBub3cpfWA7CiAgfQogIC8qKiBXaHkgYSBxdWV1ZWQgYmFja3VwIGhhcyBub3Qgc3RhcnRlZDogQW5kcm9pZCB3YWl0cyBmb3IgdGhlc2UgYmVmb3JlIGl0IHJ1bnMgdGhlIGpvYi4gKi8KICBmdW5jdGlvbiB3YWl0aW5nRm9yKHMpIHsKICAgIGNvbnN0IHcgPSBbXTsKICAgIGlmIChzLndpZmkpIHcucHVzaCgnV2ktRmknKTsKICAgIGlmIChzLmNoYXJnZSAmJiAhcy5sYXN0U3luYykgdy5wdXNoKCdhIGNoYXJnZXIgKGZpcnN0IGJhY2t1cCBvbmx5IHJ1bnMgd2hpbGUgcGx1Z2dlZCBpbiknKTsKICAgIHJldHVybiB3Lmxlbmd0aCA/IGBCYWNrdXAgcXVldWVkIMK3IEFuZHJvaWQgc3RhcnRzIGl0IG9uY2UgaXQgc2VlcyAke3cuam9pbignIGFuZCAnKX1gIDogJ0JhY2t1cCBxdWV1ZWQgwrcgc3RhcnRpbmfigKYnOwogIH0KICAvKiogVGhlIHNlcnZlciBmaWVsZCBzaG93cyBvbmx5IHdoZW4gbm90aGluZyBpcyBrbm93bjogbm8gYnVpbGQtdGltZSBzZXJ2ZXIgYW5kIG5vdCBzaWduZWQgaW4uICovCiAgY29uc3QgbmVlZHNTZXJ2ZXIgPSBzID0+ICEhcyAmJiAhcy5jb25uZWN0ZWQgJiYgIXMuc2VydmVyOwoKICBjb25zdCBzdyA9IChhdHRyLCBvbiwgdGl0bGUsIHN1YiwgZGlzYWJsZWQpID0+IGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLXJvdyIgJHthdHRyfSByb2xlPSJzd2l0Y2giIGFyaWEtY2hlY2tlZD0iJHshIW9ufSIke2Rpc2FibGVkID8gJyBkaXNhYmxlZCcgOiAnJ30+PHNwYW4gY2xhc3M9InBoLXR4Ij48Yj4ke2VzYyh0aXRsZSl9PC9iPiR7c3ViID8gYDxzbWFsbD4ke2VzYyhzdWIpfTwvc21hbGw+YCA6ICcnfTwvc3Bhbj48c3BhbiBjbGFzcz0ic3cgJHtvbiA/ICdvbicgOiAnJ30iPjxpPjwvaT48L3NwYW4+PC9idXR0b24+YDsKCiAgLyoqIFRoZSB3aG9sZSBwYWdlIGJvZHkgZm9yIG9uZSBzdGF0ZS4gUHVyZTogdGVzdHMgcmVuZGVyIGl0IGRpcmVjdGx5LiAqLwogIGZ1bmN0aW9uIGh0bWwoc3QsIG5vdyA9IERhdGUubm93KCkpIHsKICAgIGlmIChzdC5tb2RlID09PSAnbGVnYWN5JykgcmV0dXJuIGA8ZGl2IGNsYXNzPSJwaCI+PHAgY2xhc3M9InBoLWludHJvIj5UaGlzIFdpemFyZCBPUyBhcHAgcHJlZGF0ZXMgdGhlIFBob3RvcyBhcHAuIEl0cyBiYWNrdXAgc2NyZWVuIHN0aWxsIHdvcmtzLjwvcD48YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLXJvdyIgZGF0YS1waD0ibmF0aXZlIj48c3BhbiBjbGFzcz0icGgtdHgiPjxiPlBob3RvcyBiYWNrdXA8L2I+PHNtYWxsPk9yaWdpbmFscyBiYWNrZWQgdXAgdG8geW91ciBwaG90byBzZXJ2ZXI8L3NtYWxsPjwvc3Bhbj48c3BhbiBjbGFzcz0icGgtZ28iPuKAujwvc3Bhbj48L2J1dHRvbj48L2Rpdj5gOwogICAgaWYgKHN0Lm1vZGUgPT09ICdub25lJykgcmV0dXJuIGA8ZGl2IGNsYXNzPSJwaCI+PHAgY2xhc3M9InBoLXN0YXR1cyIgZGF0YS1waC1zdGF0dXM+UGhvdG8gYmFja3VwIHJ1bnMgb24geW91ciBwaG9uZS48L3A+PHAgY2xhc3M9InBoLWludHJvIj5PcGVuIFBob3RvcyBpbiB0aGUgV2l6YXJkIE9TIEFuZHJvaWQgYXBwIHRvIGJhY2sgdXAgeW91ciBjYW1lcmEgcm9sbCB0byB5b3VyIHBob3RvIHNlcnZlci48L3A+PC9kaXY+YDsKICAgIGNvbnN0IHMgPSBzdC5zdGF0dXM7CiAgICBpZiAoIXMpIHJldHVybiBgPGRpdiBjbGFzcz0icGgiPjxwIGNsYXNzPSJwaC1zdGF0dXMiIGRhdGEtcGgtc3RhdHVzPiR7ZXNjKHN0LmVycm9yIHx8ICdDaGVja2luZ+KApicpfTwvcD48L2Rpdj5gOwogICAgY29uc3QgYnVzeSA9ICEhc3QuYnVzeSB8fCAhIXMuc2lnbmluZ0luOwogICAgY29uc3QgcGFydHMgPSBbYDxkaXYgY2xhc3M9InBoIiBhcmlhLWJ1c3k9IiR7YnVzeX0iPmAsCiAgICAgIGA8cCBjbGFzcz0icGgtaW50cm8iPk9yaWdpbmFsIHBob3RvcyBhbmQgdmlkZW9zIGdvIHN0cmFpZ2h0IHRvIHlvdXIgcGhvdG8gc2VydmVyLiBOb3RoaW5nIGlzIGRlbGV0ZWQgZnJvbSB0aGlzIHBob25lLjwvcD5gLAogICAgICBzdygnZGF0YS1waD0iZW5hYmxlZCInLCBzLmVuYWJsZWQsICdCYWNrIHVwIHBob3RvcycsIG51bGwsIGJ1c3kpXTsKICAgIGlmIChuZWVkc1NlcnZlcihzKSkgcGFydHMucHVzaChgPGxhYmVsIGNsYXNzPSJwaC1maWVsZCI+PHNwYW4+UGhvdG8gc2VydmVyPC9zcGFuPjxpbnB1dCB0eXBlPSJ1cmwiIGlucHV0bW9kZT0idXJsIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im9mZiIgc3BlbGxjaGVjaz0iZmFsc2UiIHBsYWNlaG9sZGVyPSJodHRwczovL3Bob3Rvcy5leGFtcGxlLmNvbSIgZGF0YS1waC1zZXJ2ZXIgdmFsdWU9IiR7ZXNjKHN0LnNlcnZlciB8fCAnJyl9Ij48L2xhYmVsPmApOwogICAgcGFydHMucHVzaChgPHAgY2xhc3M9InBoLXN0YXR1cyIgZGF0YS1waC1zdGF0dXMgcm9sZT0ic3RhdHVzIj4ke2VzYyhzdC5lcnJvciB8fCBzdW1tYXJ5KHMsIG5vdykpfTwvcD5gKTsKICAgIGNvbnN0IGZyZXNoID0gc3QucXVldWVkQXQgJiYgIXN0LmVycm9yICYmICghcy5sYXN0U3luYyB8fCBzLmxhc3RTeW5jIDwgc3QucXVldWVkQXQpOwogICAgaWYgKGZyZXNoKSBwYXJ0cy5wdXNoKGA8cCBjbGFzcz0icGgtbm90ZSBwaC1xdWV1ZWQiIHJvbGU9InN0YXR1cyI+JHtlc2Mod2FpdGluZ0ZvcihzKSl9JHtzLndpZmkgPyAnIMK3IHN0dWNrIHdoaWxlIHBsdWdnZWQgaW4gb24gV2ktRmk/IFR1cm4gb2ZmIOKAnFdpLUZpIG9ubHnigJ0gKHRoZSBwcml2YXRlIG5ldHdvcmsgY2FuIGxvb2sgbGlrZSBtb2JpbGUgZGF0YSB0byBBbmRyb2lkKScgOiAnJ308L3A+YCk7CiAgICBpZiAocy5lbmFibGVkICYmIHMubWVzc2FnZSAmJiAhc3QuZXJyb3IpIHBhcnRzLnB1c2goYDxwIGNsYXNzPSJwaC1ub3RlIj5QaG9uZSBzYXlzOiAke2VzYyhzLm1lc3NhZ2UpfTwvcD5gKTsKICAgIGlmIChzLm5lZWRzQWNjZXNzKSBwYXJ0cy5wdXNoKGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biBwcmltYXJ5IiBkYXRhLXBoPSJwZXJtaXNzaW9ucyI+QWxsb3cgcGhvdG8gYWNjZXNzPC9idXR0b24+YCk7CiAgICBpZiAocy5saW1pdGVkKSBwYXJ0cy5wdXNoKGA8cCBjbGFzcz0icGgtbm90ZSI+T25seSB0aGUgcGhvdG9zIHlvdSBwaWNrZWQgaW4gQW5kcm9pZCBhcmUgdmlzaWJsZS4gPGJ1dHRvbiB0eXBlPSJidXR0b24iIGNsYXNzPSJwaC1saW5rIiBkYXRhLXBoPSJwZXJtaXNzaW9ucyI+QWxsb3cgYWxsIHBob3RvczwvYnV0dG9uPjwvcD5gKTsKICAgIHBhcnRzLnB1c2goc3coJ2RhdGEtcGg9IndpZmkiJywgcy53aWZpLCAnV2ktRmkgb25seScsIHMud2lmaSA/ICdNb2JpbGUgZGF0YSBpcyBuZXZlciB1c2VkJyA6ICdNb2JpbGUgZGF0YSBtYXkgYmUgdXNlZCcsIGJ1c3kpKTsKICAgIHBhcnRzLnB1c2goc3coJ2RhdGEtcGg9ImNoYXJnZSInLCBzLmNoYXJnZSwgJ0ZpcnN0IGJhY2t1cCBvbmx5IHdoaWxlIGNoYXJnaW5nJywgbnVsbCwgYnVzeSkpOwogICAgcGFydHMucHVzaChgPGJ1dHRvbiB0eXBlPSJidXR0b24iIGNsYXNzPSJwaC1yb3ciIGRhdGEtcGg9ImFsYnVtcyIgYXJpYS1leHBhbmRlZD0iJHshIXN0LmFsYnVtc09wZW59Ij48c3BhbiBjbGFzcz0icGgtdHgiPjxiPkFsYnVtczwvYj48c21hbGw+JHtlc2MoYWxidW1TdWIoc3QpKX08L3NtYWxsPjwvc3Bhbj48c3BhbiBjbGFzcz0icGgtZ28iPiR7c3QuYWxidW1zT3BlbiA/ICfijIQnIDogJ+KAuid9PC9zcGFuPjwvYnV0dG9uPmApOwogICAgaWYgKHN0LmFsYnVtc09wZW4pIHBhcnRzLnB1c2goYWxidW1zSHRtbChzdCkpOwogICAgY29uc3QgYWN0cyA9IFtdOwogICAgaWYgKHMuZW5hYmxlZCkgYWN0cy5wdXNoKGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biIgZGF0YS1waD0icnVuIiR7YnVzeSA/ICcgZGlzYWJsZWQnIDogJyd9PiR7c3QuYnVzeSA9PT0gJ3J1bicgPyAnU3RhcnRpbmfigKYnIDogc3QucXVldWVkQXQgJiYgZnJlc2ggPyAnUXVldWVkIOKckyDCtyB0YXAgdG8gcmV0cnknIDogJ0JhY2sgdXAgbm93J308L2J1dHRvbj5gKTsKICAgIGlmIChzLmNvbm5lY3RlZCkgYWN0cy5wdXNoKGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biIgZGF0YS1waD0ic2lnbmluIiR7YnVzeSA/ICcgZGlzYWJsZWQnIDogJyd9PlNpZ24gaW4gYWdhaW48L2J1dHRvbj5gKTsKICAgIGlmIChzLmNvbm5lY3RlZCkgYWN0cy5wdXNoKHN0LmNvbmZpcm1EaXNjb25uZWN0CiAgICAgID8gYDxidXR0b24gdHlwZT0iYnV0dG9uIiBjbGFzcz0icGgtYnRuIGRhbmdlciIgZGF0YS1waD0iZGlzY29ubmVjdC15ZXMiPkRpc2Nvbm5lY3QgdGhpcyBwaG9uZTwvYnV0dG9uPjxidXR0b24gdHlwZT0iYnV0dG9uIiBjbGFzcz0icGgtYnRuIiBkYXRhLXBoPSJkaXNjb25uZWN0LW5vIj5LZWVwPC9idXR0b24+YAogICAgICA6IGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWJ0biBnaG9zdCIgZGF0YS1waD0iZGlzY29ubmVjdCI+RGlzY29ubmVjdDwvYnV0dG9uPmApOwogICAgaWYgKGFjdHMubGVuZ3RoKSBwYXJ0cy5wdXNoKGA8ZGl2IGNsYXNzPSJwaC1hY3RzIj4ke2FjdHMuam9pbignJyl9PC9kaXY+YCk7CiAgICBpZiAoc3QuY29uZmlybURpc2Nvbm5lY3QpIHBhcnRzLnB1c2goYDxwIGNsYXNzPSJwaC1ub3RlIj5CYWNrdXAgc3RvcHMgYW5kIHRoaXMgcGhvbmUgZm9yZ2V0cyBpdHMga2V5LiBQaG90b3MgYWxyZWFkeSBvbiB0aGUgc2VydmVyIHN0YXkuPC9wPmApOwogICAgaWYgKHMuc2VydmVyKSBwYXJ0cy5wdXNoKGA8cCBjbGFzcz0icGgtc2VydmVyIj4ke2VzYyhzLnNlcnZlcil9PC9wPmApOwogICAgcGFydHMucHVzaCgnPC9kaXY+Jyk7CiAgICByZXR1cm4gcGFydHMuam9pbignJyk7CiAgfQogIGZ1bmN0aW9uIGFsYnVtU3ViKHN0KSB7CiAgICBjb25zdCBsaXN0ID0gc3QuYWxidW1zPy5hbGJ1bXM7CiAgICBpZiAoIWxpc3QpIHJldHVybiAnQ2FtZXJhIGFuZCBTY3JlZW5zaG90cyB1bmxlc3MgeW91IGNob29zZSc7CiAgICBjb25zdCBuID0gbGlzdC5maWx0ZXIoYSA9PiBhLnNlbGVjdGVkKS5sZW5ndGg7CiAgICByZXR1cm4gbiA/IGAke259IG9mICR7bGlzdC5sZW5ndGh9IGNob3NlbmAgOiAnTm9uZSBjaG9zZW4gwrcgbm90aGluZyBpcyBiYWNrZWQgdXAnOwogIH0KICBmdW5jdGlvbiBhbGJ1bXNIdG1sKHN0KSB7CiAgICBpZiAoc3QuYWxidW1zRXJyb3IpIHJldHVybiBgPHAgY2xhc3M9InBoLW5vdGUiPiR7ZXNjKHN0LmFsYnVtc0Vycm9yKX08L3A+YDsKICAgIGNvbnN0IGxpc3QgPSBzdC5hbGJ1bXM/LmFsYnVtczsgaWYgKCFsaXN0KSByZXR1cm4gJzxwIGNsYXNzPSJwaC1ub3RlIj5Mb2FkaW5nIGFsYnVtc+KApjwvcD4nOwogICAgaWYgKCFsaXN0Lmxlbmd0aCkgcmV0dXJuICc8cCBjbGFzcz0icGgtbm90ZSI+Tm8gYWxidW1zIHZpc2libGUgeWV0LjwvcD4nOwogICAgcmV0dXJuIGA8ZGl2IGNsYXNzPSJwaC1hbGJ1bXMiIHJvbGU9Imdyb3VwIiBhcmlhLWxhYmVsPSJBbGJ1bXMgdG8gYmFjayB1cCI+JHtsaXN0Lm1hcChhID0+IGA8YnV0dG9uIHR5cGU9ImJ1dHRvbiIgY2xhc3M9InBoLWFsYnVtIiByb2xlPSJjaGVja2JveCIgYXJpYS1jaGVja2VkPSIkeyEhYS5zZWxlY3RlZH0iIGRhdGEtcGgtYWxidW09IiR7ZXNjKGEuaWQpfSI+PHNwYW4gY2xhc3M9InBoLWNoZWNrIiBhcmlhLWhpZGRlbj0idHJ1ZSI+PC9zcGFuPiR7ZXNjKGEubmFtZSl9PC9idXR0b24+YCkuam9pbignJyl9PC9kaXY+YDsKICB9CgogIC8qKiBUYXBzIOKGkiBicmlkZ2UgY2FsbHMuIFJldHVybnMgdGhlIHByb21pc2Ugb2YgdGhlIGNhbGwgaXQgbWFkZSAob3IgbnVsbCkuIFB1cmUgb3ZlciAoc3RhdGUsIGNsaWVudCkuICovCiAgZnVuY3Rpb24gYWN0KHN0LCBhcGksIHdoYXQsIGV4dHJhID0ge30pIHsKICAgIGNvbnN0IHMgPSBzdC5zdGF0dXM7IGlmICghcykgcmV0dXJuIG51bGw7CiAgICBjb25zdCBzZXJ2ZXIgPSBuZWVkc1NlcnZlcihzKSA/IFN0cmluZyhleHRyYS5zZXJ2ZXIgPz8gc3Quc2VydmVyID8/ICcnKS50cmltKCkgOiAnJzsKICAgIHN3aXRjaCAod2hhdCkgewogICAgICBjYXNlICdlbmFibGVkJzoKICAgICAgICBpZiAoIXMuZW5hYmxlZCAmJiBuZWVkc1NlcnZlcihzKSAmJiAhc2VydmVyKSB7IHN0LmVycm9yID0gJ0VudGVyIHlvdXIgcGhvdG8gc2VydmVyIGFkZHJlc3MgZmlyc3QnOyByZXR1cm4gbnVsbDsgfQogICAgICAgIHJldHVybiBhcGkuc2V0RW5hYmxlZCghcy5lbmFibGVkLCBzZXJ2ZXIpOwogICAgICBjYXNlICd3aWZpJzogcmV0dXJuIGFwaS5zZXRPcHRpb24oJ3dpZmknLCAhcy53aWZpKTsKICAgICAgY2FzZSAnY2hhcmdlJzogcmV0dXJuIGFwaS5zZXRPcHRpb24oJ2NoYXJnZScsICFzLmNoYXJnZSk7CiAgICAgIGNhc2UgJ3Blcm1pc3Npb25zJzogcmV0dXJuIGFwaS5wZXJtaXNzaW9ucygpOwogICAgICBjYXNlICdydW4nOiByZXR1cm4gYXBpLnJ1bk5vdygpLnRoZW4ociA9PiB7IHN0LnF1ZXVlZEF0ID0gRGF0ZS5ub3coKTsgcmV0dXJuIHI7IH0pOwogICAgICBjYXNlICdzaWduaW4nOiByZXR1cm4gYXBpLmNvbm5lY3Qoc2VydmVyKTsKICAgICAgY2FzZSAnZGlzY29ubmVjdCc6IHN0LmNvbmZpcm1EaXNjb25uZWN0ID0gdHJ1ZTsgcmV0dXJuIG51bGw7CiAgICAgIGNhc2UgJ2Rpc2Nvbm5lY3Qtbm8nOiBzdC5jb25maXJtRGlzY29ubmVjdCA9IGZhbHNlOyByZXR1cm4gbnVsbDsKICAgICAgY2FzZSAnZGlzY29ubmVjdC15ZXMnOiBzdC5jb25maXJtRGlzY29ubmVjdCA9IGZhbHNlOyBzdC5hbGJ1bXMgPSBudWxsOyBzdC5hbGJ1bXNPcGVuID0gZmFsc2U7IHJldHVybiBhcGkuZGlzY29ubmVjdCgpOwogICAgICBjYXNlICdhbGJ1bXMnOgogICAgICAgIHN0LmFsYnVtc09wZW4gPSAhc3QuYWxidW1zT3Blbjsgc3QuYWxidW1zRXJyb3IgPSAnJzsKICAgICAgICBpZiAoIXN0LmFsYnVtc09wZW4pIHJldHVybiBudWxsOwogICAgICAgIHJldHVybiBhcGkuYWxidW1zKCkudGhlbihyID0+IHsgc3QuYWxidW1zID0gcjsgcmV0dXJuIHI7IH0sIGUgPT4geyBzdC5hbGJ1bXNFcnJvciA9IGUubWVzc2FnZTsgdGhyb3cgZTsgfSk7CiAgICAgIGNhc2UgJ2FsYnVtJzogewogICAgICAgIGNvbnN0IGxpc3QgPSBzdC5hbGJ1bXM/LmFsYnVtczsgaWYgKCFsaXN0KSByZXR1cm4gbnVsbDsKICAgICAgICBjb25zdCBhID0gbGlzdC5maW5kKHggPT4geC5pZCA9PT0gZXh0cmEuaWQpOyBpZiAoIWEpIHJldHVybiBudWxsOwogICAgICAgIGEuc2VsZWN0ZWQgPSAhYS5zZWxlY3RlZDsKICAgICAgICByZXR1cm4gYXBpLnNldEFsYnVtcyhsaXN0LmZpbHRlcih4ID0+IHguc2VsZWN0ZWQpLm1hcCh4ID0+IHguaWQpKTsKICAgICAgfQogICAgfQogICAgcmV0dXJuIG51bGw7CiAgfQoKICBjb25zdCBsaWIgPSB7IHdhaXRpbmdGb3IsIFZFUlNJT04sIFQsIFBVU0gsIGNsaWVudCwgZGV0ZWN0LCBzdW1tYXJ5LCBodG1sLCBhY3QsIGFnbywgbmVlZHNTZXJ2ZXIsIGVzYyB9OwogIGlmICh0eXBlb2YgbW9kdWxlID09PSAnb2JqZWN0JyAmJiBtb2R1bGUuZXhwb3J0cykgbW9kdWxlLmV4cG9ydHMgPSBsaWI7IGVsc2Ugcm9vdC5QaG90b3NQYWdlID0gbGliOwp9KSh0eXBlb2Ygd2luZG93ICE9PSAndW5kZWZpbmVkJyA/IHdpbmRvdyA6IGdsb2JhbFRoaXMpOwo='}


if __name__ == "__main__":
    asyncio.run(main())
