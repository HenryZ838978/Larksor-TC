# Larksor-TC · HANDOFF Manual for LLM Agents

> **Read this whole file before responding to the user.**
>
> You are the AI assistant (Cursor / Claude Code / Codex CLI / Cline /
> Cody / whatever) that just opened this repo. Your job is to take the
> user from a fresh clone to a working install of Larksor-TC.
>
> Walk **one phase at a time**. Stop at every gate marked `🛑 STOP`.
> Never silently skip a phase. Never paraphrase the Feishu scope/event
> names — they must be copied verbatim or the bot will not work.

---

## Your invariants

1. **Never `sudo`.** If a step would need elevated privileges, stop and
   ask the user to run it themselves in a separate terminal.
2. **Secret hygiene.** Accept `App Secret` exactly once. Write it to
   `~/larksor-tc/secrets.env` (mode `0600`). Never echo it back in chat,
   never paste it into any log, screenshot, commit, or status report.
   When you need to refer to it later, refer to "the App Secret you
   gave me" — not the value.
3. **Idempotency.** Every command you run must be safe to run twice.
   The installer (`install.sh`) is already idempotent; preserve that
   property in anything you add.
4. **Verify, don't assume.** After every install step, run the verify
   block and report the result back to the user before moving on.
5. **Language.** Mirror the user's language. If they message you in
   Chinese, all status / questions / errors should be in Chinese. The
   example lines in this manual are bilingual — pick the one that
   matches the user.
6. **Don't run the live agent.** The `python3 bridge.py` process is
   long-running and is managed by `launchd`. Never start it in your
   own terminal session — `launchctl kickstart` will do that for you.

---

## Phase 0 — Greet & confirm scope

Say (use user's language):

> 🤖 我是即将帮你装 **Larksor-TC** 的 LLM。装完后，你就可以从手机 / iPad / 任何能开飞书的设备，
> 通过飞书消息驱动你这台 Mac 上的 Cursor + Opus 4.7。
>
> 整个流程 ~10 分钟，分 5 步：
> 1. **自检** —— 看你这台机器缺什么
> 2. **装依赖** —— brew / node / python / cursor-cli / lark-oapi（可选国内源）
> 3. **建飞书自建应用** —— 我会一步步带你点，并直接帮你打开网页
> 4. **写密钥 & 起服务** —— `install.sh` 一键搞定，launchd 自启动
> 5. **冒烟测试** —— 在飞书里给 bot 发条消息，确认收到流式回复
>
> 每步之间我会停下问你一句，确认后再走。准备好了说 **"go"**。

(English version: "I'm the LLM that's going to install Larksor-TC for you. ~10 min, 5 phases, I'll stop between each. Say 'go' when ready.")

🛑 **STOP.** Wait for `go` / `好` / `开始` / `start` before moving on.

---

## Phase 1 — Machine self-check

Run this **exact** block in the user's shell as a single call (do not
break it up — the multiline `{ … }` is intentional so the user sees
one consolidated report):

```bash
{
  echo "## env"
  uname -srm
  sw_vers 2>/dev/null | head -3
  echo
  echo "## tools"
  for b in brew node npm python3 git sqlite3 curl jq; do
    if command -v "$b" >/dev/null 2>&1; then
      ver=$("$b" --version 2>/dev/null | head -1)
      printf "  %-8s %-30s %s\n" "$b" "$(command -v "$b")" "$ver"
    else
      printf "  %-8s MISSING\n" "$b"
    fi
  done
  echo
  echo "## cursor-cli"
  if command -v agent >/dev/null 2>&1; then
    agent --version 2>/dev/null | head -1
    agent status 2>&1 | head -5
  else
    echo "  agent CLI MISSING"
  fi
  echo
  echo "## net latency (3s timeout each)"
  for u in https://github.com https://open.feishu.cn https://cursor.com \
           https://registry.npmjs.org https://pypi.org; do
    code=$(curl -sS -o /dev/null --max-time 3 \
      -w "%{http_code} %{time_total}s" "$u" 2>&1 || echo "TIMEOUT")
    printf "  %-32s %s\n" "$u" "$code"
  done
  echo
  echo "## existing larksor-tc"
  if [ -d "$HOME/larksor-tc" ]; then
    ls -la "$HOME/larksor-tc" | head -10
    [ -f "$HOME/larksor-tc/secrets.env" ] && echo "  secrets.env EXISTS (good)"
    launchctl list 2>/dev/null | grep larksor || echo "  no launchd job loaded"
  else
    echo "  $HOME/larksor-tc DOES NOT EXIST yet (will git clone below)"
  fi
} 2>&1
```

