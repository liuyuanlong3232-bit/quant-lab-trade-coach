#!/bin/sh
set -eu

# API remains loopback-only inside the container; nginx is the sole public
# process and is published by compose only on the host loopback interface.
python -m quant_lab.cli trade-coach --serve --host 127.0.0.1 --port 8765 \
  --db "${QUANT_LAB_DATA_DIR}/trade_coach.sqlite3" \
  --project-root "${QUANT_LAB_DATA_DIR}" &
api_pid=$!
nginx -g 'daemon off;' &
nginx_pid=$!
trap 'kill "$api_pid" "$nginx_pid" 2>/dev/null || true' TERM INT EXIT
while kill -0 "$api_pid" 2>/dev/null && kill -0 "$nginx_pid" 2>/dev/null; do
  sleep 2
done
exit 1
