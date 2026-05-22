#!/usr/bin/env bash
# Reverse of install.sh.
# Removes the launchd job + secrets.env. Keeps state.db (so chat history
# survives) unless you pass --purge.

set -euo pipefail

BRIDGE_DIR="${BRIDGE_DIR:-$HOME/larksor-tc}"
PLIST_LABEL="cn.modelbest.larksor-tc"
PLIST_TARGET="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift;;
    --bridge-dir) BRIDGE_DIR="$2"; shift 2;;
    -h|--help) echo "usage: $0 [--purge] [--bridge-dir DIR]"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

echo "[uninstall] stopping launchd job"
launchctl bootout "gui/$UID/${PLIST_LABEL}" 2>/dev/null || true
rm -f "$PLIST_TARGET"

echo "[uninstall] killing any leftover tmux session"
tmux kill-session -t larksor-tc 2>/dev/null || true
# also kill any leftover legacy session
tmux kill-session -t cursorbridge 2>/dev/null || true

if [[ -f "$BRIDGE_DIR/secrets.env" ]]; then
  echo "[uninstall] removing secrets.env"
  rm -f "$BRIDGE_DIR/secrets.env"
fi

if [[ $PURGE -eq 1 ]]; then
  echo "[uninstall] --purge: also removing state.db and bridge.log"
  rm -f "$BRIDGE_DIR/state.db" "$BRIDGE_DIR/state.db-shm" \
        "$BRIDGE_DIR/state.db-wal" "$BRIDGE_DIR/bridge.log"
fi

cat <<EOF

[uninstall] Done.

Kept (unless --purge):
  $BRIDGE_DIR/state.db    chat history
  $BRIDGE_DIR/bridge.log  recent logs

Note: lark-cli + Cursor CLI + Homebrew packages are NOT removed. If you
also want to revoke Feishu access, uninstall the app from
https://open.feishu.cn/app
EOF
