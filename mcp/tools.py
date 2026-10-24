# Home Assistant app tools: the agent reads and controls the owner's devices through Home Assistant.
# The engine behind this front is the unmodified official Home Assistant container. This front sets it up
# by itself (owner account, onboarding), keeps its sign-in in /ha/.wizard (0700, only this app reads it),
# serves the owner's Home page at /smart-home/ and the agent's tools at /mcp, and passes everything else
# to Home Assistant unchanged, so the full Home Assistant app still opens on this app's own address.
import hashlib
import hmac
import io
import re
import secrets
import shutil
import threading
import zipfile

STATE_DIR = os.environ.get("STATE_DIR", "/ha/.wizard")
COMPONENTS = os.environ.get("COMPONENTS_DIR", "/ha/custom_components")
CLIENT_ID = "http://smart-home/"
HA_PORT_PUBLIC = os.environ.get("APP_PORT", "8811")
PAGE = "/smart-home/"
_auth_lock = threading.Lock()
_token = {"access": "", "exp": 0}

# Xiaomi's official Home Assistant integration. Downloaded from Xiaomi's own GitHub release onto the owner's
# box only when the owner taps Connect Xiaomi (it is licensed for personal use; this app never ships it).
XIAOMI_VERSION = "v0.5.0"
XIAOMI_URL = "https://github.com/XiaoMi/ha_xiaomi_home/releases/download/v0.5.0/xiaomi_home.zip"
XIAOMI_SHA256 = "fa093245846a1118c4b1d751dbe1e6298ceeccc66734ac830c062091e09496e3"
XIAOMI_REGIONS = {"sg": "Singapore (Asia)", "us": "United States", "de": "Europe", "i2": "India", "ru": "Russia", "cn": "Mainland China"}

# Devices the agent may control: the entity's own domain is the only one it can call, never system domains.
CONTROL = {"light", "switch", "fan", "climate", "humidifier", "vacuum", "cover", "media_player", "camera", "select",
           "number", "button", "input_boolean", "scene", "siren", "water_heater", "lock", "valve", "lawn_mower",
           "remote", "alarm_control_panel", "text", "notify"}
HIDDEN_DOMAINS = {"update", "conversation", "zone", "sun", "person", "tts", "stt", "wake_word", "assist_satellite",
                  "todo", "backup", "event", "ai_task", "tag"}
HIDDEN_PREFIXES = ("sensor.sun_", "sensor.backup_")
NEEDS_CONFIRM = {("lock", "unlock"), ("lock", "open"), ("alarm_control_panel", "alarm_disarm"),
                 ("cover", "open_cover"), ("valve", "open_valve")}
TOGGLE = {"light", "switch", "fan", "humidifier", "input_boolean", "media_player", "siren"}
ENTITY_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}\Z")


def _box_only():
    """Agent tools are only for apps on this box (the agent through the Wizard hub), never for a request that
    came in through Tailscale Serve, a tunnel or a proxy."""
    h = REQUEST.get()
    if any(h.get(k) for k in ("tailscale-user-login", "x-forwarded-for", "forwarded", "cf-connecting-ip",
                              "cf-access-jwt-assertion", "x-real-ip")):
        raise PermissionError("the Home Assistant tools are only for apps on this box")


# ---------------- Home Assistant sign-in (set up automatically on first start) ----------------

def _state_path(name):
    return os.path.join(STATE_DIR, name)


def _read_json(name, default=None):
    try:
        with open(_state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(name, data):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = _state_path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _state_path(name))


def _form(path, fields, timeout=30):
    body = urllib.parse.urlencode(fields).encode()
    status, raw = http("POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"}, timeout=timeout)
    return json.loads(raw or b"{}")


def _ha_up():
    """Home Assistant's first-run steps, or [] once it is set up (it then removes that endpoint); None while starting."""
    try:
        status, raw = http("GET", "/api/onboarding", timeout=5)
        return json.loads(raw or b"[]")
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None
    except Exception:
        return None


def _setup():
    """Create the owner account and finish Home Assistant's first-run steps. Safe to call again."""
    steps = _ha_up()
    if steps is None:
        raise RuntimeError("Home Assistant is starting. Try again in a minute.")
    auth = _read_json("auth.json")
    if auth and auth.get("refresh_token"):
        return auth
    done = {s.get("step"): s.get("done") for s in steps if isinstance(s, dict)}
    if done.get("user") or not steps:
        raise RuntimeError("Home Assistant was set up outside this app, so its sign-in is unknown. "
                           "Reset the app's data to start over.")
    password = secrets.token_urlsafe(18)
    body = json.dumps({"client_id": CLIENT_ID, "name": "Owner", "username": "owner", "password": password,
                       "language": "en"}).encode()
    status, raw = http("POST", "/api/onboarding/users", body, {"Content-Type": "application/json"})
    code = json.loads(raw)["auth_code"]
    tok = _form("/auth/token", {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID})
    auth = {"username": "owner", "password": password, "refresh_token": tok["refresh_token"]}
    _write_json("auth.json", auth)
    _token.update(access=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 1800)) - 60)
    hdr = {"Authorization": "Bearer " + tok["access_token"], "Content-Type": "application/json"}
    for step, payload in (("core_config", {}), ("analytics", {}),
                          ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID})):
        try:
            http("POST", "/api/onboarding/" + step, json.dumps(payload).encode(), hdr)
        except urllib.error.HTTPError:
            pass  # already done
    return auth


