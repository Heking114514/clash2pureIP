#!/usr/bin/env python3
"""Clash subscription converter: subscription URL -> importable proxy list."""

import base64
import hmac
import json
import os
import secrets
import socket
import ssl
import time
import urllib.request
from datetime import timedelta

from flask import (Flask, jsonify, redirect, render_template, request, session, url_for)

from converter import (EXPORT_FORMATS, ConvertError, build_config,
                       parse_subscription, restart_mihomo, wait_for_port)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLASH_DATA", os.path.join(BASE_DIR, "data"))
MIHOMO_DIR = os.path.join(DATA_DIR, "mihomo")
CONFIG_PATH = os.path.join(MIHOMO_DIR, "config.yaml")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
RESTART_CMD = os.environ.get("MIHOMO_RESTART_CMD", "systemctl restart mihomo-conv")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "") or LISTEN_HOST
PORT_MIN = 10000
PORT_MAX = 10999


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
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))


def credentials():
    return os.environ.get("WEBUI_USER", ""), os.environ.get("WEBUI_PASS", "")


def proxy_credentials(payload: dict) -> tuple[str, str]:
    supplied_user = str(payload.get("proxy_user") or "").strip()
    supplied_password = payload.get("proxy_password")
    supplied_password = "" if supplied_password is None else str(supplied_password)
    if bool(supplied_user) != bool(supplied_password):
        raise ValueError("代理账号和代理密码需要同时填写，或同时留空")
    if not supplied_user:
        return PROXY_USER, PROXY_PASS

    for label, value in (("代理账号", supplied_user), ("代理密码", supplied_password)):
        if "\r" in value or "\n" in value:
            raise ValueError(f"{label}不能包含换行")
        if len(value.encode("utf-8")) > 255:
            raise ValueError(f"{label}的 UTF-8 长度不能超过 255 字节")
    return supplied_user, supplied_password


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("连接被提前关闭")
        data += chunk
    return data


def recv_head(sock: socket.socket, limit: int = 16384) -> bytes:
    data = b""
    while b"\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            break
    return data


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


@app.get("/api/status")
def api_status():
    state = load_state()
    return jsonify({
        "ok": True,
        "configured": bool(state.get("entries")),
        "nodes": len(state.get("entries") or []),
        "converted_at": state.get("converted_at"),
        "port_start": state.get("port_start"),
        "entries": state.get("entries") or [],
        "listen_host": LISTEN_HOST,
        "public_host": PUBLIC_HOST,
        "proxy_user": state.get("proxy_user") or PROXY_USER,
        "formats": [{"key": key, "label": label} for key, label in EXPORT_FORMATS],
    })


