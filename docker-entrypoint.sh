#!/bin/sh
set -eu

if [ "${1:-}" = "--help" ]; then
    cat <<'EOF'
Usage: docker run IMAGE [--once | COMMAND ARGS...]

With no argument the container checks Normattiva every CHECK_INTERVAL_SECONDS,
runs a full snapshot when an edition changes, and forces one every
FULL_RUN_INTERVAL_SECONDS. Pass --once to run one snapshot and exit.
EOF
    exit 0
fi

if [ "${1:-}" = "--once" ]; then
    set -- --once
elif [ "$#" -gt 0 ]; then
    exec "$@"
else
    set -- --loop
fi

exec python -m italia_corpus \
    --download-cache "${DOWNLOAD_CACHE_PATH:-/data/download-cache}" \
    "${ROOT_PATH:-/data/work}" "$@"