def _access():
    with _auth_lock:
        if _token["access"] and time.time() < _token["exp"]:
            return _token["access"]
        auth = _setup()
        tok = _form("/auth/token", {"grant_type": "refresh_token", "refresh_token": auth["refresh_token"],
                                     "client_id": CLIENT_ID})
        _token.update(access=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 1800)) - 60)
        return _token["access"]


def _own():
    """The owner's own Home Assistant (address + long-lived token), when they chose to use theirs instead."""
    try:
        with open(_state_path("own.json")) as f:
            d = json.load(f)
        return d if d.get("url") and d.get("token") else None
    except (OSError, ValueError):
        return None


def _own_call(url, token, method, path, payload=None, timeout=30):
    import ssl
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=body, method=method,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            raw = r.read(8 << 20)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError("Your Home Assistant refused the access token. Make a new one and connect again.")
        raise RuntimeError("Home Assistant said %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Can't reach your Home Assistant at %s (%s). If it is at home, reach it through Tailscale "
                           "or Home Assistant Cloud." % (url, getattr(e, "reason", e)))
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return raw.decode("utf-8", "replace")


def own_connect(url, token):
    u = urllib.parse.urlsplit(str(url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("The address looks like http://192.168.1.20:8123 or https://xyz.ui.nabu.casa")
    url = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))
    token = str(token or "").strip()
    if not (20 <= len(token) <= 1000) or any(ch.isspace() for ch in token):
        raise ValueError("Paste the long-lived access token from your Home Assistant profile (Security tab).")
    info = _own_call(url, token, "GET", "/api/config", timeout=20)
    if not isinstance(info, dict) or "version" not in info:
        raise RuntimeError("That address answered, but not like Home Assistant.")
    _write_json("own.json", {"url": url, "token": token, "name": info.get("location_name") or "Home",
                             "version": info.get("version"), "connected_at": int(time.time())})
    return {"connected": True, "name": info.get("location_name") or "Home", "version": info.get("version")}


def own_disconnect():
    try:
        os.remove(_state_path("own.json"))
    except OSError:
        pass
    return {"own": False}


def ha_bytes(path, timeout=30):
    """Raw bytes (e.g. a camera snapshot) from the built-in or the owner's own Home Assistant."""
    own = _own()
    if own:
        import ssl
        req = urllib.request.Request(own["url"] + path, headers={"Authorization": "Bearer " + own["token"]})
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.read(10 << 20)
    status, raw = http("GET", path, None, {"Authorization": "Bearer " + _access()}, timeout=timeout)
    return raw


NO_VIDEO = ("This camera's brand encrypts its video so only the brand's own phone app can play it; Home Assistant "
            "(and so your Wizard) only gets its settings. To let your Wizard see it, a spare Android phone signed in "
            "to that app has to be connected to your Wizard. Until then, watch it live in the brand's app. Cameras "
            "that stream openly (most RTSP/ONVIF cameras, e.g. Reolink, Tapo, Amcrest) work directly.")


def cameras(devs=None):
    devs = devs if devs is not None else devices()
    video = [d for d in devs if d["kind"] == "camera"]
    with_video = {d["device"] or d["name"] for d in video}
    app_only = sorted({g["device"] for g in grouped(devs) if g["device"] not in with_video and (
        "camera" in g["device"].lower() or any(".camera" in x["entity_id"] or "_camera_" in x["entity_id"]
                                               for x in g["parts"]))})
    return video, app_only


