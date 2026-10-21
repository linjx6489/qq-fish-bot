#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/2] 创建虚拟环境..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
fi

echo "[2/2] 安装依赖..."
.venv/bin/python -m pip install -q -r requirements.txt

echo
echo "=== 吃白饭的肥鱼 启动中 ==="
exec .venv/bin/python -m fishbot "$@"
