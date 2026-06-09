#!/usr/bin/env python3
"""
Deploy the app-chat final-image filtering fix to the production Docker Compose host.

Required environment variables:
  GROK_DEPLOY_PASSWORD or SSH_PASSWORD

Optional environment variables:
  GROK_DEPLOY_HOST       default: 111.230.202.235
  GROK_DEPLOY_USER       default: root
  GROK_DEPLOY_REMOTE_DIR default: /opt/grok2api-rs-fork/grok2api-python-stack
  GROK_DEPLOY_CREDS      default: /root/.grok2api-prod-credentials
"""

from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "grok2api-python-stack"

FILES = [
    (
        STACK / "vendor/grok2api/app/services/grok/services/chat.py",
        "vendor/grok2api/app/services/grok/services/chat.py",
    ),
    (
        STACK / "vendor/grok2api/app/services/grok/utils/process.py",
        "vendor/grok2api/app/services/grok/utils/process.py",
    ),
    (
        STACK / "vendor/grok2api/app/services/cf_refresh/solver.py",
        "vendor/grok2api/app/services/cf_refresh/solver.py",
    ),
    (
        STACK / "vendor/grok2api/app/services/reverse/app_chat.py",
        "vendor/grok2api/app/services/reverse/app_chat.py",
    ),
    (
        STACK / "vendor/grok2api/app/services/grok/services/image_edit.py",
        "image_edit_override.py",
    ),
]

COMPOSE_MOUNTS = [
    "      - ./vendor/grok2api/app/services/grok/services/chat.py:/app/app/services/grok/services/chat.py:ro",
    "      - ./vendor/grok2api/app/services/grok/utils/process.py:/app/app/services/grok/utils/process.py:ro",
    "      - ./vendor/grok2api/app/services/cf_refresh/solver.py:/app/app/services/cf_refresh/solver.py:ro",
    "      - ./vendor/grok2api/app/services/reverse/app_chat.py:/app/app/services/reverse/app_chat.py:ro",
]


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def run(ssh: paramiko.SSHClient, command: str) -> str:
    stdin, stdout, stderr = ssh.exec_command(command, get_pty=False)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if code != 0:
        raise RuntimeError(
            f"remote command failed ({code}): {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        )
    return out


def main() -> int:
    host = env("GROK_DEPLOY_HOST", "111.230.202.235")
    user = env("GROK_DEPLOY_USER", "root")
    password = env("GROK_DEPLOY_PASSWORD") or env("SSH_PASSWORD")
    remote_dir = env(
        "GROK_DEPLOY_REMOTE_DIR",
        "/opt/grok2api-rs-fork/grok2api-python-stack",
    )
    creds = env("GROK_DEPLOY_CREDS", "/root/.grok2api-prod-credentials")

    if not password:
        print("Set GROK_DEPLOY_PASSWORD or SSH_PASSWORD before running.", file=sys.stderr)
        return 2

    missing = [str(local) for local, _ in FILES if not local.exists()]
    if missing:
        print("Missing local files:\n" + "\n".join(missing), file=sys.stderr)
        return 2

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, username=user, password=password, timeout=25)

    try:
        run(
            ssh,
            "test -d "
            + sh(remote_dir)
            + " && test -f "
            + sh(posixpath.join(remote_dir, "docker-compose.prod.yml")),
        )
        stamp = run(ssh, "date +%Y%m%d%H%M%S").strip()
        backup_dir = posixpath.join(remote_dir, "backups", f"app-chat-fix-{stamp}")
        run(ssh, "mkdir -p " + sh(backup_dir))
        compose_path = posixpath.join(remote_dir, "docker-compose.prod.yml")
        run(ssh, "cp -a " + sh(compose_path) + " " + sh(posixpath.join(backup_dir, "docker-compose.prod.yml")))

        sftp = ssh.open_sftp()
        try:
            for local, relative in FILES:
                remote = posixpath.join(remote_dir, relative)
                backup = posixpath.join(backup_dir, relative)
                run(
                    ssh,
                    "mkdir -p "
                    + sh(posixpath.dirname(backup))
                    + " "
                    + sh(posixpath.dirname(remote)),
                )
                run(ssh, "cp -a " + sh(remote) + " " + sh(backup))
                tmp_remote = f"{remote}.tmp.{stamp}"
                sftp.put(str(local), tmp_remote)
                run(ssh, "mv " + sh(tmp_remote) + " " + sh(remote))

            with sftp.open(compose_path, "r") as fh:
                compose_text = fh.read().decode("utf-8")
            updated_text = ensure_compose_mounts(compose_text)
            if updated_text != compose_text:
                tmp_compose = f"{compose_path}.tmp.{stamp}"
                with sftp.open(tmp_compose, "w") as fh:
                    fh.write(updated_text)
                run(ssh, "mv " + sh(tmp_compose) + " " + sh(compose_path))
        finally:
            sftp.close()

        deploy_cmd = f"""
set -eu
cd {sh(remote_dir)}
if [ -f {sh(creds)} ]; then
  set -a
  . {sh(creds)}
  set +a
fi
export GROK2API_APP_KEY="${{GROK2API_APP_KEY:-${{APP_KEY:-}}}}"
export GROK2API_API_KEY="${{GROK2API_API_KEY:-${{API_KEY:-}}}}"
export GROK_REGISTER_DEFAULT_API_TOKEN="${{GROK_REGISTER_DEFAULT_API_TOKEN:-${{GROK2API_APP_KEY}}}}"
config_file=runtime/grok2api/data/config.toml
if [ -f "$config_file" ]; then
  cp -a "$config_file" "$config_file.bak.app-chat-fix-{stamp}"
  sed -i 's/browser = "chrome148"/browser = "chrome136"/g; s/browser = "chrome146"/browser = "chrome136"/g; s/Chrome\\/148\\.0\\.0\\.0/Chrome\\/136.0.0.0/g; s/Chrome\\/146\\.0\\.0\\.0/Chrome\\/136.0.0.0/g' "$config_file"
fi
docker compose -p grok2api_prod -f docker-compose.prod.yml up -d --force-recreate --no-build grok2api
docker compose -p grok2api_prod -f docker-compose.prod.yml ps grok2api
if command -v curl >/dev/null 2>&1; then
  for i in $(seq 1 20); do
    if curl -fsS --max-time 10 http://127.0.0.1:18089/health || curl -fsS --max-time 10 http://127.0.0.1:18089/; then
      break
    fi
    if [ "$i" -eq 20 ]; then
      exit 1
    fi
    sleep 2
  done
fi
docker logs --tail=40 grok2api-prod-api
"""
        print(run(ssh, deploy_cmd))
        print(f"Backup directory: {backup_dir}")
        return 0
    finally:
        ssh.close()


def sh(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def ensure_compose_mounts(compose_text: str) -> str:
    lines = compose_text.splitlines()
    existing = set(line.rstrip() for line in lines)
    missing = [mount for mount in COMPOSE_MOUNTS if mount not in existing]
    if not missing:
        return compose_text

    insert_after = -1
    for idx, line in enumerate(lines):
        if "models_override.py:/app/app/services/token/models.py:ro" in line:
            insert_after = idx
            break
    if insert_after == -1:
        for idx, line in enumerate(lines):
            if "image_edit_override.py:/app/app/services/grok/services/image_edit.py:ro" in line:
                insert_after = idx
                break
    if insert_after == -1:
        raise RuntimeError("Could not find grok2api volume section in compose file")

    lines[insert_after + 1:insert_after + 1] = missing
    trailing_newline = "\n" if compose_text.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline


if __name__ == "__main__":
    raise SystemExit(main())