def ha(method, path, payload=None, timeout=30):
    own = _own()
    if own:
        return _own_call(own["url"], own["token"], method, path, payload, timeout)
    body = None if payload is None else json.dumps(payload).encode()
    hdr = {"Authorization": "Bearer " + _access(), "Content-Type": "application/json"}
    try:
        status, raw = http(method, path, body, hdr, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            _token["access"] = ""
        raise RuntimeError("Home Assistant said %d: %s" % (e.code, detail))
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return raw.decode("utf-8", "replace")


# ---------------- devices ----------------

INDEX_TEMPLATE = ("{%- set ns = namespace(o=[]) -%}{%- for s in states -%}"
                  "{%- set ns.o = ns.o + [[s.entity_id, area_name(s.entity_id), device_attr(s.entity_id, 'name')]] -%}"
                  "{%- endfor -%}{{ ns.o | tojson }}")


def _index():
    try:
        rows = ha("POST", "/api/template", {"template": INDEX_TEMPLATE})
        rows = json.loads(rows) if isinstance(rows, str) else rows
        return {r[0]: (r[1] or "", r[2] or "") for r in rows or []}
    except Exception:
        return {}


def _visible(entity_id):
    domain = entity_id.split(".", 1)[0]
    return domain not in HIDDEN_DOMAINS and not entity_id.startswith(HIDDEN_PREFIXES)


def devices(room="", kind="", name=""):
    idx = _index()
    out = []
    for s in ha("GET", "/api/states") or []:
        eid = s.get("entity_id", "")
        if not _visible(eid):
            continue
        domain = eid.split(".", 1)[0]
        if kind and domain != kind:
            continue
        area, device = idx.get(eid, ("", ""))
        attrs = s.get("attributes") or {}
        label = attrs.get("friendly_name") or eid
        if room and room.lower() not in area.lower():
            continue
        if name and name.lower() not in (label + " " + eid + " " + device).lower():
            continue
        unit = attrs.get("unit_of_measurement") or ""
        out.append({"entity_id": eid, "name": label, "room": area, "device": device, "kind": domain,
                    "state": s.get("state"), "unit": unit, "controllable": domain in CONTROL,
                    "toggle": domain in TOGGLE})
    out.sort(key=lambda d: (d["room"] or "~", d["device"], d["name"]))
    return out


def grouped(items):
    """Home Assistant splits one device into many parts (power, mode, PM2.5, filter...). Group them back."""
    out, idx = [], {}
    for d in items:
        key = (d["room"], d["device"] or d["name"])
        if key not in idx:
            idx[key] = {"device": key[1], "room": d["room"], "parts": []}
            out.append(idx[key])
        part = dict(d)
        for k in ("device", "room"):
            part.pop(k)
        name = part["name"]
        if key[1] and name.lower().startswith(key[1].lower()):
            part["name"] = name[len(key[1]):].strip() or name
        idx[key]["parts"].append(part)
    return out


def _actions(domain):
    for d in ha("GET", "/api/services") or []:
        if d.get("domain") == domain:
            acts = []
            for name, svc in sorted((d.get("services") or {}).items()):
                fields = {k: (v.get("description") or v.get("name") or "")[:120]
                          for k, v in (svc.get("fields") or {}).items() if isinstance(v, dict) and "fields" not in v}
                acts.append({"action": name, "options": fields})
            return acts
    return []


def control(entity_id, action, options=None, confirmed=False):
    entity_id = str(entity_id or "").strip()
    action = str(action or "").strip()
    if not ENTITY_RE.match(entity_id):
        raise ValueError("entity_id looks like fan.living_room_purifier (from home_devices)")
    if not ACTION_RE.match(action):
        raise ValueError("action is a Home Assistant action name such as turn_on, start or select_option")
    domain = entity_id.split(".", 1)[0]
    if domain not in CONTROL:
        raise PermissionError("%s is a reading, not something to control" % entity_id)
    if (domain, action) in NEEDS_CONFIRM and not confirmed:
        raise PermissionError("%s.%s affects home security: ask the owner first, then call again with "
                              "confirmed=true" % (domain, action))
    data = dict(options or {}) if isinstance(options, dict) else {}
    data.pop("entity_id", None)
    data["entity_id"] = entity_id
    ha("POST", "/api/services/%s/%s" % (domain, action), data, timeout=30)
    time.sleep(1.5)
    s = ha("GET", "/api/states/" + entity_id) or {}
    return {"done": True, "entity_id": entity_id, "action": action, "state_now": s.get("state"),
            "note": "state_now is read back from the device a moment later; some devices take a few seconds."}


# ---------------- connections (Xiaomi first; any other brand through Home Assistant itself) ----------------

def _xiaomi_installed():
    try:
        with open(os.path.join(COMPONENTS, "xiaomi_home", ".wizard-version")) as f:
            return f.read().strip() == XIAOMI_VERSION
    except OSError:
        return False


def _install_xiaomi():
    with urllib.request.urlopen(XIAOMI_URL, timeout=120) as r:
        blob = r.read(20 << 20)
    if hashlib.sha256(blob).hexdigest() != XIAOMI_SHA256:
        raise RuntimeError("the Xiaomi integration download did not match its published checksum")
    target = os.path.join(COMPONENTS, "xiaomi_home")
    tmp = target + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for member in z.namelist():
            dest = os.path.realpath(os.path.join(tmp, member))
            if not dest.startswith(os.path.realpath(tmp) + os.sep):
                raise RuntimeError("unexpected file in the Xiaomi integration")
        z.extractall(tmp)
    with open(os.path.join(tmp, ".wizard-version"), "w") as f:
        f.write(XIAOMI_VERSION)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)


def _entries():
    try:
        return ha("GET", "/api/config/config_entries/entry") or []
    except Exception:
        return []


def status():
    own = _own()
    if own:
        out = {"own": True, "home": own.get("name"), "address": own["url"], "home_page": PAGE}
        try:
            devs = devices()
            out.update(ready=True, state="ready", connected=[{"brand": "Your Home Assistant", "integration": "own",
                       "state": "loaded"}], devices=len(grouped(devs)), parts=len(devs),
                       controllable=sum(1 for d in devs if d["controllable"]), xiaomi={"connected": False},
                       next="Using the owner's own Home Assistant. Use home_devices, then home_control.")
        except Exception as e:
            out.update(ready=False, state="unreachable", next=str(e))
        return out
    steps = _ha_up()
    if steps is None:
        return {"ready": False, "state": "starting", "next": "Home Assistant is starting. Check again in a minute."}
    try:
        _access()
    except Exception as e:
        return {"ready": False, "state": "setup_failed", "next": str(e)}
    # Only what the owner connected: Home Assistant's own built-in entries come from system/onboarding/import.
    connected = [{"brand": e.get("title") or e.get("domain"), "integration": e.get("domain"), "state": e.get("state")}
                 for e in _entries() if e.get("source") not in ("system", "onboarding", "import", "ignore")
                 and e.get("domain") not in ("sun", "backup", "go2rtc", "hassio")]
    xiaomi = [c for c in connected if c["integration"] == "xiaomi_home"]
    pending = _read_json("xiaomi_flow.json")
    devs = devices()
    out = {"ready": True, "state": "ready", "connected": connected, "devices": len(grouped(devs)), "parts": len(devs),
           "controllable": sum(1 for d in devs if d["controllable"]),
           "xiaomi": {"connected": bool(xiaomi), "sign_in_started": bool(pending and not xiaomi)},
           "home_page": PAGE, "own": False}
    if not connected:
        out["next"] = ("No devices connected yet. Ask the owner to open the Home Assistant app and pick their brand "
                       "(Xiaomi signs in right there), or choose \"My own Home Assistant\" if they already run one. "
                       "Sign-in is the owner's own step; you cannot do it for them. Any other brand: the app's "
                       "\"Other brands\" button opens Home Assistant's own setup.")
    else:
        out["next"] = "Use home_devices to see what is there, then home_control to act."
    video, app_only = cameras(devs)
    out["cameras"] = {"with_video": [d["name"] for d in video], "app_only": app_only}
    if app_only:
        out["cameras"]["app_only_note"] = NO_VIDEO
    return out


