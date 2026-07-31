#!/usr/bin/env bash
set -euo pipefail

REQUIRED_GB=${1:-90}
TIMEOUT_SEC=${2:-120}
REQUIRED_KB=$(( REQUIRED_GB * 1024 * 1024 ))

echo "[gpu-cleanup] sync + drop_caches=3"
sync
echo 3 > /proc/sys/vm/drop_caches

ELAPSED=0
while true; do
    AVAIL=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
    echo "[gpu-cleanup] MemAvailable=${AVAIL} kB (need ${REQUIRED_KB} kB)"
    if [ "$AVAIL" -ge "$REQUIRED_KB" ]; then
        echo "[gpu-cleanup] Memory gate cleared"
        exit 0
    fi
    if [ "$ELAPSED" -ge "$TIMEOUT_SEC" ]; then
        echo "[gpu-cleanup] TIMEOUT waiting for GPU memory" >&2
        exit 1
    fi
    sleep 2
    ELAPSED=$(( ELAPSED + 2 ))
done
