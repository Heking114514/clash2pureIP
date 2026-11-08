#!/usr/bin/env python3
"""Clash subscription converter with proxy pool management.

- Convert a Clash subscription URL into per-node HTTP proxy listeners.
- Keep a persistent pool of proxy URLs with add/remove.
- Test any proxy URL against chatgpt.com via CONNECT.
"""

import base64
import concurrent.futures
import datetime
import hmac
import json
import os
import secrets
import subprocess
import time
import urllib.request

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

from converter import (EXPORT_FORMATS, ConvertError, build_config,
                       parse_subscription, restart_mihomo, wait_for_port)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLASH_DATA", os.path.join(BASE_DIR, "data"))
MIHOMO_DIR = os.path.join(DATA_DIR, "mihomo")
CONFIG_PATH = os.path.join(MIHOMO_DIR, "config.yaml")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
POOL_PATH = os.path.join(DATA_DIR, "pool.json")
RESTART_CMD = os.environ.get("MIHOMO_RESTART_CMD", "systemctl restart mihomo-conv")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "") or LISTEN_HOST

# Subscription sites whitelist client UAs behind Cloudflare; try real
# Clash-family UAs first and fall back to a browser UA.
FETCH_UAS = [
    "ClashMeta/1.19.0",
    "mihomo/1.19.0",
    "clash-verge/1.7.7",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]

TEST_URL = "https://www.google.com/generate_204"
TEST_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
TEST_TIMEOUT = 18
TEST_WORKERS = 8


def _proxy_auth() -> tuple[str, str]:
    """Per-listener proxy credentials; generated once and persisted."""
    user = (os.environ.get("PROXY_USER", "hjh") or "hjh").strip()
    password = (os.environ.get("PROXY_PASS", "") or "").strip()
    if password:
        return user, password
    path = os.path.join(DATA_DIR, "proxy_auth.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("password"):
            return data.get("user") or user, data["password"]
    except (OSError, json.JSONDecodeError):
        pass
    password = secrets.token_urlsafe(12)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"user": user, "password": password}, fh)
    return user, password


PROXY_USER, PROXY_PASS = _proxy_auth()


def _secret() -> str:
    value = os.environ.get("SECRET_KEY", "")
    if value:
        return value
    path = os.path.join(DATA_DIR, "secret_key")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    value = secrets.token_hex(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value


app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7))


def credentials():
    return os.environ.get("WEBUI_USER", ""), os.environ.get("WEBUI_PASS", "")


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


