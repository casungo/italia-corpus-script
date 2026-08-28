#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: docker run IMAGE [--once]

With no argument the container runs a full snapshot immediately, then repeats
after RUN_INTERVAL_SECONDS. Set RUN_INTERVAL_SECONDS=0 or pass --once to run
one snapshot and exit.
EOF
}

if [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ -n "${PUBLISH_TOKEN_FILE:-}" ]; then
    PUBLISH_TOKEN=$(cat "$PUBLISH_TOKEN_FILE")
elif [ -n "${GITHUB_TOKEN_FILE:-}" ]; then
    PUBLISH_TOKEN=$(cat "$GITHUB_TOKEN_FILE")
else
    PUBLISH_TOKEN=""
fi

PUBLISH_TARGET=${PUBLISH_TARGET:-github}
case "$PUBLISH_TARGET" in
    github)
        GITHUB_TOKEN=${GITHUB_TOKEN:-$PUBLISH_TOKEN}
        export GITHUB_TOKEN
        : "${GITHUB_TOKEN:?Set GITHUB_TOKEN, GITHUB_TOKEN_FILE, or PUBLISH_TOKEN_FILE}"
        : "${GITHUB_USERNAME:?Set GITHUB_USERNAME}"
        : "${GITHUB_TARGET_REPO:?Set GITHUB_TARGET_REPO}"
        ;;
    git)
        GIT_TARGET_TOKEN=${GIT_TARGET_TOKEN:-$PUBLISH_TOKEN}
        export GIT_TARGET_TOKEN
        : "${GIT_TARGET_URL:?Set GIT_TARGET_URL when PUBLISH_TARGET=git}"
        ;;
    *) echo "PUBLISH_TARGET must be github or git" >&2; exit 2 ;;
esac

ROOT_PATH=${ROOT_PATH:-/data/work}
DOWNLOAD_CACHE_PATH=${DOWNLOAD_CACHE_PATH:-/data/download-cache}
RUN_INTERVAL_SECONDS=${RUN_INTERVAL_SECONDS:-0}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-3600}

case "$RUN_INTERVAL_SECONDS:$RETRY_DELAY_SECONDS" in
    *[!0-9:]* | :* | *:) echo "Run intervals must be whole seconds" >&2; exit 2 ;;
esac

mkdir -p "$ROOT_PATH" "$DOWNLOAD_CACHE_PATH"

run_snapshot() {
    python -m italia_corpus \
        --download-cache "$DOWNLOAD_CACHE_PATH" \
        "$ROOT_PATH"
}

if [ "${1:-}" = "--once" ]; then
    run_snapshot
    exit 0
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

while :; do
    if run_snapshot; then
        [ "$RUN_INTERVAL_SECONDS" -gt 0 ] || exit 0
        sleep "$RUN_INTERVAL_SECONDS"
    else
        echo "Snapshot failed; retrying in ${RETRY_DELAY_SECONDS}s with the persisted cache." >&2
        sleep "$RETRY_DELAY_SECONDS"
    fi
done
