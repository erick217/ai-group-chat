#!/bin/sh
# 用法：./run.sh            使用 config.yaml
#       GROUPCHAT_CONFIG=config.demo.yaml ./run.sh   演示模式（假模型，不需要 key）
#       HOST=0.0.0.0 ./run.sh   允许同一局域网的其他设备访问
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8770}"