def _flow(flow_id, payload=None):
    if payload is None:
        return ha("GET", "/api/config/config_entries/flow/" + flow_id)
    return ha("POST", "/api/config/config_entries/flow/" + flow_id, payload, timeout=60)


def xiaomi_start(region="sg"):
    region = region if region in XIAOMI_REGIONS else "sg"
    if not _xiaomi_installed():
        _install_xiaomi()
        ha("POST", "/api/services/homeassistant/restart", {})
        _token["access"] = ""
        return {"state": "restarting", "message": "Getting Xiaomi ready. This takes about a minute."}
    old = _read_json("xiaomi_flow.json") or {}
    if old.get("flow_id"):
        try:
            ha("DELETE", "/api/config/config_entries/flow/" + old["flow_id"])
        except Exception:
            pass
    f = ha("POST", "/api/config/config_entries/flow", {"handler": "xiaomi_home"})
    if f.get("type") == "abort":
        if f.get("reason") in ("not_implemented", "unknown"):
            raise RuntimeError("Home Assistant has not loaded Xiaomi yet. Try again in a minute.")
        raise RuntimeError("Xiaomi setup stopped: %s" % f.get("reason"))
    fid = f["flow_id"]
    f = _flow(fid, {"eula": True})
    f = _flow(fid, {"cloud_server": region, "integration_language": "en",
                    "oauth_redirect_url": "http://homeassistant.local:8123", "network_detect_config": False})
    link = ((f.get("description_placeholders") or {}).get("link_left") or "")
    m = re.search(r'href="([^"]+)"', link)
    if f.get("type") != "progress" or not m:
        raise RuntimeError("Xiaomi setup did not offer a sign-in link (step %s)" % f.get("step_id"))
    url = m.group(1).replace("&amp;", "&")
    _write_json("xiaomi_flow.json", {"flow_id": fid, "region": region, "started": time.time()})
    return {"state": "sign_in", "sign_in_url": url}


def xiaomi_finish(pasted):
    pending = _read_json("xiaomi_flow.json") or {}
    fid = pending.get("flow_id")
    if not fid:
        raise RuntimeError("Tap Connect Xiaomi first.")
    u = urllib.parse.urlsplit(str(pasted or "").strip())
    m = re.match(r"^/api/webhook/(\d{1,40})\Z", u.path or "")
    q = urllib.parse.parse_qs(u.query)
    if not m or not q.get("code") or not q.get("state"):
        raise ValueError("Paste the whole address from the page Xiaomi sent you to (it contains /api/webhook/ and code=).")
    query = urllib.parse.urlencode({"code": q["code"][0][:600], "state": q["state"][0][:200]})
    try:
        http("GET", "/api/webhook/%s?%s" % (m.group(1), query), timeout=30)
    except urllib.error.HTTPError:
        pass
    f = {}
    for _ in range(40):
        f = _flow(fid)
        if f.get("type") != "progress":
            break
        time.sleep(1.5)
    if f.get("type") == "progress_done" or (f.get("type") == "form" and f.get("step_id") == "oauth"):
        f = _flow(fid, {})
    if f.get("type") == "form" and f.get("step_id") == "oauth_error":
        _write_json("xiaomi_flow.json", {})
        raise RuntimeError("Xiaomi did not accept that sign-in. Tap Connect Xiaomi and try once more.")
    if f.get("type") == "form" and f.get("step_id") == "homes_select":
        homes = []
        for field in f.get("data_schema") or []:
            if field.get("name") == "home_infos":
                opts = field.get("options") or {}
                homes = list(opts.keys()) if isinstance(opts, dict) else [o[0] for o in opts]
        rule = "room"
        for field in f.get("data_schema") or []:
            if field.get("name") == "area_name_rule":
                rule = field.get("default") or rule
        f = _flow(fid, {"home_infos": homes, "area_name_rule": rule, "advanced_options": False})
    if f.get("type") == "create_entry":
        _write_json("xiaomi_flow.json", {})
        return {"state": "connected", "title": f.get("title", "Xiaomi")}
    if f.get("type") == "abort":
        _write_json("xiaomi_flow.json", {})
        if f.get("reason") == "already_configured":
            return {"state": "connected", "title": "Xiaomi"}
        raise RuntimeError("Xiaomi setup stopped: %s" % f.get("reason"))
    errs = (f.get("errors") or {}).get("base")
    raise RuntimeError("Xiaomi setup needs another look (%s%s)." % (f.get("step_id"), ": " + errs if errs else ""))


# ---------------- agent tools ----------------

@tool("home_status", "Home Assistant app status (built in, or the owner's own Home Assistant): whether Home Assistant is ready, which brands are connected, "
      "how many devices there are, and the next step. Call it first. Camera video is not available to agents.")
def home_status():
    _box_only()
    return status()


@tool("home_devices", "List the owner's smart-home devices, grouped by physical device, each with its parts and their "
      "current state: power, mode, sensors (air quality, temperature, battery, filter). Filter by room, kind (light, fan, vacuum, sensor, "
      "switch, camera, select...) or part of a name.",
      {"room": {"type": "string", "description": "Part of a room name (optional)"},
       "kind": {"type": "string", "description": "Device kind, e.g. vacuum, fan, light, sensor (optional)"},
       "name": {"type": "string", "description": "Part of a device name (optional)"}})
