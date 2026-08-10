#!/bin/sh
# Self-healing entrypoint.
#
# When a host bind-mount is used for /data, the directory's ownership comes
# from the host, not from anything the Dockerfile chown'd at build time. If
# UID 1000 (the app user) can't write to it, pairing fails with a 500 the
# first time the user clicks "Start pairing" because androidtvremote2 can't
# write its cert. This script fixes ownership at startup before dropping
# privileges to the unprivileged app user via gosu.
set -e

DATA_DIR="${DATA_DIR:-/data}"
APP_USER="app"
APP_UID="1000"

mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
    # Normal path: we started as root, can fix /data ownership freely.
    chown -R "$APP_UID:$APP_UID" "$DATA_DIR"
    exec gosu "$APP_USER" "$@"
fi

# Compose `user:` override or some other reason we're already non-root.
# Can't chown — verify writability and fail loudly with an actionable hint.
if ! [ -w "$DATA_DIR" ]; then
    echo "ERROR: $DATA_DIR is not writable by UID $(id -u)." >&2
    echo "Either remove the compose 'user:' override so the entrypoint can" >&2
    echo "chown /data, or pre-chown the host bind-mount path to UID $(id -u)." >&2
    exit 1
fi

exec "$@"
