#!/usr/bin/env bash
# Boot the Feishu <-> cursor-cli bridge in a tmux session.
# v2: single window, runs bridge.py only. Agent is invoked headlessly
# per message via `agent -p --resume <chat_id>`.

set -euo pipefail

SESSION="${BRIDGE_SESSION:-larksor-tc}"
BRIDGE_DIR="$HOME/larksor-tc"
PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:$PATH"
export PATH

mkdir -p "$BRIDGE_DIR"

for bin in tmux agent lark-cli python3; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "$bin not found in PATH" >&2
    exit 1
  fi
done

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[start] reusing existing session: $SESSION"
else
  echo "[start] creating session: $SESSION"
  tmux new-session -d -s "$SESSION" -n bridge \
    "python3 $BRIDGE_DIR/bridge.py 2>&1 | tee -a $BRIDGE_DIR/bridge.log"
fi

echo
echo "Attach with:  tmux attach -t $SESSION"
echo "Detach:       Ctrl-b d"
echo "Stop:         tmux kill-session -t $SESSION"
echo

exec tmux attach -t "$SESSION"