@app.get("/api/test")
def api_test():
    try:
        port = int(request.args.get("port") or 0)
    except ValueError:
        return jsonify({"ok": False, "error": "端口不是数字"}), 400
    scheme = (request.args.get("scheme") or "http").lower()
    if scheme not in ("http", "socks5", "socks5h"):
        return jsonify({"ok": False, "error": "测速协议必须是 http、socks5 或 socks5h"}), 400
    if not PORT_MIN <= port <= PORT_MAX:
        return jsonify({"ok": False, "error": f"端口必须在 {PORT_MIN}-{PORT_MAX}"}), 400

    state = load_state()
    entries = state.get("entries") or []
    if not any(int(entry.get("port", 0)) == port for entry in entries):
        return jsonify({"ok": False, "error": "该端口不是当前转换结果"}), 404
    test_user = state.get("proxy_user") or PROXY_USER
    test_password = state.get("proxy_password") or PROXY_PASS

    host = "127.0.0.1"
    target_host = "www.gstatic.com"
    target_path = "/generate_204"
    started = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=5)
        if scheme == "http":
            headers = [
                f"CONNECT {target_host}:443 HTTP/1.1",
                f"Host: {target_host}:443",
                "Connection: keep-alive",
            ]
            if test_user and test_password:
                auth = base64.b64encode(
                    f"{test_user}:{test_password}".encode()).decode()
                headers.append(f"Proxy-Authorization: Basic {auth}")
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
            response = recv_head(sock)
            first_line = response.split(b"\r\n", 1)[0].decode("latin1", "replace")
            if " 200 " not in first_line:
                raise RuntimeError(f"CONNECT 失败: {first_line or '无响应'}")
        else:
            methods = b"\x00\x02" if test_user and test_password else b"\x00"
            sock.sendall(b"\x05" + bytes((len(methods),)) + methods)
            greeting = recv_exact(sock, 2)
            if greeting[0] != 5 or greeting[1] == 0xFF:
                raise RuntimeError(f"SOCKS5 认证协商失败: {greeting.hex()}")
            if greeting[1] == 2:
                username = test_user.encode()
                password = test_password.encode()
                sock.sendall(
                    bytes((1, len(username))) + username
                    + bytes((len(password),)) + password)
                auth_reply = recv_exact(sock, 2)
                if auth_reply != b"\x01\x00":
                    raise RuntimeError(f"SOCKS5 认证失败: {auth_reply.hex()}")
            elif greeting[1] != 0:
                raise RuntimeError(f"SOCKS5 返回未知认证方式: {greeting[1]}")
            target = target_host.encode()
            request = b"\x05\x01\x00\x03" + bytes((len(target),)) + target + (443).to_bytes(2, "big")
            sock.sendall(request)
            reply = recv_exact(sock, 4)
            if reply[:2] != b"\x05\x00":
                raise RuntimeError(f"SOCKS5 CONNECT 失败: {reply.hex()}")
            atyp = reply[3]
            if atyp == 1:
                recv_exact(sock, 4)
            elif atyp == 4:
                recv_exact(sock, 16)
            elif atyp == 3:
                length = recv_exact(sock, 1)[0]
                recv_exact(sock, length)
            else:
                raise RuntimeError(f"SOCKS5 返回未知地址类型: {atyp}")
            recv_exact(sock, 2)

        context = ssl.create_default_context()
        tls = context.wrap_socket(sock, server_hostname=target_host)
        sock = tls
        tls.sendall((
            f"GET {target_path} HTTP/1.1\r\n"
            f"Host: {target_host}\r\n"
            "User-Agent: clash-converter-speedtest\r\n"
            "Connection: close\r\n\r\n"
        ).encode())
        status_line = tls.recv(4096).split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 204 " not in status_line:
            raise RuntimeError(f"HTTP 测速失败: {status_line or '无响应'}")
        delay_ms = round((time.perf_counter() - started) * 1000)
        return jsonify({"ok": True, "port": port, "scheme": scheme, "delay_ms": delay_ms})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


@app.post("/api/convert")
def api_convert():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "请填 http(s) 订阅链接"}), 400
    try:
        raw_port = payload.get("port_start")
        port_start = int(raw_port) if raw_port not in (None, "") else PORT_MIN
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "起始端口不是数字"}), 400
    if not PORT_MIN <= port_start <= PORT_MAX:
        return jsonify({"ok": False, "error": f"起始端口需在 {PORT_MIN}-{PORT_MAX}"}), 400
    try:
        auth_user, auth_password = proxy_credentials(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        request_ = urllib.request.Request(url, headers={"User-Agent": "clash.meta"})
        with urllib.request.urlopen(request_, timeout=30) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"拉取订阅失败: {exc}"}), 502

    try:
        proxies = parse_subscription(text)
        if port_start + len(proxies) - 1 > PORT_MAX:
            raise ConvertError(
                f"节点过多：从 {port_start} 开始会超过上限 {PORT_MAX}，最多允许 "
                f"{PORT_MAX - port_start + 1} 个节点")
        config_text, entries = build_config(
            proxies, port_start, limit=PORT_MAX - PORT_MIN + 1,
            listen_host=LISTEN_HOST, public_host=PUBLIC_HOST,
            auth_user=auth_user, auth_password=auth_password)
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
        "port_start": port_start,
        "proxy_user": auth_user,
        "proxy_password": auth_password,
        "entries": entries,
        "converted_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    save_state(state)
    return jsonify({"ok": True, "nodes": len(entries), "ready": ready,
                    "port_start": port_start, "proxy_user": auth_user,
                    "entries": entries})


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8090")), threaded=True)