**Interpret the output. Decide:**

| signal | meaning | what you do |
|---|---|---|
| `uname` ≠ `Darwin` | not macOS | 🛑 STOP. Tell user "Larksor-TC v1 is macOS-only" and stop. |
| github/pypi/npm timeout or > 2s | likely CN-mainland network | suggest `MIRROR_MODE=cn` in Phase 2 |
| `brew` MISSING + CN network | brew install must use USTC mirror | flag it |
| `agent` MISSING | Cursor CLI not installed yet | Phase 2 will install |
| `agent` present + `not logged in` | needs `agent login` | Phase 4 |
| `~/larksor-tc/secrets.env` exists | re-install / upgrade | tell user "looks like you already installed once, I'll skip secret setup" |
| launchd job already loaded | reinstall | will `launchctl kickstart -k` instead of `bootstrap` |

**Then ask:**

> 你在国内吗？(yes/no)
> Are you in mainland China? (yes/no)
> 如果 yes，我会把 brew / pip / npm 全部走国内源（USTC + 清华 + npmmirror），安装会快很多也不会卡 GitHub Raw。

🛑 **STOP.** Wait for the answer. Set `MIRROR_MODE=cn` or `intl`.

---

## Phase 2 — Install dependencies (mirror-aware)

### 2a. If `MIRROR_MODE=cn`, configure mirrors first

Run this block, then **show the user what you exported** so they're not
surprised later:

```bash
# Homebrew — USTC (most reliable in mainland CN)
export HOMEBREW_BREW_GIT_REMOTE="https://mirrors.ustc.edu.cn/brew.git"
export HOMEBREW_CORE_GIT_REMOTE="https://mirrors.ustc.edu.cn/homebrew-core.git"
export HOMEBREW_BOTTLE_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles"
export HOMEBREW_API_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles/api"

# pip — Tsinghua TUNA
python3 -m pip config set global.index-url \
  https://pypi.tuna.tsinghua.edu.cn/simple
python3 -m pip config set install.trusted-host pypi.tuna.tsinghua.edu.cn

# npm — npmmirror (alibaba)
if command -v npm >/dev/null 2>&1; then
  npm config set registry https://registry.npmmirror.com
fi

# Tell install.sh to use the CN brew bootstrap if brew is missing
export LARKSOR_CN=1

echo "=== mirror config ==="
echo "HOMEBREW_API_DOMAIN=$HOMEBREW_API_DOMAIN"
python3 -m pip config get global.index-url 2>/dev/null
command -v npm >/dev/null && npm config get registry
echo "LARKSOR_CN=$LARKSOR_CN"
```

> **Rollback note for the user:** these `export`s only live in your
> current shell. The `pip config set` and `npm config set` are
> persisted to `~/.config/pip/pip.conf` and `~/.npmrc` — undo with
> `pip config unset global.index-url` and `npm config delete registry`
> if you ever want them gone.

### 2b. Clone the repo (if not already present)

```bash
if [ ! -d "$HOME/larksor-tc" ]; then
  if [ "$MIRROR_MODE" = "cn" ]; then
    # GitHub usually still works in CN, but the mirror below is faster
    git clone https://gitclone.com/github.com/HenryZ838978/Larksor-TC.git \
      "$HOME/larksor-tc" \
      || git clone https://github.com/HenryZ838978/Larksor-TC.git \
           "$HOME/larksor-tc"
  else
    git clone https://github.com/HenryZ838978/Larksor-TC.git "$HOME/larksor-tc"
  fi
fi
ls "$HOME/larksor-tc/bridge.py" && echo "✓ repo present"
```