def home_devices(room="", kind="", name=""):
    _box_only()
    found = grouped(devices(room, kind, name))
    return {"count": len(found), "devices": found,
            "next": "Each device lists its parts (power, mode, sensors) with their entity_id. home_device shows one "
                    "part's details and actions; home_control acts on a part."}


@tool("home_device", "One device in detail: its state, every attribute (mode, fan speed, battery, filter life, "
      "air quality...) and the actions it accepts with their options. Use before an action you have not used yet.",
      {"entity_id": {"type": "string", "description": "From home_devices, e.g. vacuum.robot_cleaner"}}, ["entity_id"])
def home_device(entity_id):
    _box_only()
    entity_id = str(entity_id or "").strip()
    if not ENTITY_RE.match(entity_id):
        raise ValueError("entity_id looks like vacuum.robot_cleaner (from home_devices)")
    s = ha("GET", "/api/states/" + entity_id)
    domain = entity_id.split(".", 1)[0]
    return {"entity_id": entity_id, "state": s.get("state"), "attributes": s.get("attributes"),
            "last_changed": s.get("last_changed"), "controllable": domain in CONTROL,
            "actions": _actions(domain) if domain in CONTROL else []}


@tool("home_camera", "See what a camera sees: returns a current snapshot picture when Home Assistant can read that "
      "camera's video. When the brand encrypts its video (e.g. Xiaomi), it explains in plain words what the owner "
      "needs instead; tell them that. Call it whenever the owner asks to see, show or check a camera.",
      {"name": {"type": "string", "description": "Part of the camera's name or room (optional; default the first camera)"}})
def home_camera(name=""):
    _box_only()
    import base64
    devs = devices()
    video, app_only = cameras(devs)
    q = str(name or "").lower()
    pick = [d for d in video if not q or q in (d["name"] + " " + d["device"] + " " + d["room"] + " " + d["entity_id"]).lower()]
    if pick:
        d = pick[0]
        data = ha_bytes("/api/camera_proxy/" + d["entity_id"])
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return {"_content": [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime},
                             {"type": "text", "text": "Snapshot from %s (%s), taken just now." % (d["name"], d["entity_id"])}]}
    names = [n for n in app_only if not q or q in n.lower()] or app_only
    if names:
        return {"video": False, "cameras": names, "why": NO_VIDEO}
    return {"video": False, "cameras": [], "why": "No cameras are connected to Home Assistant."}


@tool("home_control", "Control one device: turn on or off, start or dock a vacuum, set a purifier's mode or speed, "
      "pick an option. action is one of the device's actions from home_device (turn_on, turn_off, toggle, start, "
      "return_to_base, set_percentage, set_preset_mode, select_option...). options holds that action's settings. "
      "Unlocking, disarming or opening doors needs the owner's explicit yes: then pass confirmed=true.",
      {"entity_id": {"type": "string", "description": "From home_devices"},
       "action": {"type": "string", "description": "e.g. turn_on, start, return_to_base, select_option"},
       "options": {"type": "object", "description": "Settings for the action, e.g. {\"percentage\": 50} (optional)"},
       "confirmed": {"type": "boolean", "description": "Only after the owner said yes to a security action"}},
      ["entity_id", "action"])
def home_control(entity_id, action, options=None, confirmed=False):
    _box_only()
    return control(entity_id, action, options, bool(confirmed))


# ---------------- the owner's Home page (/smart-home/) ----------------

def _json(code, obj):
    return code, "application/json", json.dumps(obj).encode()


def _body(headers, body):
    if not headers.get("content-type", "").startswith("application/json"):
        raise PermissionError("send application/json")
    return json.loads(body or b"{}")


@route(PAGE)
def page_route(method, path, query, headers, body):
    sub = path[len(PAGE):] if path.startswith(PAGE) else ""
    if sub.startswith("api/") and headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return _json(403, {"error": "open Home Assistant from your Wizard"})
    try:
        if method == "GET" and sub in ("", "index.html"):
            return 200, "text/html; charset=utf-8", PAGE_HTML.encode()
        if method == "GET" and sub == "app.js":
            return 200, "text/javascript; charset=utf-8", PAGE_JS.encode()
        if method == "GET" and sub == "app.css":
            return 200, "text/css; charset=utf-8", PAGE_CSS.encode()
        if method == "GET" and sub == "healthz":
            return _json(200, {"ok": True, "app": APP, "version": VERSION, "home_assistant": _ha_up() is not None})
        if method == "GET" and sub == "api/status":
            s = status()
            s["regions"] = XIAOMI_REGIONS
            s["advanced_port"] = HA_PORT_PUBLIC
            return _json(200, s)
        if method == "GET" and sub == "api/devices":
            return _json(200, {"devices": devices()})
        if method == "GET" and sub == "api/login":
            a = _read_json("auth.json") or {}
            return _json(200, {"username": a.get("username", ""), "password": a.get("password", "")})
        if method == "POST" and sub == "api/control":
            d = _body(headers, body)
            if d.get("action") not in ("turn_on", "turn_off", "toggle"):
                return _json(400, {"error": "the page only turns things on and off"})
            return _json(200, control(d.get("entity_id"), d.get("action")))
        if method == "POST" and sub == "api/xiaomi/start":
            return _json(200, xiaomi_start(str(_body(headers, body).get("region") or "sg")))
        if method == "POST" and sub == "api/own/connect":
            d = _body(headers, body)
            return _json(200, own_connect(d.get("url"), d.get("token")))
        if method == "POST" and sub == "api/own/disconnect":
            _body(headers, body)
            return _json(200, own_disconnect())
        if method == "POST" and sub == "api/xiaomi/finish":
            return _json(200, xiaomi_finish(_body(headers, body).get("url")))
    except PermissionError as e:
        return _json(403, {"error": str(e)})
    except (ValueError, RuntimeError) as e:
        return _json(400, {"error": str(e)})
    except Exception as e:  # never a stack trace on the page
        return _json(502, {"error": "Home Assistant is not reachable right now (%s)." % type(e).__name__})
    return _json(404, {"error": "not found"})


