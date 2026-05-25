#!/usr/bin/env bash
# Idempotent installer for Larksor-TC on macOS.
#
# Usage:
#   bash install.sh                              (interactive prompts)
#   LARK_APP_ID=cli_x LARK_APP_SECRET=y bash install.sh
#   LARKSOR_CN=1 bash install.sh                 (use CN mirrors)
#   bash install.sh --app-id cli_x --app-secret y
#
# Env knobs:
#   LARKSOR_CN=1   route brew (USTC), pip (Tsinghua TUNA), npm (npmmirror)
#                  through mainland-China mirrors; rewrites Homebrew env
#                  vars + npm registry only for this run (pip writes to
#                  ~/.config/pip/pip.conf, which is also reversible).
#
# What it does:
#   1. Ensures Homebrew, node, python3, jq are present (tmux optional)
#   2. Installs @larksuite/cli (npm), lark-oapi (pip), Cursor CLI (curl)
#   3. Runs lark-cli OAuth login if not already logged in
#   4. Writes ~/larksor-tc/secrets.env (0600) with App ID + Secret
#   5. Generates ~/Library/LaunchAgents/cn.modelbest.larksor-tc.plist
#      and loads it via launchctl (so bridge auto-starts at login)
#   6. Smoke-tests the new bridge

set -euo pipefail

BRIDGE_DIR="${BRIDGE_DIR:-$HOME/larksor-tc}"
PLIST_LABEL="cn.modelbest.larksor-tc"
PLIST_TARGET="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PLIST_TEMPLATE="$BRIDGE_DIR/${PLIST_LABEL}.plist.template"

APP_ID="${LARK_APP_ID:-}"
APP_SECRET="${LARK_APP_SECRET:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-id)      APP_ID="$2"; shift 2;;
    --app-secret)  APP_SECRET="$2"; shift 2;;
    --bridge-dir)  BRIDGE_DIR="$2"; shift 2;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

say()  { printf "\033[1;34m[install]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 0. Sanity
# --------------------------------------------------------------------------

[[ "$(uname)" == "Darwin" ]] || die "this installer is macOS only"
[[ -d "$BRIDGE_DIR" ]] || die "$BRIDGE_DIR does not exist; clone or copy the project there first"
[[ -f "$BRIDGE_DIR/bridge.py" ]] || die "$BRIDGE_DIR/bridge.py missing"
[[ -f "$PLIST_TEMPLATE" ]] || die "$PLIST_TEMPLATE missing"

# --------------------------------------------------------------------------
# 0.5. Mirror selection (LARKSOR_CN=1 → switch brew/pip/npm to CN mirrors)
# --------------------------------------------------------------------------
# Why this lives in the installer (not only in HANDOFF.md): users running
# install.sh by hand (Option B in README) skip the LLM walkthrough, so we
# still need a single env flag they can flip. The mirror domains are read
# by brew / pip / npm directly via env + per-user config; nothing here
# writes to /etc.

if [[ "${LARKSOR_CN:-0}" == "1" ]]; then
  say "LARKSOR_CN=1 → using mainland-China mirrors (USTC / Tsinghua / npmmirror)"
  export HOMEBREW_BREW_GIT_REMOTE="${HOMEBREW_BREW_GIT_REMOTE:-https://mirrors.ustc.edu.cn/brew.git}"
  export HOMEBREW_CORE_GIT_REMOTE="${HOMEBREW_CORE_GIT_REMOTE:-https://mirrors.ustc.edu.cn/homebrew-core.git}"
  export HOMEBREW_BOTTLE_DOMAIN="${HOMEBREW_BOTTLE_DOMAIN:-https://mirrors.ustc.edu.cn/homebrew-bottles}"
  export HOMEBREW_API_DOMAIN="${HOMEBREW_API_DOMAIN:-https://mirrors.ustc.edu.cn/homebrew-bottles/api}"
  PIP_INDEX_URL_FLAG=(--index-url https://pypi.tuna.tsinghua.edu.cn/simple
                      --trusted-host pypi.tuna.tsinghua.edu.cn)
  BREW_BOOTSTRAP_URL="https://mirrors.ustc.edu.cn/misc/brew-install.sh"
  NPM_REGISTRY="https://registry.npmmirror.com"
else
  PIP_INDEX_URL_FLAG=()
  BREW_BOOTSTRAP_URL="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
  NPM_REGISTRY=""
fi

# --------------------------------------------------------------------------
# 1. Homebrew + brew packages
# --------------------------------------------------------------------------

if ! command -v brew >/dev/null 2>&1; then
  say "Homebrew not found - installing from $BREW_BOOTSTRAP_URL"
  /bin/bash -c "$(curl -fsSL "$BREW_BOOTSTRAP_URL")"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi

# tmux is optional now (we no longer require it; bridge runs under launchd
# directly). Install it if missing but never block on it.
for pkg in node jq; do
  if ! command -v "$pkg" >/dev/null 2>&1; then
    say "brew install $pkg"
    brew install "$pkg"
  fi
done
if ! command -v tmux >/dev/null 2>&1; then
  say "brew install tmux (optional, for manual session debugging)"
  brew install tmux || warn "tmux install failed - skipping (not required)"
fi

# npm registry override (only if user opted in to CN mirrors)
if [[ -n "$NPM_REGISTRY" ]] && command -v npm >/dev/null 2>&1; then
  current_reg="$(npm config get registry 2>/dev/null || echo)"
  if [[ "$current_reg" != "$NPM_REGISTRY"* ]]; then
    say "npm config set registry $NPM_REGISTRY"
    npm config set registry "$NPM_REGISTRY"
  fi
fi

# Ensure global npm prefix is owned by the user (avoid sudo for -g installs).
if [[ "$(npm config get prefix 2>/dev/null)" == "/usr/local" || \
      "$(npm config get prefix 2>/dev/null)" == "/opt/homebrew" ]]; then
  mkdir -p "$HOME/.npm-global"
  npm config set prefix "$HOME/.npm-global"
  case ":$PATH:" in
    *":$HOME/.npm-global/bin:"*) ;;
    *) export PATH="$HOME/.npm-global/bin:$PATH";;
  esac