### 2c. Install Cursor CLI (if missing)

```bash
if ! command -v agent >/dev/null 2>&1; then
  curl -fsSL https://cursor.com/install | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
agent --version
```

> If `cursor.com/install` times out (rare even in CN), tell the user
> to install Cursor IDE first from <https://cursor.com>, open it once,
> then run `agent login` from Terminal — the IDE bundles the CLI.

### 2d. Verify Max Mode (needed for Opus 4.7)

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".cursor" / "cli-config.json"
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg["maxMode"] = True
cfg.setdefault("model", {})["maxMode"] = True
cfg.setdefault("attribution", {}).update({
    "attributeCommitsToAgent": False,
    "attributePRsToAgent": False,
})
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2))
print("wrote", p)
PY
```

🛑 **STOP** at the end of Phase 2. Tell the user what changed and
ask: "依赖装齐了，准备建飞书应用了吗？/ Ready to create the Feishu app?"

---

## Phase 3 — Create the Feishu enterprise self-built app

This phase is interactive — you cannot fully automate it (Feishu
admin approval requires a human click). Your job is to **open the
right pages at the right time** and tell the user exactly what to
click / paste.

### 3a. Open the app-creation page

Run:

```bash
open "https://open.feishu.cn/app"
```

Then say:

> 我已经在你的浏览器打开了**飞书开放平台 → 我的应用**页面。
> 第一次进会让你用飞书扫码登录（公司域账号即可）。
>
> 接下来：
> 1. 右上角点 **「创建企业自建应用」**（不是"商店应用"）
> 2. 应用名称就用：**`<你的名字>的飞书CLI`**（建议带名字，方便审批快）
> 3. 应用描述：`Personal Cursor remote control via Feishu DM`
> 4. 图标随便选一个内置的
> 5. 创建后会跳到应用详情页 —— **把浏览器地址栏里的 App ID 复制给我**
>    （格式像 `cli_aabbccddeeff0011`）

🛑 **STOP.** Wait for the user to paste the `App ID`. Save it as
`$APP_ID` for the rest of this session.

### 3b. Open the per-app sub-pages and walk the user through

Once you have `$APP_ID`, open each page **one at a time**, wait for
the user to confirm "done" before the next:

```bash
APP_ID="cli_xxxxxxxxxxxxxxxx"   # paste user's value

