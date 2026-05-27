#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
TMP_DIR="${TMP_DIR:-$DATA_DIR/tmp}"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$TMP_DIR"

if [ ! -f "$DATA_DIR/config.toml" ]; then
  python3 - "$DATA_DIR/config.toml" <<'PY'
import json
import os
import sys

config_file = sys.argv[1]

config = {
    "app": {
        "app_key": os.getenv("GROK2API_APP_KEY", "grok2api"),
        "api_key": os.getenv("GROK2API_API_KEY", ""),
    },
    "proxy": {
        "base_proxy_url": os.getenv("GROK2API_BASE_PROXY_URL", ""),
        "asset_proxy_url": os.getenv(
            "GROK2API_ASSET_PROXY_URL",
            os.getenv("GROK2API_BASE_PROXY_URL", ""),
        ),
    },
}

with open(config_file, "w", encoding="utf-8") as f:
    for section, values in config.items():
        f.write(f"[{section}]\n")
        for key, value in values.items():
            f.write(f"{key} = {json.dumps(value)}\n")
        f.write("\n")
PY
fi

if [ ! -f "$DATA_DIR/token.json" ]; then
  echo "{}" > "$DATA_DIR/token.json"
fi

chmod 600 "$DATA_DIR/config.toml" "$DATA_DIR/token.json" || true