PAGE_HTML = """<!doctype html>
<html lang="en" data-app="smart-home">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Home Assistant</title>
<script>try { if (window.parent !== window) document.documentElement.classList.add('wz-hosted'); } catch (e) { document.documentElement.classList.add('wz-hosted'); }</script>
<link rel="stylesheet" href="app.css">
<script src="/wizard-os-home/core/app-client.js" onerror="this.remove()"></script>
<script defer src="app.js"></script>
</head>
<body>
<div class="sh-app">
  <header class="sh-head"><h1>Home Assistant</h1><p id="line" class="sh-line">Loading…</p></header>
  <main class="sh-scroll">
    <section id="connect" class="sh-card" hidden>
      <h2>Connect your devices</h2>
      <div id="step1">
        <label class="sh-field"><span>Brand</span><select id="brand"><option value="">Choose your brand</option><option value="xiaomi">Xiaomi / Mi Home</option><option value="other">Another brand</option><option value="own">My own Home Assistant</option></select></label>
        <label class="sh-field" id="regionf" hidden><span>Xiaomi account region</span><select id="region"></select></label>
        <button id="start" class="sh-primary" type="button" hidden>Sign in to Xiaomi</button>
      </div>
      <div id="ownf" hidden>
        <label class="sh-field"><span>Your Home Assistant address</span><input id="ownurl" class="sh-input" type="url" inputmode="url" autocomplete="off" placeholder="http://192.168.1.20:8123"></label>
        <label class="sh-field"><span>Long-lived access token</span><input id="owntoken" class="sh-input" type="password" autocomplete="off" placeholder="Profile › Security › Create token"></label>
        <button id="owngo" class="sh-primary" type="button">Use my Home Assistant</button>
      </div>
      <div id="step2" hidden>
        <p class="sh-help">After you sign in, Xiaomi opens a page that won't load. Copy that page's address and paste it here.</p>
        <input id="paste" class="sh-input" type="url" inputmode="url" autocomplete="off" placeholder="http://homeassistant.local:8123/api/webhook/…">
        <button id="finish" class="sh-primary" type="button">Finish</button>
        <button id="again" class="sh-quiet" type="button">Open sign-in again</button>
      </div>
    </section>
    <section id="list"></section>
    <p id="empty" class="sh-empty" hidden>No devices yet.</p>
    <footer class="sh-foot"><a id="advanced" target="_blank" rel="noopener">Other brands ↗</a><button id="showlogin" class="sh-quiet" type="button">Home Assistant sign-in</button><button id="ownoff" class="sh-quiet" type="button" hidden>Stop using my own</button><span id="login" hidden></span></footer>
  </main>
  <p id="status" class="sh-status" role="status" aria-live="polite"></p>
</div>
</body>
</html>
"""