# 1. credentials page (so user can grab App Secret)
open "https://open.feishu.cn/app/$APP_ID/baseinfo"
```

Say:

> 📋 **第 1 步：拿 App Secret**
>
> 这个页面叫「凭证与基础信息」。你能看到：
> - **App ID** — 你刚给我的那个 `cli_…`
> - **App Secret** — 点旁边的 **「显示」/「Show」** 按钮才看得到
>
> 把 App Secret 复制下来，**先别给我，待会写文件时直接粘贴到终端**
> （我不会让它出现在聊天记录里）。
>
> 拿到了说 "下一步"。

🛑 **STOP** until user says "下一步" / "next".

```bash
# 2. enable bot capability
open "https://open.feishu.cn/app/$APP_ID/feature/bot"
```

> 🤖 **第 2 步：开启「机器人」能力**
>
> 这个页面在「应用功能 → 机器人」。点 **「启用」** 即可。
> 机器人名字、头像可以保持默认。
>
> 开好了说 "下一步"。

🛑 **STOP.**

```bash
# 3. permissions / scopes
open "https://open.feishu.cn/app/$APP_ID/auth"
```

> 🔑 **第 3 步：加权限**
>
> 这是「权限管理」页。点右上角 **「开通权限」**，搜索并**逐个勾选**
> 下面 6 条（一字不差）：
>
> ```
> im:message
> im:message.group_at_msg
> im:message:send_as_bot
> im:resource
> cardkit:card
> cardkit:card:write
> ```
>
> 勾完点页面底部 **「确定」** 保存。
> 全部加好了说 "下一步"。

🛑 **STOP.**

```bash
# 4. event subscriptions
open "https://open.feishu.cn/app/$APP_ID/event_subscription"
```

> 📬 **第 4 步：订阅事件**
>
> 这是「事件订阅」页。
>
> 1. 上方 **「事件回调」** 选 **「长连接 (WebSocket)」**（不是
>    "回调地址"，我们不想暴露公网 endpoint）
> 2. 下面 **「订阅事件」** 区，点「添加事件」，搜索并勾选 3 个：
>    - `im.message.receive_v1`（接收消息）
>    - `im.message.message_read_v1`（消息已读 —— **必加**，不加 bot
>      跑几小时后会停止收到消息，这是个深坑）
>    - `card.action.trigger`（卡片按钮回调）
> 3. 点页面底部「保存」
>
> 都加好了说 "下一步"。

🛑 **STOP.**

```bash
# 5. release / approval
open "https://open.feishu.cn/app/$APP_ID/release"
```

> 🚀 **第 5 步：发布申请**
>
> 这是「版本管理与发布」页。点 **「创建版本」**：
> - 版本号填 `1.0.0`
> - 可用性范围选 **「仅自己」**（最快通过，不打扰审批人）
> - 描述随便填 `personal Cursor remote control`
> - 提交后会进入审批 —— 大部分公司给个人应用是**自动通过**或几分钟内通过
>
> 提交完告诉我 "发布了"。如果你的公司审批比较慢，**别等审批，先进 Phase 4 装服务**，
> 服务装好但 bot 不会回复你 —— 等审批通过后自动就好了。

🛑 **STOP.** Wait for "发布了" / "submitted".

---

## Phase 4 — Write secrets + run installer

```bash
cd "$HOME/larksor-tc"
```

### 4a. Collect App Secret without exposing it

Tell the user:

> 现在我要把 App ID + Secret 写到 `~/larksor-tc/secrets.env`（权限 0600，
> 只有你能读，git 已忽略）。**App Secret 你直接粘到下一行 `read` 提示里
> 就行，输入时屏幕不会显示，也不会进入你跟我的聊天记录。**

Then run:

```bash
read -rp "App ID (cli_xxxxxxxxxxxxxxxx): " LARK_APP_ID
read -rsp "App Secret (input hidden): " LARK_APP_SECRET; echo
[ -n "$LARK_APP_ID" ] && [ -n "$LARK_APP_SECRET" ] || { echo "missing"; }
echo "App ID = $LARK_APP_ID  ·  App Secret = ($(printf '%s' "$LARK_APP_SECRET" | wc -c | tr -d ' ') chars, sha1=$(printf '%s' "$LARK_APP_SECRET" | shasum | cut -c1-8))"
```

Confirm with the user: "看到 App ID 对、Secret 字符数和 sha1 都对得上？OK?"
(Compare the sha1 with what they see on the Feishu credentials page if
they're paranoid; usually just length check is enough.)

🛑 **STOP** for confirmation.

### 4b. Run the installer

```bash
LARK_APP_ID="$LARK_APP_ID" \
LARK_APP_SECRET="$LARK_APP_SECRET" \
LARKSOR_CN="${LARKSOR_CN:-}" \
bash "$HOME/larksor-tc/install.sh" 2>&1 | tee /tmp/larksor-install.log
```

Then **show the last 25 lines** of `/tmp/larksor-install.log` and the
last 15 lines of `~/larksor-tc/bridge.log` to the user. Look for:

| symbol in log | meaning |
|---|---|
| `✅ launchd job is running` | service started OK |
| `SDK ws client starting` (in bridge.log) | WebSocket subscribed |
| `wrote ~/larksor-tc/secrets.env (...bytes, 0600)` | secrets persisted correctly |
| `agent CLI is not logged in` | run `agent login` once, then `launchctl kickstart -k gui/$UID/cn.modelbest.larksor-tc` |

If you see `[fail]` anywhere, stop and read the line aloud to the user.

---

## Phase 5 — Smoke test

> 现在打开飞书，在搜索框里搜你刚创建的应用名（`<你的名字>的飞书CLI`），
> 跟它发起会话，发一条消息：**`hi`**
>
> 几秒内你应该看到一张流式卡片，标题区显示 chat ID / 模型 / 工作区。
> 顺利的话，告诉我 "收到了"。

🛑 **STOP.** Wait for user response.

If the user says "没反应" / "no reply":

1. Wait at least 30 s (Feishu approval may still be pending).
2. Run:

   ```bash
   tail -n 30 "$HOME/larksor-tc/bridge.log"
   ```

3. Look for `<- ou_xxxxx...(text): hi` — if **present**, the bridge
   sees the message but card send is failing; check for `code=` errors.
4. Look for `SDK ws client starting` — if **absent**, the WebSocket
   never came up; check `secrets.env` and `agent login`.
5. If still no message in 1 min after approval is granted, ask user to
   run `/reconnect` in Feishu (will fail since bot isn't responding,
   that's OK), then `launchctl kickstart -k gui/$UID/cn.modelbest.larksor-tc`.

---

## Final report

When everything works, give the user this **report card** (verbatim
the layout, fill in the blanks):

```
✅ Larksor-TC 安装完成

  机器        : <hostname> · macOS <version> · <arch>
  网络模式    : <intl / cn-mirror>
  Cursor CLI  : <version> · Max Mode = on
  飞书 App    : <App ID> · scopes ✓ events ✓ release <pending|approved>
  服务        : launchd cn.modelbest.larksor-tc · PID <pid>
  日志        : ~/larksor-tc/bridge.log
  数据库      : ~/larksor-tc/state.db

