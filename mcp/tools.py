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


def _epoch(iso):
    try:
        return calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


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
    # The capture inbox (a box switch, wizard-os#696) was dropped too (wizard-os#949): forget its
    # batch state. Tags, albums and Wizard/Processed marks already in Immich are the owner's photos and stay.
    for name in ("review-albums.json", "capture.json"):
        try:
            os.remove(_state_path(name))
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