def load_pool() -> list:
    try:
        with open(POOL_PATH, encoding="utf-8") as fh:
            pool = json.load(fh)
        return pool if isinstance(pool, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_pool(pool: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(POOL_PATH, "w", encoding="utf-8") as fh:
        json.dump(pool, fh, ensure_ascii=False, indent=1)


@app.before_request
def require_login():
    if request.path in ("/login", "/healthz") or request.path.startswith("/static/"):
        return None
    user, password = credentials()
    if not user or not password:
        return None
    auth = request.authorization
    if auth and auth.username is not None and auth.password is not None:
        if (hmac.compare_digest(auth.username, user)
                and hmac.compare_digest(auth.password, password)):
            return None
    if session.get("user") == user:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "auth required"}), 401
    return redirect(url_for("login_page", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    user, password = credentials()
    if not user or not password:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        given = request.form.get("password") or ""
        if hmac.compare_digest(username, user) and hmac.compare_digest(given, password):
            session.permanent = True
            session["user"] = username
            target = request.form.get("next") or "/"
            if not target.startswith("/") or target.startswith("//"):
                target = "/"
            return redirect(target)
        return render_template("login.html", error="账号或密码不对", username=username), 401
    return render_template("login.html", error=None, username="")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.get("/")
def index():
    return render_template("index.html", user=session.get("user", ""))


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


def fetch_subscription(url: str) -> str:
    last_error = None
    for ua in FETCH_UAS:
        try:
            request_ = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(request_, timeout=30) as response:
                code = response.status
                text = response.read().decode("utf-8", "replace")
            if code == 200 and text.strip():
                return text
            last_error = f"HTTP {code} (UA {ua[:20]})"
        except Exception as exc:
            last_error = f"{exc} (UA {ua[:20]})"
    raise ConvertError(f"拉取订阅失败: {last_error}")


CACHED_SUB_PATH = os.path.join(DATA_DIR, "last-subscription.yaml")
CACHED_SUB_MAX_AGE = 72 * 3600  # seconds


def load_cached_subscription(max_age: float = CACHED_SUB_MAX_AGE):
    """Return cached subscription text if it exists and is fresh enough."""
    try:
        mtime = os.path.getmtime(CACHED_SUB_PATH)
    except OSError:
        return None
    if max_age is not None and time.time() - mtime > max_age:
        return None
    try:
        with open(CACHED_SUB_PATH, encoding="utf-8") as fh:
            text = fh.read()
        return text if text.strip() else None
    except OSError:
        return None


@app.post("/api/upload-sub")
def api_upload_sub():
    """Receive subscription text pushed from a machine that can fetch it
    (the source site blocks datacenter IPs, so the server often cannot)."""
    auth = request.authorization
    user, password = credentials()
    token = os.environ.get("UPLOAD_TOKEN", "")
    ok = False
    if user and password and auth and auth.username and auth.password:
        ok = (hmac.compare_digest(auth.username, user)
              and hmac.compare_digest(auth.password, password))
    if not ok and token:
        ok = hmac.compare_digest(request.headers.get("X-Upload-Token", ""), token)
    if not ok:
        return jsonify({"ok": False, "error": "auth required"}), 401
    text = request.get_data(as_text=True)
    if not text.strip():
        return jsonify({"ok": False, "error": "empty body"}), 400
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHED_SUB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, CACHED_SUB_PATH)
    return jsonify({"ok": True, "bytes": len(text),
                    "cached_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})


@app.get("/api/sub-cache")
def api_sub_cache():
    try:
        mtime = os.path.getmtime(CACHED_SUB_PATH)
        return jsonify({"ok": True, "exists": True,
                        "cached_at": datetime.datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
                        "age_hours": round((time.time() - mtime) / 3600, 1)})
    except OSError:
        return jsonify({"ok": True, "exists": False})


@app.get("/api/status")
def api_status():
    state = load_state()
    return jsonify({
        "ok": True,
        "configured": bool(state.get("entries")),
        "nodes": len(state.get("entries") or []),
        "converted_at": state.get("converted_at"),
        "port_start": state.get("port_start"),
        "source": state.get("source"),
        "entries": state.get("entries") or [],
        "push": state.get("push") or {},
        "listen_host": LISTEN_HOST,
        "public_host": PUBLIC_HOST,
        "proxy_user": PROXY_USER,
        "proxy_password": PROXY_PASS,
        "formats": [{"key": key, "label": label} for key, label in EXPORT_FORMATS],
    })


@app.post("/api/convert")
def api_convert():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "请填 http(s) 订阅链接"}), 400
    try:
        port_start = int(payload.get("port_start") or 21001)
    except ValueError:
        return jsonify({"ok": False, "error": "起始端口不是数字"}), 400
    if not 1025 <= port_start <= 65000:
        return jsonify({"ok": False, "error": "起始端口需在 1025-65000"}), 400

    try:
        text = None
        fetch_note = ""
        try:
            text = fetch_subscription(url)
            source_used = "direct"
        except ConvertError as direct_exc:
            cached = load_cached_subscription()
            if cached:
                text = cached
                source_used = "cache"
                fetch_note = f"直连失败（{str(direct_exc)[:80]}），已使用本机推送的缓存"
            else:
                raise
        proxies = parse_subscription(text)
        config_text, entries = build_config(
            proxies, port_start, listen_host=LISTEN_HOST, public_host=PUBLIC_HOST,
            auth_user=PROXY_USER, auth_password=PROXY_PASS)
        os.makedirs(MIHOMO_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write(config_text)
        restart_mihomo(RESTART_CMD)
        ready = wait_for_port(entries[0]["port"]) if entries else False
    except ConvertError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    state = load_state()
    state.update({
        "source": url,
        "source_used": source_used,
        "fetch_note": fetch_note,
        "port_start": port_start,
        "entries": entries,
        "converted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    save_state(state)
    return jsonify({"ok": True, "nodes": len(entries), "ready": ready,
                    "source_used": source_used, "fetch_note": fetch_note,
                    "port_start": port_start, "entries": entries})


# ---------------------------------------------------------------- pool ----

def _normalize_urls(raw) -> list:
    if isinstance(raw, str):
        candidates = raw.splitlines()
    elif isinstance(raw, list):
        candidates = [str(item) for item in raw]
    else:
        return []
    seen, result = set(), []
    for line in candidates:
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if not url.startswith(("http://", "https://", "socks5://")):
            url = "http://" + url
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


@app.get("/api/pool")
def api_pool():
    return jsonify({"ok": True, "pool": load_pool()})


@app.post("/api/pool/add")
def api_pool_add():
    payload = request.get_json(silent=True) or {}
    urls = _normalize_urls(payload.get("urls"))
    if not urls:
        return jsonify({"ok": False, "error": "没有可添加的地址"}), 400
    pool = load_pool()
    existing = {item["url"] for item in pool}
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    added = 0
    for url in urls:
        if url in existing:
            continue
        existing.add(url)
        pool.append({"url": url, "name": (payload.get("name") or "").strip(),
                     "added_at": now, "last_status": None,
                     "last_latency": None, "last_tested_at": None})
        added += 1
    save_pool(pool)
    return jsonify({"ok": True, "added": added, "skipped": len(urls) - added,
                    "total": len(pool)})


@app.post("/api/pool/remove")
def api_pool_remove():
    payload = request.get_json(silent=True) or {}
    urls = set(_normalize_urls(payload.get("urls")))
    pool = load_pool()
    if payload.get("all"):
        removed = len(pool)
        save_pool([])
        return jsonify({"ok": True, "removed": removed, "total": 0})
    kept = [item for item in pool if item["url"] not in urls]
    removed = len(pool) - len(kept)
    save_pool(kept)
    return jsonify({"ok": True, "removed": removed, "total": len(kept)})


def _classify(code, headers: str, body: str) -> tuple[str, str]:
    """Return (verdict, human message).

    verdict: ok | challenge | blocked | error
    Target is Google's generate_204, so a healthy proxy returns 204.
    """
    low_headers = headers.lower()
    low_body = body.lower()
    if code in ("200", "204"):
        return "ok", "可连通 Google"
    if "cf-mitigated: challenge" in low_headers:
        return "challenge", "Cloudflare 人机校验"
    if code == "403":
        return "blocked", "被目标站拒绝（403）"
    if code and code != "000":
        return "error", f"异常状态码 {code}"
    return "error", "连接失败（无响应）"


def _test_one(url: str) -> dict:
    headers_path = os.path.join(DATA_DIR, f".testh-{os.getpid()}-{abs(hash(url))}.txt")
    body_path = os.path.join(DATA_DIR, f".testb-{os.getpid()}-{abs(hash(url))}.txt")
    try:
        completed = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", str(TEST_TIMEOUT), "--proxy", url,
             "-A", TEST_UA, "-D", headers_path, "-o", body_path,
             "-w", "%{http_code}|%{time_total}", TEST_URL],
            capture_output=True, text=True, timeout=TEST_TIMEOUT + 5)
        if completed.returncode == 0:
            code, _, latency = completed.stdout.strip().partition("|")
            try:
                with open(headers_path, encoding="utf-8", errors="replace") as fh:
                    headers = fh.read()
            except OSError:
                headers = ""
            try:
                with open(body_path, encoding="utf-8", errors="replace") as fh:
                    body = fh.read(4096)
            except OSError:
                body = ""
            verdict, message = _classify(code, headers, body)
            return {"url": url, "ok": verdict == "ok", "verdict": verdict,
                    "message": message, "code": code, "latency": latency}
        return {"url": url, "ok": False, "verdict": "error", "code": None,
                "message": completed.stderr.strip()[:160] or f"curl {completed.returncode}",
                "error": completed.stderr.strip()[:160] or f"curl {completed.returncode}"}
    except Exception as exc:
        return {"url": url, "ok": False, "verdict": "error", "code": None,
                "message": str(exc)[:160], "error": str(exc)[:160]}
    finally:
        for path in (headers_path, body_path):
            try:
                os.remove(path)
            except OSError:
                pass


def _run_tests(urls: list) -> list:
    if not urls:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=TEST_WORKERS) as executor:
        return list(executor.map(_test_one, urls))


@app.post("/api/pool/test")
def api_pool_test():
    payload = request.get_json(silent=True) or {}
    urls = _normalize_urls(payload.get("urls"))
    pool = load_pool()
    if not urls:
        urls = [item["url"] for item in pool]
    if not urls:
        return jsonify({"ok": False, "error": "池为空"}), 400
    results = _run_tests(urls)
    by_url = {item["url"]: item for item in pool}
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for result in results:
        item = by_url.get(result["url"])
        if item is None:
            continue
        item["last_status"] = "ok" if result["ok"] else result.get("verdict", "fail")
        item["last_latency"] = result.get("latency")
        item["last_code"] = result.get("code")
        item["last_message"] = result.get("message")
        item["last_error"] = result.get("error")
        item["last_tested_at"] = now
    save_pool(pool)
    return jsonify({"ok": True, "results": results, "tested_at": now})


@app.post("/api/test")
def api_test():
    """Ad-hoc test of arbitrary proxy URLs without touching the pool."""
    payload = request.get_json(silent=True) or {}
    urls = _normalize_urls(payload.get("urls"))
    if not urls:
        return jsonify({"ok": False, "error": "没有可测试的地址"}), 400
    if len(urls) > 300:
        return jsonify({"ok": False, "error": "一次最多测试 300 条"}), 400
    return jsonify({"ok": True, "results": _run_tests(urls)})


@app.post("/api/push")
def api_push():
    payload = request.get_json(silent=True) or {}
    target = (payload.get("target") or "").strip().rstrip("/")
    user = payload.get("user") or ""
    password = payload.get("password") or ""
    label = (payload.get("label") or "clash").strip() or "clash"
    if not target.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "目标地址需要 http(s)"}), 400

    state = load_state()
    entries = state.get("entries") or []
    if not entries:
        return jsonify({"ok": False, "error": "还没有转换结果"}), 400
    lines = [entry["url"] for entry in entries]

    body = json.dumps({"text": "\n".join(lines), "scheme": "http", "label": label}).encode()
    headers = {"Content-Type": "application/json"}
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    request_ = urllib.request.Request(f"{target}/api/proxies/import",
                                      data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request_, timeout=30) as response:
            result = json.loads(response.read().decode())
    except Exception as exc:
        return jsonify({"ok": False, "error": f"推送失败: {exc}"}), 502

    state["push"] = {"target": target, "user": user, "label": label}
    save_state(state)
    return jsonify({"ok": True, "result": result})


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8090")), threaded=True)