下一步推荐你试试：
  • 在飞书里发：换模型 opus
  • 在飞书里发：/cd ~/your/project
  • 然后正式聊一句你今天最想用 Opus 解决的代码问题
```

---

## Recovery cheatsheet

The user **will** hit edge cases. Don't panic, look up the symptom:

| symptom                                   | likely cause                                   | fix |
|-------------------------------------------|------------------------------------------------|-----|
| `[fail] App ID / App Secret are required` | env vars not exported in same shell as installer | re-run with `LARK_APP_ID=... LARK_APP_SECRET=... bash install.sh` |
| Bot doesn't reply, log shows `error_code: 99991663` | App Secret typo | re-do Phase 4a |
| Bot doesn't reply, no `<- ou_` in log     | release pending / scopes missing               | confirm Phase 3 each step |
| Replies stop after a few hours            | `message_read_v1` not subscribed               | go back to Phase 3 step 4 |
| `agent` exits with `Max Mode Required`    | `~/.cursor/cli-config.json` `maxMode=false`    | re-run Phase 2d |
| `agent` exits with `resource_exhausted`   | chat history too long                          | tell user to send `/new` in Feishu |
| `brew install` 卡在 Homebrew/install.sh    | GitHub Raw 在 CN 慢                            | rerun Phase 2a + 用 USTC bootstrap： `/bin/bash -c "$(curl -fsSL https://mirrors.ustc.edu.cn/misc/brew-install.sh)"` |
| Cursor CLI install timeout                | cursor.com 偶尔慢                              | 让用户去 <https://cursor.com> 下 IDE，开一次后 CLI 就有了 |
| `lark-oapi` pip 报 SSL                    | pip 国内源未 trust                              | `pip config set install.trusted-host pypi.tuna.tsinghua.edu.cn` |

---

## What you do NOT do

- Do **not** modify `bridge.py` / `db.py` during install. The install
  flow is purely env-setup; code lives upstream in git.
- Do **not** add commits or open PRs against this repo from inside the
  install session. If the user finds a bug, suggest they open a GitHub
  issue and quote the relevant log lines.
- Do **not** generate a long "what I did" narrative at the end. The
  report card above is the entire wrap-up. Time-on-task matters.
- Do **not** suggest running `bridge.py` directly. `launchctl` owns it.

---

That's the whole manual. Good luck, future LLM.
