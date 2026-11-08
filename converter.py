"""Clash subscription -> importable local proxy list."""

from __future__ import annotations

import re
import socket
import subprocess
import time

import yaml

BAD_NAME = re.compile(
    r"剩余流量|套餐到期|过期|官网|订阅|traffic|expire|通知|邀请|返利|流量重置|"
    r"更新于|一元|yiyuan|重置|群|频道|telegram|tg群|电报", re.I)
BAD_SERVER = re.compile(r"^(127\.|0\.0\.0\.0$|localhost$)", re.I)
LISTEN_HOST = "127.0.0.1"


class ConvertError(Exception):
    pass


def parse_subscription(text: str) -> list[dict]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConvertError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(document, dict):
        raise ConvertError("不是 Clash 配置")
    proxies = [
        item for item in (document.get("proxies") or [])
        if isinstance(item, dict)
        and item.get("name")
        and item.get("type")
        and not BAD_NAME.search(str(item["name"]))
        and not BAD_SERVER.search(str(item.get("server", "")))
    ]
    if not proxies:
        raise ConvertError("订阅里没有可用节点（或全是流量/到期提示）")

    seen: dict[str, int] = {}
    result = []
    for node in proxies:
        name = str(node["name"])
        if name in seen:
            seen[name] += 1
            node = dict(node)
            node["name"] = f"{name}#{seen[name]}"
        else:
            seen[name] = 0
        result.append(node)
    return result


EXPORT_FORMATS = [
    ("host:port:user:pass", "hostname:port:username:password"),
    ("user:pass:host:port", "username:password:hostname:port"),
    ("user:pass@host:port", "username:password@hostname:port"),
    ("host:port@user:pass", "hostname:port@username:password"),
]


def _formats(host: str, port: int, user: str, password: str) -> dict:
    endpoint = f"{host}:{port}"
    if not (user and password):
        return {"url": f"http://{endpoint}", "host:port": endpoint}
    return {
        "url": f"http://{user}:{password}@{endpoint}",
        "host:port": endpoint,
        "host:port:user:pass": f"{endpoint}:{user}:{password}",
        "user:pass:host:port": f"{user}:{password}:{endpoint}",
        "user:pass@host:port": f"{user}:{password}@{endpoint}",
        "host:port@user:pass": f"{endpoint}@{user}:{password}",
    }


def build_config(proxies: list[dict], port_start: int = 21001,
                 limit: int = 300, listen_host: str = "127.0.0.1",
                 public_host: str = "", auth_user: str = "",
                 auth_password: str = "") -> tuple[str, list[dict]]:
    """listen_host is what mihomo binds; public_host is what the list advertises."""
    listen_host = (listen_host or "127.0.0.1").strip()
    public_host = (public_host or listen_host).strip()
    auth_user = (auth_user or "").strip()
    auth_password = auth_password or ""
    selected = proxies[:max(1, limit)]
    listeners = []
    entries = []
    for index, node in enumerate(selected):
        port = port_start + index
        listener = {
            "name": f"conv-{port}",
            "type": "http",
            "port": port,
            "listen": listen_host,
            "proxy": node["name"],
        }
        if auth_user and auth_password:
            listener["users"] = [{"username": auth_user, "password": auth_password}]
        listeners.append(listener)
        endpoint = f"{public_host}:{port}"
        if auth_user and auth_password:
            url = f"http://{auth_user}:{auth_password}@{endpoint}"
        else:
            url = f"http://{endpoint}"
        entries.append({
            "port": port,
            "name": node["name"],
            "url": url,
            "line": endpoint,
            "formats": _formats(public_host, port, auth_user, auth_password),
        })
    document = {
        "mixed-port": 0,
        "allow-lan": listen_host not in ("127.0.0.1", "localhost", "::1"),
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "proxies": selected,
        "listeners": listeners,
        "rules": ["MATCH,DIRECT"],
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False), entries


def restart_mihomo(command: str) -> None:
    if not command.strip():
        return
    completed = subprocess.run(command, shell=True, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ConvertError(f"重启 mihomo 失败: {completed.stderr.strip()[:300]}")


def wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((LISTEN_HOST, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False