PAGE_CSS = """
:root { --ink: var(--wz-text, #f3efff); --ink-2: var(--wz-text-secondary, #b9b0d8); --ink-3: var(--wz-text-muted, #a59abf);
  --line: var(--wz-border, rgba(205,190,255,.24)); --accent: var(--wz-accent, #b99bff); --on-accent: var(--wz-on-accent, #160b32);
  --canvas: var(--wz-base, var(--wz-canvas, #0b0814)); --solid: color-mix(in srgb, var(--ink) 9%, var(--canvas));
  --solid-2: color-mix(in srgb, var(--ink) 16%, var(--canvas)); --danger: #ff8f8f;
  --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color-scheme: dark; }
html[data-color-scheme="light"] { color-scheme: light; --danger: #b42318; }
*, *::before, *::after { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font: 15px/1.4 var(--font); color: var(--ink); background: var(--canvas); -webkit-font-smoothing: antialiased; }
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; border: 0; background: none; padding: 0; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
[hidden] { display: none !important; }
.sh-app { display: flex; flex-direction: column; height: 100%; max-width: 680px; margin: 0 auto; }
.sh-head { flex: none; padding: 18px 14px 6px; }
.sh-head h1 { margin: 0; font-size: 26px; font-weight: 750; letter-spacing: -.02em; }
html.wz-hosted .sh-head h1 { display: none; }
.sh-line { margin: 2px 0 0; color: var(--ink-2); font-size: 14px; }
.sh-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 6px 14px 120px; }
.sh-card { background: var(--solid); border: 1px solid var(--line); border-radius: 16px; padding: 16px; margin: 6px 0 16px; }
.sh-card h2 { margin: 0 0 12px; font-size: 17px; }
.sh-field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; font-size: 13px; color: var(--ink-2); }
.sh-field select, .sh-input { width: 100%; min-height: 44px; padding: 0 12px; border-radius: 12px; border: 1px solid var(--line); background: var(--solid-2); color: var(--ink); }
.sh-input { margin-bottom: 10px; }
.sh-help { margin: 0 0 10px; color: var(--ink-2); font-size: 14px; }
.sh-primary { width: 100%; min-height: 48px; border-radius: 14px; background: var(--accent); color: var(--on-accent); font-weight: 700; }
.sh-primary:disabled { opacity: .55; cursor: progress; }
.sh-quiet { min-height: 40px; margin-top: 6px; padding: 0 12px; border-radius: 99px; border: 1px solid var(--line); background: var(--solid); color: var(--ink-2); font-size: 13px; }
.sh-room { margin: 14px 4px 6px; font-size: 13px; font-weight: 700; color: var(--ink-2); }
.sh-rows { list-style: none; margin: 0; padding: 0; background: var(--solid); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
.sh-row { display: flex; align-items: center; gap: 12px; min-height: 54px; padding: 8px 14px; }
.sh-row + .sh-row { border-top: 1px solid var(--line); }
.sh-main { flex: 1; min-width: 0; }
.sh-main b { display: block; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sh-main small { color: var(--ink-3); font-size: 13px; }
.sh-val { flex: none; color: var(--ink-2); font-size: 14px; }
.sh-sw { flex: none; width: 50px; height: 30px; border-radius: 99px; background: var(--solid-2); border: 1px solid var(--line); position: relative; }
.sh-sw::after { content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px; border-radius: 50%; background: var(--ink-3); transition: transform .15s; }
.sh-sw[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); }
.sh-sw[aria-pressed="true"]::after { transform: translateX(20px); background: var(--on-accent); }
.sh-sw:disabled { opacity: .5; }
.sh-dev + .sh-dev { border-top: 1px solid var(--line); }
.sh-dev summary { list-style: none; cursor: pointer; }
.sh-dev summary::-webkit-details-marker { display: none; }
.sh-dev summary.sh-nomore { cursor: default; }
.sh-more { list-style: none; margin: 0 0 6px; padding: 0 0 0 14px; }
.sh-more .sh-row { min-height: 44px; padding: 4px 14px 4px 0; font-size: 14px; color: var(--ink-2); }
.sh-more .sh-row + .sh-row { border-top: 1px solid var(--line); }
.sh-main small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sh-empty { text-align: center; color: var(--ink-3); padding: 30px 0; }
.sh-foot { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 18px 0 0; font-size: 13px; color: var(--ink-3); }
.sh-foot a { display: inline-flex; align-items: center; min-height: 40px; margin-top: 6px; padding: 0 12px; border-radius: 99px; border: 1px solid var(--line); background: var(--solid); color: var(--ink-2); text-decoration: none; }
.sh-foot span { width: 100%; font-family: ui-monospace, Menlo, monospace; color: var(--ink-2); overflow-wrap: anywhere; }
.sh-status { flex: none; margin: 0; padding: 0 18px 8px; color: var(--danger); font-size: 13px; }
.sh-status:empty { padding: 0; }
.sh-status.ok { color: var(--ink-2); }
"""

