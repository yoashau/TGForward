#!/usr/bin/env bash
# 首次部署：deploy.sh init；更新：deploy.sh。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mode=${1:-update}
case "$mode" in
  init|update) ;;
  -h|--help) echo '用法：scripts/deploy.sh [init|update]'; exit 0 ;;
  *) echo "未知操作：$mode" >&2; exit 1 ;;
esac

env_file=${TGFORWARD_ENV_FILE:-/etc/tgforward/tgforward.env}
data_dir=${TGFORWARD_DATA_DIR:-/var/lib/tgforward}
case "$env_file:$data_dir" in
  /*:/*) ;;
  *) echo '配置和数据目录必须使用绝对路径。' >&2; exit 1 ;;
esac
export TGFORWARD_ENV_FILE="$env_file" TGFORWARD_DATA_DIR="$data_dir"
export APP_UID=${APP_UID:-10001} APP_GID=${APP_GID:-10001}
export SOURCE_REVISION=${SOURCE_REVISION:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}

if [ "$mode" = init ]; then
  install -d -m 0750 "$(dirname -- "$env_file")"
  install -d -o "$APP_UID" -g "$APP_GID" -m 0750 \
    "$data_dir" "$data_dir/state" "$data_dir/thumbs"
  if [ ! -e "$env_file" ]; then
    python3 - "$env_file" <<'PY'
import os
import secrets
import sys
from pathlib import Path

text = Path('.env.example').read_text()
text = text.replace('\nMASTER_KEY=\n', '\nMASTER_KEY=' + secrets.token_urlsafe(32) + '\n')
text = text.replace('\nSALT_KEY=\n', '\nSALT_KEY=' + secrets.token_urlsafe(16) + '\n')
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(text)
PY
    echo "已创建配置：$env_file"
    echo '填写 API_ID、API_HASH、BOT_TOKEN、OWNER_ID 后，再运行本命令。密钥无需修改。'
    exit 2
  fi
fi

test -f "$env_file" || { echo "缺少配置：$env_file；首次部署请运行 deploy.sh init。" >&2; exit 1; }
compose() { docker compose --env-file "$env_file" "$@"; }
if [ "$mode" = init ]; then
  compose build bot
  compose run --rm --no-deps bot python -m tgforward.tools.init_state
  compose up -d --wait --wait-timeout 180 bot
else
  test -f "$data_dir/state/tgforward.sqlite3" || {
    echo '缺少状态库。新实例请运行 deploy.sh init；已有数据请先完成导入。' >&2
    exit 1
  }
  compose up -d --build --wait --wait-timeout 180 bot
fi
echo 'TGForward 已启动。'