fi

# --------------------------------------------------------------------------
# 2. CLI tools (lark-cli, Cursor CLI) + Python SDK
# --------------------------------------------------------------------------

if ! command -v lark-cli >/dev/null 2>&1; then
  say "npm install -g @larksuite/cli"
  npm install -g @larksuite/cli
fi

if ! command -v agent >/dev/null 2>&1; then
  say "installing Cursor CLI from https://cursor.com/install"
  curl -fsSL https://cursor.com/install | bash
  if [[ -x "$HOME/.local/bin/agent" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

say "ensuring lark-oapi (Python) is up to date"
python3 -m pip install --user --quiet --upgrade "${PIP_INDEX_URL_FLAG[@]}" lark-oapi

# --------------------------------------------------------------------------
# 3. Cursor CLI login (only if not already)
# --------------------------------------------------------------------------

if ! agent status 2>&1 | grep -qi 'logged in'; then
  warn "Cursor CLI is not logged in. Opening login flow now..."
  say "(this will open a browser tab; complete the login then come back)"
  agent login
fi

# --------------------------------------------------------------------------
# 4. lark-cli OAuth (only if not already)
# --------------------------------------------------------------------------

if ! lark-cli auth status 2>/dev/null | grep -q '"appId"'; then
  if [[ -z "$APP_ID" ]]; then
    cat <<'EOF'
[install] lark-cli is not configured yet. You need a Feishu app first.
[install] Open https://open.feishu.cn/app and:
[install]   1. 创建企业自建应用
[install]   2. App name suggestion: "<你的名字>的飞书CLI"
[install]   3. Bot capability: ON
[install]   4. Add scopes: im:message, im:message:send_as_bot, cardkit:card:write
[install]   5. Subscribe events: im.message.receive_v1, card.action.trigger
[install]   6. 申请发布 - 内部审批
[install]   7. Copy App ID and App Secret
EOF
    open "https://open.feishu.cn/app" 2>/dev/null || true
    printf "\nApp ID (cli_xxxxxxxxxxxxxxxx): "; read -r APP_ID
    printf "App Secret: "; read -rs APP_SECRET; echo
  fi
  say "running lark-cli config init"
  lark-cli config init --new --lang zh
  say "running lark-cli auth login"
  lark-cli auth login --recommend
fi

# --------------------------------------------------------------------------
# 5. Resolve App ID + Secret and write secrets.env
# --------------------------------------------------------------------------

if [[ -z "$APP_ID" ]]; then
  APP_ID="$(python3 -c '
import json,pathlib
try:
  c=json.loads(pathlib.Path.home().joinpath(".lark-cli/config.json").read_text())
  print(c["apps"][0]["appId"])
except Exception:
  pass
' 2>/dev/null || true)"
fi

if [[ -z "$APP_ID" ]]; then
  printf "App ID (cli_xxxxxxxxxxxxxxxx): "; read -r APP_ID
fi
if [[ -z "$APP_SECRET" ]]; then
  printf "App Secret (paste once, will be 0600-saved to secrets.env): "
  read -rs APP_SECRET; echo
fi

[[ -n "$APP_ID" && -n "$APP_SECRET" ]] || die "App ID / App Secret are required"

umask 077
SECRETS_FILE="$BRIDGE_DIR/secrets.env"
cat > "$SECRETS_FILE" <<EOF
LARK_APP_ID=$APP_ID
LARK_APP_SECRET=$APP_SECRET
EOF
chmod 600 "$SECRETS_FILE"
say "wrote $SECRETS_FILE ($(wc -c < "$SECRETS_FILE") bytes, 0600)"

# --------------------------------------------------------------------------
# 6. launchd plist
# --------------------------------------------------------------------------

mkdir -p "$(dirname "$PLIST_TARGET")"
sed "s|__HOME__|$HOME|g" "$PLIST_TEMPLATE" > "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"

# Stop any pre-existing instance (manual `python3 bridge.py` or old plist),
# so we don't end up with two ws clients fighting for the same event stream.
launchctl bootout "gui/$UID/${PLIST_LABEL}" 2>/dev/null || true
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t larksor-tc 2>/dev/null || true
  tmux kill-session -t cursorbridge 2>/dev/null || true
fi

say "loading launchd job: $PLIST_LABEL"
launchctl bootstrap "gui/$UID" "$PLIST_TARGET"
launchctl enable    "gui/$UID/${PLIST_LABEL}"
launchctl kickstart -k "gui/$UID/${PLIST_LABEL}"

# --------------------------------------------------------------------------
# 7. Smoke test
# --------------------------------------------------------------------------

say "waiting 4s for bridge to settle, then peeking at log..."
sleep 4

if [[ -f "$BRIDGE_DIR/bridge.log" ]]; then
  echo "----- bridge.log (last 12 lines) -----"
  tail -n 12 "$BRIDGE_DIR/bridge.log"
  echo "--------------------------------------"
fi

if launchctl print "gui/$UID/${PLIST_LABEL}" 2>/dev/null | grep -q 'state = running'; then
  say "✅ launchd job is running"
else
  warn "launchd job is NOT in 'running' state - inspect with:"
  warn "    launchctl print gui/$UID/${PLIST_LABEL}"
  warn "and check $BRIDGE_DIR/bridge.log"
fi

cat <<EOF

[install] Setup complete.

Next steps:
  1. In Feishu, DM the bot you created ("你的名字的飞书CLI").
     Send "hi" - you should get a streaming card back with Opus 4.7 thinking.

  2. Useful commands inside Feishu:
       /help         show all bridge commands
       /status       chat_id / model / workspace
       /new          start a fresh chat
       /cd <path>    change workspace for the active chat
       /model opus   switch model (auto / opus / sonnet / gpt5 / codex)
       /history 5    recent turns + token cost
       /cost today   token usage today
       /include <p>  attach a Mac file to the NEXT prompt

  3. To watch live logs:
       tail -f $BRIDGE_DIR/bridge.log

  4. To stop:    launchctl bootout gui/$UID/${PLIST_LABEL}
     To restart: launchctl kickstart -k gui/$UID/${PLIST_LABEL}
     To remove: bash $BRIDGE_DIR/uninstall.sh
EOF