PAGE_JS = r"""'use strict';
(() => {
  const el = id => document.getElementById(id);
  let st = null, signInUrl = '';
  const say = (t, ok) => { el('status').textContent = t || ''; el('status').className = 'sh-status' + (ok ? ' ok' : ''); };
  async function api(path, data) {
    const r = await fetch('api/' + path, data ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)} : {cache: 'no-store'});
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.error || 'Home Assistant is not reachable right now.');
    return b;
  }
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  function shown(d) {
    if (d.state === 'unavailable') return 'Offline';
    if (d.state === 'unknown') return '–';
    return d.unit ? d.state + ' ' + d.unit : String(d.state).replace(/_/g, ' ');
  }
  const KEY = /pm2\.?5|air quality|battery level|filter life|temperature|humidity|mode(?![\s\S])/i;
  const known = d => d.state !== 'unknown' && d.state !== 'unavailable' && !/^\s*\*/.test(short(d));
  function short(d) {
    let n = String(d.name || '');
    if (d.device && n.toLowerCase().startsWith(d.device.toLowerCase())) n = n.slice(d.device.length);
    n = n.trim().split(/\s+/).filter((w, i, a) => i === 0 || w.toLowerCase() !== a[i - 1].toLowerCase()).join(' ');
    return n || d.name;
  }
  function mainOf(items) {
    return items.find(d => d.kind === 'vacuum') || items.find(d => d.kind === 'fan') ||
      items.find(d => d.kind === 'switch' && /(power|switch status)(?![\s\S])/i.test(d.name)) || null;
  }
  function control(d) {
    if (d.toggle && (d.state === 'on' || d.state === 'off')) {
      const b = node('button', 'sh-sw'); b.type = 'button'; b.setAttribute('aria-pressed', String(d.state === 'on'));
      b.setAttribute('aria-label', d.name); b.onclick = e => { e.preventDefault(); flip(d, b); }; return b;
    }
    return node('span', 'sh-val', shown(d));
  }
  function paintDevices(list) {
    const rooms = new Map();
    for (const d of list) {
      const r = d.room || 'Other', k = d.device || d.name;
      if (!rooms.has(r)) rooms.set(r, new Map());
      const devs = rooms.get(r); if (!devs.has(k)) devs.set(k, []); devs.get(k).push(d);
    }
    const frag = []; let count = 0;
    for (const [room, devs] of rooms) {
      frag.push(node('h3', 'sh-room', room));
      const ul = node('ul', 'sh-rows');
      for (const [name, items] of devs) {
        count++;
        const main = mainOf(items), live = items.filter(known);
        const keys = live.filter(d => d !== main && d.kind === 'sensor' && KEY.test(d.name)).slice(0, 3);
        const li = node('li', 'sh-dev'); const det = node('details'); const sum = node('summary', 'sh-row');
        const m = node('div', 'sh-main'); m.appendChild(node('b', '', name));
        const label = k => { const n = short(k);
          if (/battery/i.test(n)) return 'Battery'; if (/pm2/i.test(n)) return 'PM2.5'; if (/air quality/i.test(n)) return 'Air';
          if (/filter/i.test(n)) return 'Filter'; if (/mop/i.test(n)) return 'Mop'; if (/brush/i.test(n)) return 'Brush';
          return n.replace(/^environment\s+/i, ''); };
        const sub = keys.map(k => label(k) + ' ' + shown(k)).join(' · ');
        if (sub) m.appendChild(node('small', '', sub));
        sum.appendChild(m);
        if (main) sum.appendChild(control(main)); else if (!live.length) sum.appendChild(node('span', 'sh-val', 'Offline'));
        det.appendChild(sum);
        const more = node('ul', 'sh-more');
        for (const d of live) { if (d === main) continue; const r = node('li', 'sh-row'); r.appendChild(node('span', 'sh-main', short(d))); r.appendChild(control(d)); more.appendChild(r); }
        if (more.children.length) det.appendChild(more); else sum.classList.add('sh-nomore');
        li.appendChild(det); ul.appendChild(li);
      }
      frag.push(ul);
    }
    el('list').replaceChildren(...frag);
    if (st && st.ready) el('line').textContent = count ? count + (count === 1 ? ' device' : ' devices') : 'Nothing connected yet';
    el('empty').hidden = list.length > 0 || !st || !st.ready || !el('connect').hidden;
  }
  async function flip(d, b) {
    b.disabled = true;
    try { const r = await api('control', {entity_id: d.entity_id, action: d.state === 'on' ? 'turn_off' : 'turn_on'}); d.state = r.state_now; b.setAttribute('aria-pressed', String(r.state_now === 'on')); say(''); }
    catch (e) { say(e.message); } finally { b.disabled = false; }
  }
  async function load() {
    try {
      st = await api('status');
      const n = st.devices || 0;
      el('line').textContent = !st.ready ? (st.state === 'starting' ? 'Starting…' : 'Needs attention') : (n ? n + (n === 1 ? ' device' : ' devices') : 'Nothing connected yet');
      if (!st.ready) { say(st.next); return setTimeout(load, 8000); }
      el('connect').hidden = st.own || st.xiaomi.connected;
      el('ownoff').hidden = !st.own; el('advanced').hidden = el('showlogin').hidden = !!st.own;
      if (!el('region').options.length) for (const [k, v] of Object.entries(st.regions)) { const o = node('option', '', v); o.value = k; el('region').appendChild(o); }
      el('region').value = el('region').value || 'us';
      if (!st.own) el('advanced').href = location.protocol + '//' + location.hostname + ':' + st.advanced_port + '/';
      if (st.xiaomi.connected) say('');
      paintDevices((await api('devices')).devices);
    } catch (e) { say(e.message); setTimeout(load, 10000); }
  }
  async function start() {
    el('start').disabled = true; say('');
    try {
      let r = await api('xiaomi/start', {region: el('region').value});
      while (r.state === 'restarting') { say(r.message, true); await new Promise(f => setTimeout(f, 12000)); try { r = await api('xiaomi/start', {region: el('region').value}); } catch (e) { r = {state: 'restarting', message: 'Still getting Xiaomi ready…'}; } }
      signInUrl = r.sign_in_url; say('');
      el('step1').hidden = true; el('step2').hidden = false;
      window.open(signInUrl, '_blank', 'noopener');
    } catch (e) { say(e.message); } finally { el('start').disabled = false; }
  }
  async function finish() {
    el('finish').disabled = true; say('Connecting…', true);
    try { await api('xiaomi/finish', {url: el('paste').value}); el('paste').value = ''; el('step2').hidden = true; el('step1').hidden = false; say('Xiaomi connected.', true); await load(); }
    catch (e) { say(e.message); } finally { el('finish').disabled = false; }
  }
  el('brand').onchange = () => {
    const b = el('brand').value;
    el('regionf').hidden = el('start').hidden = b !== 'xiaomi';
    el('ownf').hidden = b !== 'own';
    if (b === 'other') { window.open(el('advanced').href, '_blank', 'noopener'); el('brand').value = ''; }
  };
  el('start').onclick = start;
  el('owngo').onclick = async () => {
    el('owngo').disabled = true; say('Connecting…', true);
    try { const r = await api('own/connect', {url: el('ownurl').value, token: el('owntoken').value}); el('owntoken').value = ''; say('Using ' + r.name + '.', true); await load(); }
    catch (e) { say(e.message); } finally { el('owngo').disabled = false; }
  };
  el('ownoff').onclick = async () => {
    if (el('ownoff').dataset.armed !== '1') { el('ownoff').dataset.armed = '1'; el('ownoff').textContent = 'Tap again to stop'; return; }
    el('ownoff').dataset.armed = ''; el('ownoff').textContent = 'Stop using my own';
    try { await api('own/disconnect', {}); await load(); } catch (e) { say(e.message); }
  };
  el('finish').onclick = finish;
  el('again').onclick = () => { if (signInUrl) window.open(signInUrl, '_blank', 'noopener'); };
  el('showlogin').onclick = async () => {
    try { const a = await api('login'); el('login').textContent = 'User ' + a.username + ' · password ' + a.password; el('login').hidden = false; el('showlogin').hidden = true; }
    catch (e) { say(e.message); }
  };
  window.WizardApp?.ready?.({name: 'Home Assistant'});
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  load();
})();
"""
