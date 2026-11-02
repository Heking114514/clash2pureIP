#!/usr/bin/env python3
"""Deploy the isolated 10000-range converter over SSH/SFTP."""

from __future__ import annotations

import io
import os
import posixpath
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("DEPLOY_HOST", "").strip()
USER = os.environ.get("DEPLOY_USER", "root").strip()
LOCAL_ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/opt/clash-converter-10000"
OLD_ROOT = os.environ.get("OLD_ROOT", "").strip()
FILES = (
    "app.py",
    "converter.py",
    "requirements.txt",
    "README.md",
    ".env.example",
    "static/style.css",
    "templates/index.html",
    "templates/login.html",
    "deploy/clash-converter.service",
    "deploy/mihomo-conv.service",
)


def run(client: paramiko.SSHClient, command: str, timeout: int = 120) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    status = stdout.channel.recv_exit_status()
    return status, out, err


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{name} must be a single line")
    return value


def main() -> int:
    if not HOST:
        raise SystemExit("DEPLOY_HOST is required")
    password = required_env("SSH_PASS")
    webui_user = required_env("WEBUI_USER")
    webui_password = required_env("WEBUI_PASS")
    proxy_user = required_env("PROXY_USER")
    proxy_password = required_env("PROXY_PASS")
    public_host = os.environ.get("PUBLIC_HOST", HOST).strip() or HOST

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    try:
        status, out, err = run(client, f"mkdir -p {REMOTE_ROOT}/data/mihomo {REMOTE_ROOT}/static {REMOTE_ROOT}/templates {REMOTE_ROOT}/deploy")
        if status:
            raise RuntimeError(f"mkdir failed: {err}")

        sftp = client.open_sftp()
        try:
            for relative in FILES:
                local = LOCAL_ROOT / relative
                remote = posixpath.join(REMOTE_ROOT, relative)
                sftp.put(str(local), remote)
                print(f"UPLOADED {relative} {local.stat().st_size}")

            env_text = (
                f"WEBUI_USER={webui_user}\n"
                f"WEBUI_PASS={webui_password}\n"
                f"CLASH_DATA={REMOTE_ROOT}/data\n"
                "MIHOMO_RESTART_CMD=systemctl restart mihomo-conv-10000\n"
                "LISTEN_HOST=0.0.0.0\n"
                f"PUBLIC_HOST={public_host}\n"
                f"PROXY_USER={proxy_user}\n"
                f"PROXY_PASS={proxy_password}\n"
            )
            with sftp.open(f"{REMOTE_ROOT}/.env", "w") as handle:
                handle.write(env_text)
            sftp.chmod(f"{REMOTE_ROOT}/.env", 0o600)
        finally:
            sftp.close()

        setup_commands = [
            f"test -x {REMOTE_ROOT}/.venv/bin/gunicorn || python3 -m venv {REMOTE_ROOT}/.venv",
            f"{REMOTE_ROOT}/.venv/bin/pip install -q -r {REMOTE_ROOT}/requirements.txt",
        ]
        if OLD_ROOT:
            setup_commands.extend([
                f"test -f {REMOTE_ROOT}/data/secret_key || cp -a {OLD_ROOT}/data/secret_key {REMOTE_ROOT}/data/secret_key",
                f"test -f {REMOTE_ROOT}/data/state.json || cp -a {OLD_ROOT}/data/state.json {REMOTE_ROOT}/data/state.json",
                f"test -f {REMOTE_ROOT}/data/mihomo/config.yaml || cp -a {OLD_ROOT}/data/mihomo/config.yaml {REMOTE_ROOT}/data/mihomo/config.yaml",
            ])
        setup_commands.extend([
            f"cp {REMOTE_ROOT}/deploy/mihomo-conv.service /etc/systemd/system/mihomo-conv-10000.service",
            f"cp {REMOTE_ROOT}/deploy/clash-converter.service /etc/systemd/system/clash-converter.service",
            "systemctl daemon-reload",
            "systemctl enable --now mihomo-conv-10000.service",
            "systemctl restart mihomo-conv-10000.service",
            "systemctl restart clash-converter.service",
        ])
        for command in setup_commands:
            status, out, err = run(client, command, timeout=240)
            print(f"RUN {command} -> {status}")
            if out.strip():
                print(out.strip())
            if status:
                print(err.strip(), file=sys.stderr)
                raise RuntimeError(f"command failed: {command}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
