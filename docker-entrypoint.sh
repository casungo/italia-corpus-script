#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: docker run IMAGE [--once]

With no argument the container checks Normattiva every CHECK_INTERVAL_SECONDS,
runs a full snapshot when an edition changes, and forces one every
FULL_RUN_INTERVAL_SECONDS. Pass --once to run one snapshot and exit.
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
CHECK_INTERVAL_SECONDS=${CHECK_INTERVAL_SECONDS:-86400}
FULL_RUN_INTERVAL_SECONDS=${FULL_RUN_INTERVAL_SECONDS:-${RUN_INTERVAL_SECONDS:-2592000}}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-3600}
LAST_FULL_RUN_FILE=${LAST_FULL_RUN_FILE:-/data/last-full-run}

case "$CHECK_INTERVAL_SECONDS:$FULL_RUN_INTERVAL_SECONDS:$RETRY_DELAY_SECONDS" in
    *[!0-9:]* | :* | *: | *::* ) echo "Run intervals must be whole seconds" >&2; exit 2 ;;
esac

mkdir -p "$ROOT_PATH" "$DOWNLOAD_CACHE_PATH"

run_snapshot() {
    python -m italia_corpus \
        --download-cache "$DOWNLOAD_CACHE_PATH" \
        "$ROOT_PATH"
}

run_full_snapshot() {
    if run_snapshot; then
        date +%s > "$LAST_FULL_RUN_FILE"
        return 0
    fi
    return 1
}

if [ "${1:-}" = "--once" ]; then
    run_full_snapshot
    exit 0
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

while :; do
    now=$(date +%s)
    last=0
    [ -f "$LAST_FULL_RUN_FILE" ] && last=$(cat "$LAST_FULL_RUN_FILE")
    if [ "$last" -gt 0 ] && [ $((now - last)) -lt "$FULL_RUN_INTERVAL_SECONDS" ] \
        && python -m italia_corpus --check-upstream --download-cache "$DOWNLOAD_CACHE_PATH" "$ROOT_PATH"
    then
        sleep "$CHECK_INTERVAL_SECONDS"
    elif ! run_full_snapshot; then
        echo "Snapshot failed; retrying in ${RETRY_DELAY_SECONDS}s with the persisted cache." >&2
        sleep "$RETRY_DELAY_SECONDS"
    fi
done
