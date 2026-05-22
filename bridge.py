#!/usr/bin/env python3
"""
Feishu (Lark) <-> cursor-cli bridge, v3 (CardKit streaming).

Each incoming Feishu message creates a streaming Card (CardKit v2),
sends it to the user, then runs `agent -p --output-format stream-json
--stream-partial-output --resume <chat>` and streams the answer into
the card via `PUT /cardkit/v1/cards/:card_id/elements/:eid/content`.

Card layout (markdown elements only, all streamable):

    [meta]     💬 chat ab12cd · 🧠 auto · 📁 ~/larksor-tc · 🏷 plan
    [tools]    ✓ ran `git status` (0.2s)
               ⚙ writing src/foo.ts ...
    [answer]   (streaming model output)
    [footer]   8.2s · in 4.2k / out 380 · auto · session 7b3...
    [actions]  appended on result: [Stop] [Retry] [New chat] [Auto▾]

If CardKit creation fails (permission missing, network), we fall back
to the previous plain-text reply.

Slash commands are still supported and short-circuit the agent run:
    /help /status /new /model <name> /cancel /plan ... /ask ...
plus Chinese natural-language: 换模型 X / 切换到 X / 用 X 模型.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path.home() / "larksor-tc"
STATE_FILE = ROOT / "state.json"  # legacy; migrated into state.db on first run
INBOX_DIR = ROOT / "inbox"        # downloaded images / files from Feishu
sys.path.insert(0, str(Path(__file__).parent))
import db as _db  # noqa: E402
def _detect_default_model() -> str:
    """Pick a sensible default by reading the user's cli-config.json:
      - if BRIDGE_MODEL env is set, use it
      - else if cli-config has maxMode=true, default to opus-thinking-high
      - else fall back to sonnet-thinking (best non-Max model that actually
        emits reasoning, so the 💭 thinking panel doesn't stay empty)
    """
    env = os.environ.get("BRIDGE_MODEL")
    if env:
        return env
    try:
        cfg = json.loads(Path.home().joinpath(".cursor/cli-config.json").read_text())
        if cfg.get("maxMode") is True:
            return "claude-opus-4-7-thinking-high"
    except Exception:
        pass
    return "claude-4.6-sonnet-medium-thinking"


DEFAULT_MODEL = _detect_default_model()
DEFAULT_WORKSPACE = os.environ.get("BRIDGE_WORKSPACE") or str(Path.home())

# Friendly aliases so Feishu users don't have to type full cursor model ids.
# Keys are matched case-insensitively against the full /model argument.
MODEL_ALIASES = {
    "auto": "auto",
    "opus": "claude-opus-4-7-thinking-high",
    "opus4.7": "claude-opus-4-7-thinking-high",
    "opus47": "claude-opus-4-7-thinking-high",
    "opus-thinking": "claude-opus-4-7-thinking-high",
    "opus4.7-thinking": "claude-opus-4-7-thinking-high",
    "opus4.7-thinking-high": "claude-opus-4-7-thinking-high",
    "opus-max": "claude-opus-4-7-max",
    "sonnet": "claude-4.6-sonnet-medium-thinking",
    "sonnet-thinking": "claude-4.6-sonnet-medium-thinking",
    "sonnet4.6": "claude-4.6-sonnet-medium",
    "gpt5": "gpt-5.5-high",
    "gpt-5": "gpt-5.5-high",
    "gpt5.5": "gpt-5.5-high",
    "codex": "gpt-5.3-codex-high",
    "codex5.3": "gpt-5.3-codex-high",
    "composer": "composer-2.5",
    "composer-fast": "composer-2.5-fast",
}


def resolve_model(name: str) -> str:
    """Map friendly aliases to canonical cursor model ids."""
    key = name.strip().lower()
    return MODEL_ALIASES.get(key, name)
WORKSPACE = DEFAULT_WORKSPACE  # global default; per-chat override lives in state["workspace"]


def current_workspace(state: dict) -> str:
    """Return the per-chat workspace if set, otherwise the global default."""
    return state.get("workspace") or WORKSPACE
TENANT = os.environ.get("BRIDGE_TENANT", "ModelBest")
PUSH_INTERVAL_S = 0.20
LARK_TIMEOUT_S = 15
PLAIN_TEXT_CHUNK = 3500

state_lock = threading.Lock()
proc_lock = threading.Lock()
current_proc: Optional[subprocess.Popen] = None
current_card_ctx: dict = {}

# Cached lark-oapi sync client (populated by init_lark_client at startup)
_sdk_client: Any = None
_sdk_client_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Logging + persistent state
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_PERSIST_KEYS = ("chat_id", "model", "workspace", "last_open_id",
                 "last_prompt", "pending_includes")


def load_state() -> dict:
    """Load state from SQLite kv (migrating from state.json on first run)."""
    _db.init()
    state: dict = {}
    for k in _PERSIST_KEYS:
        v = _db.kv_get(k)
        if v is not None:
            state[k] = v
    return state


def save_state(state: dict) -> None:
    """Persist all known state keys into the kv table."""
    with state_lock:
        _db.init()
        for k in _PERSIST_KEYS:
            if k in state:
                _db.kv_set(k, state[k])
            else:
                _db.kv_delete(k)


# ----------------------------------------------------------------------------
# Lark helpers - SDK-first, lark-cli subprocess as fallback
# ----------------------------------------------------------------------------

def init_lark_client(app_id: str, app_secret: str) -> None:
    """Initialize the cached lark-oapi sync Client. Idempotent."""
    global _sdk_client
    with _sdk_client_lock:
        if _sdk_client is not None:
            return
        import lark_oapi as lark
        _sdk_client = (lark.Client.builder()
                       .app_id(app_id).app_secret(app_secret)
                       .log_level(lark.LogLevel.WARNING)
                       .build())
        log(f"SDK client ready (app_id={app_id})")


def _check_resp(name: str, resp: Any) -> bool:
    """SDK response check + logging. Returns True on success."""
    try:
        ok = resp.success()
    except Exception:
        ok = False
    if not ok:
        code = getattr(resp, "code", None)
        msg = getattr(resp, "msg", None)
        log(f"{name} failed: code={code} msg={msg}")
        return False
    return True


def lark_api(method: str, path: str, *,
             data: Any = None, params: Any = None,
             as_: str = "bot") -> Optional[dict]:
    """Call `lark-cli api <METHOD> <PATH>` and return parsed JSON.

    Returns None on transport/parse error. Caller is responsible for
    checking `code` in the returned dict (Lark uses 0 = success).
    """
    args = ["lark-cli", "api", method, path, "--as", as_]
    if data is not None:
        args += ["--data", json.dumps(data, ensure_ascii=False)]
    if params is not None:
        args += ["--params", json.dumps(params, ensure_ascii=False)]
    try:
        res = subprocess.run(args, capture_output=True, text=True,
                             timeout=LARK_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        log(f"lark_api timeout: {method} {path}")
        return None
    if res.returncode != 0 and not res.stdout.strip():
        log(f"lark_api fail rc={res.returncode}: {method} {path}: "
            f"{res.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(res.stdout)
    except Exception:
        log(f"lark_api non-json: {method} {path}: {res.stdout[:200]}")
        return None


def lark_send_text(open_id: Optional[str], text: str) -> None:
    if not open_id or not text:
        return
    text = text.rstrip()
    if not text:
        return
    if _sdk_client is not None:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        for i in range(0, len(text), PLAIN_TEXT_CHUNK):
            chunk = text[i: i + PLAIN_TEXT_CHUNK]
            req = (CreateMessageRequest.builder()
                   .receive_id_type("open_id")
                   .request_body(CreateMessageRequestBody.builder()
                                 .receive_id(open_id)
                                 .msg_type("text")
                                 .content(json.dumps({"text": chunk},
                                                     ensure_ascii=False))
                                 .build())
                   .build())
            try:
                resp = _sdk_client.im.v1.message.create(req)
                _check_resp("lark_send_text", resp)
            except Exception as exc:
                log(f"lark_send_text exc: {exc}")
        return
    for i in range(0, len(text), PLAIN_TEXT_CHUNK):
        chunk = text[i: i + PLAIN_TEXT_CHUNK]
        subprocess.run(
            ["lark-cli", "im", "+messages-send", "--as", "bot",
             "--user-id", open_id, "--text", chunk],
            check=False, capture_output=True,
        )


# ----------------------------------------------------------------------------
# CardKit helpers
# ----------------------------------------------------------------------------

def create_card(card_json: dict) -> Optional[str]:
    data_str = json.dumps(card_json, ensure_ascii=False, separators=(",", ":"))
    if _sdk_client is not None:
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest, CreateCardRequestBody)
        req = (CreateCardRequest.builder().request_body(
            CreateCardRequestBody.builder()
            .type("card_json").data(data_str).build()
        ).build())
        try:
            resp = _sdk_client.cardkit.v1.card.create(req)
        except Exception as exc:
            log(f"create_card SDK exc: {exc}")
            return None
        if not _check_resp("create_card", resp):
            return None
        return getattr(resp.data, "card_id", None)
    # fallback
    resp = lark_api("POST", "/open-apis/cardkit/v1/cards",
                    data={"type": "card_json", "data": data_str})
    if not resp or resp.get("code", -1) != 0:
        if resp:
            log(f"create_card error: code={resp.get('code')} msg={resp.get('msg')}")
        return None
    return ((resp.get("data") or {}).get("card_id"))


def send_card(open_id: str, card_id: str) -> Optional[str]:
    if _sdk_client is not None:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody)
        req = (CreateMessageRequest.builder()
               .receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(open_id)
                             .msg_type("interactive")
                             .content(json.dumps(
                                 {"type": "card",
                                  "data": {"card_id": card_id}},
                                 ensure_ascii=False))
                             .build())
               .build())
        try:
            resp = _sdk_client.im.v1.message.create(req)
        except Exception as exc:
            log(f"send_card SDK exc: {exc}")
            return None
        if not _check_resp("send_card", resp):
            return None
        return getattr(resp.data, "message_id", None)
    body = {
        "receive_id": open_id,
        "msg_type": "interactive",
        "content": json.dumps({"type": "card", "data": {"card_id": card_id}},
                              ensure_ascii=False),
    }
    resp = lark_api("POST", "/open-apis/im/v1/messages",
                    data=body, params={"receive_id_type": "open_id"})
    if not resp or resp.get("code", -1) != 0:
        if resp:
            log(f"send_card error: code={resp.get('code')} msg={resp.get('msg')}")
        return None
    return ((resp.get("data") or {}).get("message_id"))


def stream_text(card_id: str, element_id: str, full_content: str,
                sequence: int) -> bool:
    if _sdk_client is not None:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest, ContentCardElementRequestBody)
        req = (ContentCardElementRequest.builder()
               .card_id(card_id).element_id(element_id)
               .request_body(ContentCardElementRequestBody.builder()
                             .uuid(str(uuid.uuid4()))
                             .content(full_content)
                             .sequence(sequence)
                             .build())
               .build())
        try:
            resp = _sdk_client.cardkit.v1.card_element.content(req)
        except Exception as exc:
            log(f"stream_text SDK exc: {exc}")
            return False
        return _check_resp(f"stream_text({element_id})", resp)
    body = {"content": full_content, "uuid": str(uuid.uuid4()), "sequence": sequence}
    resp = lark_api(
        "PUT",
        f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
        data=body,
    )
    if not resp:
        return False
    if resp.get("code", -1) != 0:
        log(f"stream_text({element_id}) code={resp.get('code')} msg={resp.get('msg')}")
        return False
    return True


def close_streaming(card_id: str) -> bool:
    settings_str = json.dumps({"config": {"streaming_mode": False}},
                              separators=(",", ":"))
    if _sdk_client is not None:
        from lark_oapi.api.cardkit.v1 import (
            SettingsCardRequest, SettingsCardRequestBody)
        req = (SettingsCardRequest.builder().card_id(card_id)
               .request_body(SettingsCardRequestBody.builder()
                             .settings(settings_str)
                             .uuid(str(uuid.uuid4()))
                             .sequence(9999)
                             .build())
               .build())
        try:
            resp = _sdk_client.cardkit.v1.card.settings(req)
        except Exception as exc:
            log(f"close_streaming SDK exc: {exc}")
            return False
        return _check_resp("close_streaming", resp)
    body = {"settings": settings_str, "uuid": str(uuid.uuid4()), "sequence": 9999}
    resp = lark_api("PATCH",
                    f"/open-apis/cardkit/v1/cards/{card_id}/settings",
                    data=body)
    return bool(resp) and resp.get("code", -1) == 0


def download_image(message_id: str, image_key: str) -> Optional[Path]:
    """Download an image resource from a Feishu message and save it to
    ~/larksor-tc/inbox/. Returns the local path on success."""
    if _sdk_client is None:
        log("download_image: SDK client not initialized")
        return None
    from lark_oapi.api.im.v1 import GetMessageResourceRequest
    req = (GetMessageResourceRequest.builder()
           .message_id(message_id).file_key(image_key)
           .type("image").build())
    try:
        resp = _sdk_client.im.v1.message_resource.get(req)
    except Exception as exc:
        log(f"download_image exc: {exc}")
        return None
    if not _check_resp("download_image", resp):
        return None
    raw = getattr(resp, "file", None) or getattr(resp, "raw", None)
    # lark-oapi exposes resp.file as a Python BinaryIO/bytes-like
    data: Optional[bytes] = None
    if raw is None:
        data = getattr(resp, "data", None) and bytes(resp.data) or None
    elif hasattr(raw, "read"):
        try:
            data = raw.read()
        except Exception:
            data = None
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    if not data:
        log(f"download_image: no bytes returned for {image_key[:14]}...")
        return None
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    short = image_key.split("_")[-1][:10] if "_" in image_key else image_key[:10]
    # Guess extension from first bytes (PNG/JPEG/GIF/WebP)
    ext = ".bin"
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif data[:4] == b"GIF8":
        ext = ".gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    out = INBOX_DIR / f"{ts}-{short}{ext}"
    out.write_bytes(data)
    os.chmod(out, 0o600)
    log(f"download_image -> {out} ({len(data)} bytes)")
    return out


def patch_element(card_id: str, element_id: str,
                  partial: dict, sequence: int = 10500) -> bool:
    """Partial-update an element's properties (e.g. {"expanded": False}).
    Called AFTER close_streaming (seq 9999), so we use seq >= 10000 to keep
    the strictly-increasing sequence number constraint happy."""
    partial_str = json.dumps(partial, ensure_ascii=False,
                             separators=(",", ":"))
    if _sdk_client is not None:
        from lark_oapi.api.cardkit.v1 import (
            PatchCardElementRequest, PatchCardElementRequestBody)
        req = (PatchCardElementRequest.builder()
               .card_id(card_id).element_id(element_id)
               .request_body(PatchCardElementRequestBody.builder()
                             .partial_element(partial_str)
                             .uuid(str(uuid.uuid4()))
                             .sequence(sequence)
                             .build())
               .build())
        try:
            resp = _sdk_client.cardkit.v1.card_element.patch(req)
        except Exception as exc:
            log(f"patch_element SDK exc: {exc}")
            return False
        return _check_resp(f"patch_element({element_id})", resp)
    body = {"partial_element": partial_str,
            "uuid": str(uuid.uuid4()), "sequence": sequence}
    resp = lark_api("PATCH",
                    f"/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}",
                    data=body)
    return bool(resp) and resp.get("code", -1) == 0


def append_actions(card_id: str, chat_id: Optional[str], model: str,
                   sequence: int = 10001) -> bool:
    """Append a row of buttons below the footer for retry / new / pin / stop.

    CardKit "新增组件" API: POST /open-apis/cardkit/v1/cards/:card_id/elements
    Body fields: type, target_element_id, uuid, sequence (int, strictly
    increasing), elements (JSON-string of an array of v2 components).
    """
    elements = [{
        "tag": "column_set",
        "element_id": "actions",
        "columns": [
            _btn_column("primary_text", "🔁 重试",
                        {"a": "retry", "chat": chat_id}),
            _btn_column("default", "🆕 新会话",
                        {"a": "new"}),
            _btn_column("default", "📌 置顶 chat",
                        {"a": "pin", "chat": chat_id}),
            _btn_column("danger_text", "⏹ 停止",
                        {"a": "cancel", "chat": chat_id}),
        ],
    }]
    elements_str = json.dumps(elements, ensure_ascii=False,
                              separators=(",", ":"))
    if _sdk_client is not None:
        from lark_oapi.api.cardkit.v1 import (
            CreateCardElementRequest, CreateCardElementRequestBody)
        req = (CreateCardElementRequest.builder().card_id(card_id)
               .request_body(CreateCardElementRequestBody.builder()
                             .type("insert_after")
                             .target_element_id("footer")
                             .uuid(str(uuid.uuid4()))
                             .sequence(sequence)
                             .elements(elements_str)
                             .build())
               .build())
        try:
            resp = _sdk_client.cardkit.v1.card_element.create(req)
        except Exception as exc:
            log(f"append_actions SDK exc: {exc}")
            return False
        return _check_resp("append_actions", resp)
    body = {
        "type": "insert_after",
        "target_element_id": "footer",
        "uuid": str(uuid.uuid4()),
        "sequence": sequence,
        "elements": elements_str,
    }
    resp = lark_api("POST",
                    f"/open-apis/cardkit/v1/cards/{card_id}/elements",
                    data=body)
    if not resp or resp.get("code", -1) != 0:
        if resp:
            log(f"append_actions error: code={resp.get('code')} msg={resp.get('msg')}")
        return False
    return True


def _btn_column(typ: str, text: str, value: dict) -> dict:
    """One button wrapped in a column so the buttons sit on a single row."""
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "elements": [{
            "tag": "button",
            "type": typ,
            "size": "medium",
            "width": "default",
            "text": {"tag": "plain_text", "content": text},
            "behaviors": [{
                "type": "callback",
                "value": value,
            }],
        }],
    }


# ----------------------------------------------------------------------------
# Card content templates
# ----------------------------------------------------------------------------

def abbreviate(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def _fmt_k(n: int) -> str:
    if n is None:
        return "0"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def render_meta(chat_id: Optional[str], model: str, workspace: str,
                mode: Optional[str], chat_title: Optional[str] = None,
                usage: Optional[dict] = None,
                elapsed_s: Optional[float] = None,
                running: bool = True) -> str:
    short_id = (chat_id or "new")[:8]
    if chat_title:
        title_disp = chat_title[:30] + ("…" if len(chat_title) > 30 else "")
        chat_bit = f"💬 **{title_disp}**  `{short_id}`"
    else:
        chat_bit = f"💬 `{short_id}`"
    bits = [
        chat_bit,
        f"🧠 `{model}`",
        f"📁 `{abbreviate(workspace)}`",
    ]
    if mode:
        bits.append(f"🏷 `{mode}`")
    if usage:
        in_tok = _fmt_k(usage.get("inputTokens", 0))
        out_tok = _fmt_k(usage.get("outputTokens", 0))
        tok_bit = f"🪙 in {in_tok} / out {out_tok}"
        if usage.get("cacheReadTokens"):
            tok_bit += f" / cache {_fmt_k(usage['cacheReadTokens'])}"
        bits.append(tok_bit)
    if elapsed_s is not None:
        bits.append(f"⏱ {elapsed_s:.1f}s")
    elif running:
        bits.append("⏱ _running..._")
    return "  ·  ".join(bits)


def render_tools(tools: list[dict]) -> str:
    if not tools:
        return "_no tool calls_"
    lines = []
    for t in tools:
        status = t.get("status_icon") or "•"
        line = f"{status} {t.get('icon', '')} {t.get('label', '')}".rstrip()
        if t.get("elapsed_ms") not in (None, 0):
            line += f"  _{t['elapsed_ms']/1000:.1f}s_"
        if t.get("rc") not in (None, 0):
            line += f"  ⚠️ rc={t['rc']}"
        if t.get("error"):
            line += f"  ⚠️ {str(t['error'])[:80]}"
        lines.append("- " + line)
    return "\n".join(lines)


def render_tools_header(tools: list[dict], done: bool = False) -> str:
    if not tools:
        return "🔧 _no tool calls yet_"
    n = len(tools)
    total_ms = sum(t.get("elapsed_ms") or 0 for t in tools)
    failed = sum(1 for t in tools
                 if t.get("status_icon") == "✗" or t.get("error"))
    bits = [f"🔧 {n} tool calls"]
    if total_ms:
        bits.append(f"{total_ms/1000:.1f}s")
    if failed:
        bits.append(f"⚠️ {failed} failed")
    return "  ·  ".join(bits)


def render_thinking_header(chunks_total: int, elapsed_s: Optional[float],
                            done: bool) -> str:
    if not chunks_total:
        return "💭 _thinking..._"
    if done:
        bits = [f"💭 thought · {chunks_total} chars"]
        if elapsed_s is not None:
            bits.append(f"{elapsed_s:.1f}s")
        return "  ·  ".join(bits)
    return f"💭 thinking... ({chunks_total} chars)"


def render_footer(result_evt: dict, started: float) -> str:
    usage = result_evt.get("usage") or {}
    elapsed = time.time() - started
    parts = [
        f"⏱ {elapsed:.1f}s",
        f"in {usage.get('inputTokens', 0)} / out {usage.get('outputTokens', 0)}",
    ]
    if usage.get("cacheReadTokens"):
        parts.append(f"cache {usage['cacheReadTokens']}")
    sid = result_evt.get("session_id") or ""
    if sid:
        parts.append(f"chat `{sid[:8]}`")
    if result_evt.get("is_error"):
        parts.insert(0, "⚠️ error")
    return " · ".join(parts)


def build_initial_card(chat_id: Optional[str], model: str,
                       workspace: str, mode: Optional[str],
                       chat_title: Optional[str] = None) -> dict:
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "summary": {"content": "🤖 生成中..."},
            "streaming_config": {
                "print_frequency_ms": {"default": 30},
                "print_step": {"default": 2},
                "print_strategy": "fast",
            },
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🤖 {TENANT} Agent",
            },
            "subtitle": {
                "tag": "plain_text",
                "content": f"powered by cursor-cli · {model}",
            },
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "element_id": "meta",
                 "content": render_meta(chat_id, model, workspace, mode,
                                        chat_title=chat_title)},
                {
                    "tag": "collapsible_panel",
                    "element_id": "thinking_panel",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "markdown",
                            "element_id": "thinking_header",
                            "content": render_thinking_header(0, None, False),
                        },
                        "vertical_align": "center",
                    },
                    "elements": [
                        {"tag": "markdown",
                         "element_id": "thinking_content", "content": ""},
                    ],
                },
                {
                    # Tools panel - expanded during run so user sees live
                    # progress; we auto-collapse it after the result event so
                    # the answer dominates once the run is done.
                    "tag": "collapsible_panel",
                    "element_id": "tools_panel",
                    "expanded": True,
                    "header": {
                        "title": {
                            "tag": "markdown",
                            "element_id": "tools_header",
                            "content": render_tools_header([]),
                        },
                        "vertical_align": "center",
                    },
                    "elements": [
                        {"tag": "markdown",
                         "element_id": "tools", "content": ""},
                    ],
                },
                {"tag": "markdown", "element_id": "answer", "content": ""},
            ],
        },
    }


# ----------------------------------------------------------------------------
# Async card updater: coalesces frequent writes to ~5 Hz per element
# ----------------------------------------------------------------------------

class CardUpdater:
    def __init__(self, card_id: str):
        self.card_id = card_id
        self.pending: dict[str, str] = {}
        self.last_pushed: dict[str, str] = {}
        self.seq = 0
        self.lock = threading.Lock()
        self.alive = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def set(self, element_id: str, content: str) -> None:
        with self.lock:
            self.pending[element_id] = content

    def _next_seq(self) -> int:
        with self.lock:
            self.seq += 1
            return self.seq

    def _flush_once(self) -> None:
        with self.lock:
            snapshot = dict(self.pending)
        for eid, content in snapshot.items():
            if self.last_pushed.get(eid) == content:
                continue
            seq = self._next_seq()
            if stream_text(self.card_id, eid, content, seq):
                self.last_pushed[eid] = content

    def _loop(self) -> None:
        while self.alive:
            time.sleep(PUSH_INTERVAL_S)
            try:
                self._flush_once()
            except Exception as exc:
                log(f"updater loop error: {exc}")

    def stop(self) -> None:
        self.alive = False
        try:
            self.t.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._flush_once()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Job + slash commands + Chinese aliases
# ----------------------------------------------------------------------------

@dataclass
class Job:
    open_id: str
    text: str
    mode: Optional[str] = None  # None | "plan" | "ask"
    force_new_chat: bool = False


CHINESE_MODEL_RE = re.compile(r"^(?:换|切换|切到|用)\s*(?:模型\s*)?"
                              r"([A-Za-z0-9][\w\.\-]*)\s*(?:模型)?$")

# Path must START with ~ or / so "切到 opus" isn't mistakenly routed to /cd
NL_CD_RE = re.compile(
    r"^(?:切到|切换到|切换工作目录(?:到)?|cd\s*到|"
    r"工作目录(?:改成|换成|切到)|workspace\s+(?:to|=))"
    r"\s*[`'\"]?([~/]\S+?)[`'\"]?\s*$",
    re.IGNORECASE)


def normalize_command(text: str) -> Optional[tuple[str, str]]:
    text = text.strip()
    m = NL_CD_RE.match(text)
    if m:
        return ("/cd", m.group(1))
    m = CHINESE_MODEL_RE.match(text)
    if m:
        return ("/model", m.group(1))
    return None


HELP = """[Larksor-TC · CardKit streaming + SQLite]
plain text             -> next turn of current chat (or new chat if none)
/status                show chat_id / model / queue / pending includes
/new                   start a fresh chat (drop chat_id)
/model <name>          switch model (auto / opus / sonnet / gpt5 / ...)
/plan <prompt>         run once in plan mode (read-only / planning)
/ask  <prompt>         run once in ask mode (Q&A read-only)
/include <path>        attach a Mac file to the NEXT prompt
/included              list pending includes
/clear-include         drop all pending includes
/retry                 re-run the last prompt
/cancel                SIGINT the currently running agent
/cost [today|week|all] token usage summary
/history [N]           recent N turns (default 5)
/ls                    list recent chats
/resume <chat_id|N>    switch active chat (N = position from /ls)
/cd <path>             change workspace for THIS chat (~ and relative ok)
/cd reset              go back to global default workspace
/pwd                   print current workspace
/help                  this help

Chinese shortcuts:
  换模型 auto  /  切换到 sonnet-thinking  /  用 opus-thinking 模型

Card layout:
  meta (chat · model · workspace · tokens · elapsed)
  ▸ 💭 thinking  (collapsed; auto-fills if model emits reasoning)
  ▸ 🔧 tool calls  (live + expanded while running, auto-collapses on result)
  answer  (streaming)

Notes:
  - opus-4.7-* models require maxMode=true in ~/.cursor/cli-config.json
    (without maxMode, bridge defaults to sonnet-thinking)
  - workspace defaults to $HOME so the agent can read any file under ~
"""


MAX_INCLUDE_BYTES = 64 * 1024  # cap one file at 64 KiB to keep prompts sane


def cmd_include(arg: str, state: dict, open_id: str) -> None:
    path_str = arg.strip()
    if not path_str:
        lark_send_text(open_id, "[bridge] usage: /include <path>")
        return
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = Path(current_workspace(state)) / p
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        lark_send_text(open_id, f"[bridge] not found: {p}")
        return
    except Exception as exc:
        lark_send_text(open_id, f"[bridge] read error {p}: {exc}")
        return
    truncated = False
    if len(raw) > MAX_INCLUDE_BYTES:
        raw = raw[:MAX_INCLUDE_BYTES]
        truncated = True
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    entry = {"path": str(p), "content": content,
             "truncated": truncated, "size": len(raw)}
    state.setdefault("pending_includes", []).append(entry)
    save_state(state)
    suffix = " (truncated to 64 KiB)" if truncated else ""
    lark_send_text(
        open_id,
        f"[bridge] included `{abbreviate(str(p))}` "
        f"({len(raw)} bytes{suffix}). "
        f"Will be attached to next prompt. "
        f"Total queued: {len(state['pending_includes'])}")


def cmd_included(state: dict, open_id: str) -> None:
    items = state.get("pending_includes") or []
    if not items:
        lark_send_text(open_id, "[bridge] no pending includes.")
        return
    lines = ["[bridge] pending includes (will attach to next prompt):"]
    for i, it in enumerate(items, 1):
        suffix = " (truncated)" if it.get("truncated") else ""
        lines.append(f"  {i}. {abbreviate(it['path'])}  ({it.get('size', 0)} B{suffix})")
    lark_send_text(open_id, "\n".join(lines))


def cmd_clear_include(state: dict, open_id: str) -> None:
    n = len(state.get("pending_includes") or [])
    state.pop("pending_includes", None)
    save_state(state)
    lark_send_text(open_id, f"[bridge] cleared {n} pending include(s).")


def consume_includes(state: dict, prompt: str) -> tuple[str, list[dict]]:
    """Prefix `prompt` with any queued includes, drain them, and return
    (augmented_prompt, included_items).

    Two flavors:
      - text/code files: embed full contents inline under <file>...</file>
      - images: reference path only, ask the agent to read the file with
        its `read` tool (cursor-cli will pull the image into model context
        when the model is multimodal-capable, e.g. opus-4.7).
    """
    items = state.get("pending_includes") or []
    if not items:
        return prompt, []

    file_blocks: list[str] = []
    image_paths: list[str] = []
    for it in items:
        if it.get("kind") == "image":
            image_paths.append(it["path"])
        else:
            suffix = "  (truncated)" if it.get("truncated") else ""
            file_blocks.append(f'<file path="{it["path"]}"{suffix}>\n'
                               f'{it["content"]}\n</file>')

    parts: list[str] = []
    if file_blocks:
        parts.append("<files>")
        parts.extend(file_blocks)
        parts.append("</files>")
    if image_paths:
        parts.append("<images>")
        for p in image_paths:
            parts.append(f'<image path="{p}" />')
        parts.append("</images>")
        parts.append(f"(Please open the image(s) above with the `read` "
                     f"tool to see them — they were sent by the user "
                     f"alongside this prompt.)")
    parts.append("")
    parts.append(prompt)
    state.pop("pending_includes", None)
    save_state(state)
    return "\n".join(parts), items


# ----------------------------------------------------------------------------
# Agent stream handling
# ----------------------------------------------------------------------------

def build_agent_args(state: dict, job: Job, prompt: str) -> list[str]:
    """Build agent CLI args; `prompt` is the FINAL text (already including any
    file include block)."""
    model = state.get("model") or DEFAULT_MODEL
    args = ["agent", "-p", "--force",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--model", model,
            "--workspace", current_workspace(state)]
    if state.get("chat_id") and not job.force_new_chat:
        args += ["--resume", state["chat_id"]]
    if job.mode == "plan":
        args += ["--mode", "plan"]
    elif job.mode == "ask":
        args += ["--mode", "ask"]
    args.append(prompt)
    return args


def extract_tool_info(evt: dict) -> dict:
    """Normalize a tool_call event into {id, kind, icon, label,
    elapsed_ms, rc, error}, dispatching by the inner tool-call key."""
    call_id = evt.get("call_id")
    tc = evt.get("tool_call") or {}
    for kind, body in tc.items():
        if not isinstance(body, dict):
            continue
        return _format_tool(kind, body, call_id)
    return {"id": call_id, "kind": "unknown", "icon": "•",
            "label": "tool", "elapsed_ms": None, "rc": None, "error": None}


def _format_tool(kind: str, body: dict, call_id: Optional[str]) -> dict:
    args = body.get("args") or {}
    result = body.get("result") or {}
    success = result.get("success") if isinstance(result, dict) else None
    success = success or {}
    error = None
    if isinstance(result, dict):
        error = (result.get("error") or result.get("failure")
                 or result.get("rejection"))
    elapsed_ms = (success.get("localExecutionTimeMs")
                  or success.get("executionTime"))
    rc = None
    icon = "•"
    label = f"`{kind}`"

    if kind == "shellToolCall":
        cmd = (args.get("command") or "").strip()
        desc = args.get("description") or body.get("description") or ""
        rc = success.get("exitCode") if success else None
        label = f"`{cmd[:120]}`"
        if desc and desc != cmd:
            label += f"  —  _{desc[:80]}_"
        icon = "$"
    elif kind == "editToolCall":
        path = args.get("path") or "?"
        added = success.get("linesAdded")
        removed = success.get("linesRemoved")
        diff = ""
        if added is not None or removed is not None:
            diff = f" (+{added or 0}/-{removed or 0})"
        label = f"edit `{abbreviate(path)}`{diff}"
        icon = "✏️"
    elif kind == "writeToolCall":
        path = args.get("path") or args.get("targetFile") or "?"
        bytes_n = success.get("bytesWritten")
        suffix = f" ({bytes_n}B)" if bytes_n else ""
        label = f"write `{abbreviate(path)}`{suffix}"
        icon = "💾"
    elif kind == "readToolCall":
        path = args.get("path") or "?"
        total = success.get("totalLines")
        suffix = f" ({total} lines)" if total else ""
        label = f"read `{abbreviate(path)}`{suffix}"
        icon = "📖"
    elif kind == "globToolCall":
        gp = args.get("globPattern") or args.get("pattern") or "?"
        td = args.get("targetDirectory") or ""
        n = success.get("totalFiles")
        suffix = f"  →  {n} files" if n is not None else ""
        scope = f" in `{abbreviate(td)}`" if td else ""
        label = f"glob `{gp}`{scope}{suffix}"
        icon = "🗂"
    elif kind == "grepToolCall":
        p = args.get("pattern") or "?"
        glob = args.get("glob") or ""
        path = args.get("path") or ""
        scope_bits = []
        if glob:
            scope_bits.append(f"`{glob}`")
        if path:
            scope_bits.append(f"in `{abbreviate(path)}`")
        scope = "  " + " ".join(scope_bits) if scope_bits else ""
        label = f"grep `{p}`{scope}"
        icon = "🔎"
    elif kind == "webSearchToolCall":
        q = args.get("query") or args.get("queryText") or "?"
        label = f"web search `{q[:80]}`"
        icon = "🌐"
    elif kind == "fetchToolCall":
        url = args.get("url") or "?"
        label = f"fetch `{url[:100]}`"
        icon = "🔗"
    elif kind == "todoWriteToolCall":
        todos = args.get("todos") or []
        label = f"update todos ({len(todos)})"
        icon = "📋"
    elif kind == "mcpToolCall":
        server = args.get("serverName") or args.get("server") or ""
        tname = args.get("toolName") or args.get("name") or "?"
        label = f"mcp `{server}/{tname}`" if server else f"mcp `{tname}`"
        icon = "🔌"
    else:
        nice = kind.removesuffix("ToolCall") if hasattr(kind, "removesuffix") else kind
        label = f"`{nice}`"
        icon = "•"

    return {
        "id": call_id,
        "kind": kind,
        "icon": icon,
        "label": label,
        "elapsed_ms": elapsed_ms,
        "rc": rc,
        "error": error,
    }


def stream_agent_into_card(state: dict, job: Job, card_id: str,
                           updater: CardUpdater, prompt: str) -> dict:
    """Run the agent and stream events into the given card. Returns the
    final result event dict (or a synthetic error dict)."""

    global current_proc
    args = build_agent_args(state, job, prompt)
    log(f"run model={state.get('model')} mode={job.mode} chat={state.get('chat_id')} "
        f"text={prompt[:60]!r}")
    started = time.time()
    answer_chunks: list[str] = []
    tools_state: list[dict] = []
    tools_index: dict[str, int] = {}
    thinking_chunks: list[str] = []
    thinking_started_at: Optional[float] = None
    agent_status_lines: list[str] = []  # T:/S: messages from agent (errors etc.)
    final_event: dict = {"is_error": True, "result": "(no result event)"}

    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                bufsize=1)
    except FileNotFoundError as exc:
        return {"is_error": True, "result": f"[bridge] cannot launch agent: {exc}"}

    with proc_lock:
        current_proc = proc

    HARD_TIMEOUT_S = int(os.environ.get("BRIDGE_AGENT_TIMEOUT_S", "900"))
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.time() - started > HARD_TIMEOUT_S:
                log(f"agent hard-timeout after {HARD_TIMEOUT_S}s, killing")
                proc.kill()
                final_event = {"is_error": True,
                               "result": f"(bridge timeout after {HARD_TIMEOUT_S}s)"}
                updater.set("answer", final_event["result"])
                break
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                # Non-JSON lines like "T: [resource_exhausted] Error" or
                # "S: Max Mode Required ..." are agent-level status / error
                # messages. We collect them so we can surface a useful card
                # error instead of just "agent exited rc=1".
                if line.startswith(("T:", "S:", "E:", "W:")):
                    agent_status_lines.append(line)
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            t = evt.get("type")
            sub = evt.get("subtype")

            if t == "tool_call":
                info = extract_tool_info(evt)
                if sub == "started":
                    info["status_icon"] = "⚙"
                    tools_index[info["id"]] = len(tools_state)
                    tools_state.append(info)
                elif sub == "completed":
                    idx = tools_index.get(info["id"])
                    failed = (info.get("rc") not in (None, 0)) or info.get("error")
                    if idx is not None:
                        prev = tools_state[idx]
                        # keep icon/label from started, layer in result fields
                        for k in ("elapsed_ms", "rc", "error"):
                            if info.get(k) is not None:
                                prev[k] = info[k]
                        prev["status_icon"] = "✗" if failed else "✓"
                    else:
                        info["status_icon"] = "✗" if failed else "✓"
                        tools_state.append(info)
                updater.set("tools", render_tools(tools_state))
                updater.set("tools_header", render_tools_header(tools_state))

            elif t == "thinking":
                if sub == "delta":
                    if thinking_started_at is None:
                        thinking_started_at = time.time()
                    txt = evt.get("text") or ""
                    if txt:
                        thinking_chunks.append(txt)
                        updater.set("thinking_content", "".join(thinking_chunks))
                        updater.set("thinking_header",
                                    render_thinking_header(
                                        sum(len(c) for c in thinking_chunks),
                                        None, False))
                elif sub == "completed":
                    elapsed_s = (time.time() - thinking_started_at) \
                        if thinking_started_at else None
                    updater.set("thinking_header",
                                render_thinking_header(
                                    sum(len(c) for c in thinking_chunks),
                                    elapsed_s, True))
                    # reset for the next thinking burst within same turn
                    thinking_started_at = None

            elif t == "assistant":
                msg = evt.get("message") or {}
                contents = msg.get("content") or []
                # Treat events WITH model_call_id as aggregated finals - skip
                # to avoid double-counting. Per-token deltas have timestamp_ms
                # only.
                if evt.get("model_call_id"):
                    # If we never accumulated anything (very short reply),
                    # adopt the aggregated text once.
                    if not answer_chunks:
                        for c in contents:
                            if c.get("type") == "text":
                                answer_chunks.append(c.get("text") or "")
                else:
                    for c in contents:
                        if c.get("type") == "text":
                            answer_chunks.append(c.get("text") or "")
                updater.set("answer", "".join(answer_chunks))

            elif t == "result":
                final_event = evt
                if evt.get("session_id"):
                    state["chat_id"] = evt["session_id"]
                    save_state(state)
                # Final answer: prefer the explicit `result` field, fall back
                # to accumulated deltas.
                final_text = (evt.get("result") or "").strip() or \
                             "".join(answer_chunks).strip() or \
                             "(empty result)"
                updater.set("answer", final_text)
                elapsed_s = time.time() - started
                # Update meta to carry the token + elapsed info (which used
                # to live in a footer line).
                updater.set(
                    "meta",
                    render_meta(state.get("chat_id"),
                                state.get("model") or DEFAULT_MODEL,
                                current_workspace(state), job.mode,
                                chat_title=_db.get_chat_title(state.get("chat_id")),
                                usage=evt.get("usage"),
                                elapsed_s=elapsed_s,
                                running=False),
                )
                # Finalize tools header (with totals) and thinking header.
                updater.set("tools_header",
                            render_tools_header(tools_state, done=True))
                if not thinking_chunks:
                    updater.set("thinking_header",
                                "💭 _(no reasoning trace from this model)_")
                break

            elif t == "system":
                pass  # ignored

        # drain remainder briefly
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        # If we never saw a `result` event, surface that in the card so the
        # user doesn't end up staring at a permanent "thinking..." spinner.
        if final_event.get("result") in (None, "", "(no result event)"):
            elapsed = time.time() - started
            rc = proc.returncode
            tail = "agent exited without emitting a result event"
            if rc not in (None, 0):
                tail = f"agent exited rc={rc}"

            joined_status = "\n".join(agent_status_lines).strip()
            if joined_status:
                tail = f"{tail}\n```\n{joined_status[:600]}\n```"

            # Useful hint for the very common case where Cursor backend
            # bails on resuming an old chat thread.
            hint = ""
            if "resource_exhausted" in joined_status.lower():
                hint = ("\n\n💡 Cursor backend returned `resource_exhausted`. "
                        "Often happens when resuming a long chat with a "
                        "premium model. Try `/new` to start a fresh chat.")
            elif "max mode required" in joined_status.lower():
                hint = ("\n\n💡 Enable Max Mode in `~/.cursor/cli-config.json` "
                        "(`maxMode: true`) or switch to a non-Max model with "
                        "`/model sonnet-thinking`.")

            body = ("".join(answer_chunks).strip() or
                    f"⚠️ {tail}{hint}")
            updater.set("answer", body)
            # Surface failure in the meta line since the footer element is gone.
            updater.set(
                "meta",
                "⚠️ **agent failed**  ·  " +
                render_meta(state.get("chat_id"),
                            state.get("model") or DEFAULT_MODEL,
                            current_workspace(state), job.mode,
                            chat_title=_db.get_chat_title(state.get("chat_id")),
                            elapsed_s=elapsed, running=False),
            )
    finally:
        with proc_lock:
            current_proc = None

    return final_event


# ----------------------------------------------------------------------------
# Top-level message handling
# ----------------------------------------------------------------------------

def run_agent_card(state: dict, job: Job) -> None:
    model = state.get("model") or DEFAULT_MODEL
    workspace = current_workspace(state)
    prompt, includes = consume_includes(state, job.text)

    card_json = build_initial_card(
        state.get("chat_id"), model, workspace, job.mode,
        chat_title=_db.get_chat_title(state.get("chat_id")))
    card_id = create_card(card_json)
    if not card_id:
        log("CardKit unavailable, falling back to plain text reply")
        run_agent_plain(state, job)
        return

    msg_id = send_card(job.open_id, card_id)
    if not msg_id:
        log("send_card failed; using plain text fallback")
        run_agent_plain(state, job)
        return

    turn_id = _db.turn_start(state.get("chat_id"), job.open_id, model,
                             job.mode, prompt, card_id=card_id)
    if includes:
        _db.add_includes(turn_id, includes)

    current_card_ctx[job.open_id] = {"card_id": card_id, "started_at": time.time(),
                                     "turn_id": turn_id}

    updater = CardUpdater(card_id)
    final: dict = {}
    try:
        final = stream_agent_into_card(state, job, card_id, updater, prompt)
    except Exception as exc:
        final = {"is_error": True, "result": f"(bridge crash: {exc})"}
        log(f"run_agent_card crash: {exc}")
    finally:
        updater.stop()
        close_streaming(card_id)

    # Auto-collapse the tool-calls panel after the run finishes (matches
    # the Cursor IDE behavior of hiding completed tool blocks).
    try:
        patch_element(card_id, "tools_panel", {"expanded": False})
    except Exception as exc:
        log(f"collapse tools_panel failed: {exc}")

    # persist this turn's outcome
    new_chat_id = state.get("chat_id")
    _db.turn_end(turn_id, chat_id=new_chat_id,
                 result=(final.get("result") or "")[:8000],
                 usage=final.get("usage"),
                 error=("error" if final.get("is_error") else None))
    if new_chat_id:
        _db.upsert_chat(new_chat_id, job.open_id, model, workspace)
        # On the very first turn of a chat, derive a human-readable title
        # from the prompt so /ls and the meta line don't only show the uuid.
        if not _db.get_chat_title(new_chat_id):
            title = job.text.strip().splitlines()[0][:40] if job.text else ""
            if title:
                _db.set_chat_title(new_chat_id, title)


def run_agent_plain(state: dict, job: Job) -> None:
    """Legacy non-streaming fallback: single JSON run, return result."""
    model = state.get("model") or DEFAULT_MODEL
    prompt, includes = consume_includes(state, job.text)
    turn_id = _db.turn_start(state.get("chat_id"), job.open_id, model,
                             job.mode, prompt)
    if includes:
        _db.add_includes(turn_id, includes)
    args = ["agent", "-p", "--force", "--output-format", "json",
            "--model", model, "--workspace", current_workspace(state)]
    if state.get("chat_id") and not job.force_new_chat:
        args += ["--resume", state["chat_id"]]
    if job.mode == "plan":
        args += ["--mode", "plan"]
    elif job.mode == "ask":
        args += ["--mode", "ask"]
    args.append(prompt)

    global current_proc
    lark_send_text(job.open_id, f"[bridge] (plain mode) thinking with {model}...")
    started = time.time()
    try:
        with proc_lock:
            current_proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
        stdout, stderr = current_proc.communicate(timeout=1800)
        rc = current_proc.returncode
    except Exception as exc:
        lark_send_text(job.open_id, f"[bridge] launch error: {exc}")
        return
    finally:
        with proc_lock:
            current_proc = None

    elapsed = time.time() - started
    payload = None
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    if not payload:
        _db.turn_end(turn_id, chat_id=state.get("chat_id"),
                     result=None, usage=None, error=f"no JSON result rc={rc}")
        lark_send_text(job.open_id,
                       f"[bridge] no JSON result (rc={rc}) in {elapsed:.1f}s\n"
                       + ((stdout or "")[-1500:] or (stderr or "")[-1500:]))
        return
    if payload.get("session_id"):
        state["chat_id"] = payload["session_id"]
        save_state(state)
        _db.upsert_chat(state["chat_id"], job.open_id, model, current_workspace(state))
    body = (payload.get("result") or "").strip() or "(empty result)"
    usage = payload.get("usage") or {}
    _db.turn_end(turn_id, chat_id=state.get("chat_id"),
                 result=body[:8000], usage=usage,
                 error=("error" if payload.get("is_error") else None))
    footer = (f"\n\n---\n{model} · {elapsed:.1f}s · "
              f"in {usage.get('inputTokens', 0)} out {usage.get('outputTokens', 0)} · "
              f"chat {(state.get('chat_id') or '')[:8]}")
    lark_send_text(job.open_id, body + footer)


def handle(state: dict, job: Job, q: "queue.Queue[Job]") -> None:
    text = job.text.strip()
    if not text:
        return

    nat = normalize_command(text)
    if nat:
        cmd, arg = nat
    elif text.startswith("/"):
        parts = text.split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
    else:
        cmd = ""
        arg = text

    if cmd == "/help":
        lark_send_text(job.open_id, HELP)
        return
    if cmd == "/status":
        pending = state.get("pending_includes") or []
        inc_summary = "(none)"
        if pending:
            inc_summary = ", ".join(
                f"{abbreviate(it['path'])}({it.get('size',0)}B)"
                for it in pending)
        lark_send_text(
            job.open_id,
            f"chat_id : {state.get('chat_id') or '(none)'}\n"
            f"model   : {state.get('model') or DEFAULT_MODEL}\n"
            f"queue   : {q.qsize()}\n"
            f"running : {current_proc is not None}\n"
            f"workspace: {current_workspace(state)} (global default: {WORKSPACE})\n"
            f"tenant  : {TENANT}\n"
            f"includes: {len(pending)} -> {inc_summary}")
        return
    if cmd == "/include":
        cmd_include(arg, state, job.open_id)
        return
    if cmd == "/included":
        cmd_included(state, job.open_id)
        return
    if cmd == "/clear-include" or cmd == "/clear-includes":
        cmd_clear_include(state, job.open_id)
        return
    if cmd == "/new":
        state.pop("chat_id", None)
        save_state(state)
        lark_send_text(job.open_id, "[bridge] cleared chat. next message starts a new conversation.")
        return
    if cmd == "/model":
        if not arg:
            lark_send_text(
                job.open_id,
                "[bridge] usage: /model <name>\n"
                "aliases: opus | opus-thinking | opus-max | "
                "sonnet | sonnet-thinking | gpt5 | codex | auto\n"
                "or any full cursor id (see `agent models`)")
            return
        resolved = resolve_model(arg)
        state["model"] = resolved
        save_state(state)
        if resolved != arg:
            lark_send_text(job.open_id, f"[bridge] model -> {resolved}  (alias `{arg}`)")
        else:
            lark_send_text(job.open_id, f"[bridge] model -> {resolved}")
        return
    if cmd == "/retry":
        last = state.get("last_prompt")
        if not last:
            lark_send_text(job.open_id, "[bridge] no last prompt to retry")
            return
        run_agent_card(state, Job(job.open_id, last, mode=job.mode))
        return
    if cmd == "/cancel":
        with proc_lock:
            if current_proc is None:
                lark_send_text(job.open_id, "[bridge] nothing running")
                return
            try:
                current_proc.send_signal(signal.SIGINT)
                lark_send_text(job.open_id, "[bridge] sent SIGINT")
            except Exception as exc:
                lark_send_text(job.open_id, f"[bridge] cancel error: {exc}")
        return
    if cmd == "/cost":
        scope = arg.lower().strip() or "today"
        now = time.time()
        since = None
        if scope == "today":
            since = now - 86400
        elif scope == "week":
            since = now - 7 * 86400
        elif scope == "all":
            since = None
        else:
            lark_send_text(job.open_id, "[bridge] usage: /cost [today|week|all]")
            return
        s = _db.cost_summary(open_id=job.open_id, since_unix=since)
        lark_send_text(
            job.open_id,
            f"[/cost {scope}]\n"
            f"  turns : {s['turn_count']}\n"
            f"  in    : {s['in_tokens']:>9} tokens\n"
            f"  out   : {s['out_tokens']:>9} tokens\n"
            f"  cache : {s['cache_read_tokens']:>9} tokens (read)")
        return
    if cmd == "/history":
        try:
            n = int(arg) if arg else 5
        except ValueError:
            n = 5
        rows = _db.recent_turns(open_id=job.open_id, limit=n)
        if not rows:
            lark_send_text(job.open_id, "[bridge] no turns yet")
            return
        lines = [f"[history · last {len(rows)}]"]
        for r in rows:
            chat = (r["chat_id"] or "")[:8] or "(new)"
            dur = f"{r['dur_s']:.1f}s" if r["dur_s"] else "?"
            err = " ⚠️" if r["error"] else ""
            lines.append(
                f"  {r['started']}  {chat}  {r['model']}{err}  "
                f"{dur}  in {r['in_tokens']}/out {r['out_tokens']}\n"
                f"    > {r['prompt']!r}")
        lark_send_text(job.open_id, "\n".join(lines))
        return
    if cmd == "/ls":
        chats = _db.list_chats(open_id=job.open_id, limit=10)
        if not chats:
            lark_send_text(job.open_id, "[bridge] no chats yet")
            return
        active = state.get("chat_id") or ""
        lines = ["[chats · last 10]"]
        for i, c in enumerate(chats, 1):
            mark = "►" if c["chat_id"] == active else " "
            title = c["title"] or "(untitled)"
            lines.append(
                f"  {mark}{i:>2}. {c['chat_id'][:8]}  {c['used']}  "
                f"{c['model'] or '?'}  turns={c['turn_count']}  {title}")
        lines.append("\nuse  /resume <N>  or  /resume <chat_id>  to switch")
        lark_send_text(job.open_id, "\n".join(lines))
        return
    if cmd == "/cd":
        if not arg:
            lark_send_text(
                job.open_id,
                f"[bridge] current workspace: {current_workspace(state)}\n"
                f"usage: /cd <path>  (absolute or ~/relative)\n"
                f"       /cd reset   (back to global default {WORKSPACE})")
            return
        if arg in ("reset", "default", "-"):
            state.pop("workspace", None)
            save_state(state)
            lark_send_text(job.open_id, f"[bridge] workspace -> {WORKSPACE} (default)")
            return
        p = Path(arg).expanduser()
        if not p.is_absolute():
            p = Path(current_workspace(state)) / p
        try:
            p = p.resolve()
        except Exception:
            pass
        if not p.exists() or not p.is_dir():
            lark_send_text(job.open_id, f"[bridge] not a directory: {p}")
            return
        state["workspace"] = str(p)
        save_state(state)
        if state.get("chat_id"):
            _db.upsert_chat(state["chat_id"], job.open_id,
                            state.get("model"), str(p))
        lark_send_text(job.open_id, f"[bridge] workspace -> {p}")
        return
    if cmd == "/pwd":
        lark_send_text(job.open_id, f"[bridge] {current_workspace(state)}")
        return
    if cmd == "/resume":
        if not arg:
            lark_send_text(job.open_id, "[bridge] usage: /resume <chat_id|N>")
            return
        target = arg.strip()
        if target.isdigit():
            chats = _db.list_chats(open_id=job.open_id, limit=20)
            idx = int(target) - 1
            if 0 <= idx < len(chats):
                target = chats[idx]["chat_id"]
            else:
                lark_send_text(job.open_id, f"[bridge] no chat at position {target}")
                return
        state["chat_id"] = target
        save_state(state)
        lark_send_text(job.open_id, f"[bridge] active chat -> {target[:8]}")
        return
    if cmd == "/plan":
        if not arg:
            lark_send_text(job.open_id, "[bridge] usage: /plan <prompt>")
            return
        run_agent_card(state, Job(job.open_id, arg, mode="plan"))
        return
    if cmd == "/ask":
        if not arg:
            lark_send_text(job.open_id, "[bridge] usage: /ask <prompt>")
            return
        run_agent_card(state, Job(job.open_id, arg, mode="ask"))
        return
    if cmd.startswith("/"):
        lark_send_text(job.open_id, f"[bridge] unknown command: {cmd}. /help for list.")
        return

    run_agent_card(state, Job(job.open_id, arg, mode=job.mode))


def worker(state: dict, q: "queue.Queue[Job]") -> None:
    while True:
        job = q.get()
        try:
            handle(state, job, q)
        except Exception as exc:
            log(f"worker error: {exc}")
            lark_send_text(job.open_id, f"[bridge error] {exc}")
        finally:
            q.task_done()


# ----------------------------------------------------------------------------
# Event subscription loop
# ----------------------------------------------------------------------------

def extract_text(event: dict) -> str:
    msg = (event.get("event") or {}).get("message") or {}
    raw = msg.get("content") or "{}"
    try:
        return (json.loads(raw).get("text") or "").strip()
    except Exception:
        return ""


def extract_open_id(event: dict) -> Optional[str]:
    sender = (event.get("event") or {}).get("sender") or {}
    sid = sender.get("sender_id") or {}
    return sid.get("open_id") or sender.get("open_id")


def handle_card_action(evt: dict, state: dict, q: "queue.Queue[Job]") -> None:
    """Handle a card.action.trigger event from a button click."""
    inner = (evt.get("event") or {})
    action = (inner.get("action") or {})
    value = action.get("value") or {}
    operator = (inner.get("operator") or {})
    open_id = operator.get("open_id") or operator.get("union_id")
    a = value.get("action") if isinstance(value, dict) else None
    if not open_id:
        log(f"card action: missing operator open_id: {inner}")
        return
    log(f"card action: {a} from {open_id[:14]}...")
    if a == "retry":
        text = state.get("last_prompt") or "(no last prompt)"
        q.put(Job(open_id=open_id, text=text))
    elif a == "new":
        state.pop("chat_id", None)
        save_state(state)
        lark_send_text(open_id, "[bridge] cleared chat. next message starts a new conversation.")
    elif a == "pin":
        lark_send_text(open_id, "[bridge] pin not implemented yet (would call /im/v1/chats/pin)")
    elif a == "cancel":
        with proc_lock:
            if current_proc is not None:
                try:
                    current_proc.send_signal(signal.SIGINT)
                    lark_send_text(open_id, "[bridge] sent SIGINT")
                except Exception as exc:
                    lark_send_text(open_id, f"[bridge] cancel error: {exc}")
            else:
                lark_send_text(open_id, "[bridge] nothing running")
    else:
        log(f"card action: unknown action {a}")


SECRETS_FILE = ROOT / "secrets.env"


def load_secrets() -> dict:
    """Read LARK_APP_ID/LARK_APP_SECRET. Precedence: env vars > secrets.env."""
    creds: dict = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("LARK_APP_ID", "LARK_APP_SECRET"):
        v = os.environ.get(k)
        if v:
            creds[k] = v
    if not creds.get("LARK_APP_ID"):
        try:
            cfg = json.loads(Path.home().joinpath(".lark-cli/config.json").read_text())
            apps = cfg.get("apps") or []
            if apps and apps[0].get("appId"):
                creds["LARK_APP_ID"] = apps[0]["appId"]
        except Exception:
            pass
    return creds


def start_sdk_event_loop(state: dict, q: "queue.Queue[Job]",
                         app_id: str, app_secret: str) -> None:
    """Subscribe to events via lark-oapi WebSocket SDK (no subprocess).

    Compared to the lark-cli subprocess loop this:
      - handles card.action.trigger and returns a proper toast within 3s
      - removes per-event node cold-start cost (events come straight from WS)
      - reuses the same Job queue + handle() so worker semantics are unchanged
    """
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger, P2CardActionTriggerResponse,
    )

    def on_message(data: "P2ImMessageReceiveV1") -> None:
        try:
            event = data.event
            msg = getattr(event, "message", None)
            msg_type = getattr(msg, "message_type", None) if msg else None
            content_raw = getattr(msg, "content", None) if msg else None

            text = ""
            try:
                content = json.loads(content_raw or "{}")
                if msg_type == "text":
                    text = (content.get("text") or "").strip()
                elif msg_type == "post":
                    # Extract all text spans from rich-text post format
                    parts: list[str] = []
                    for line in content.get("content") or []:
                        for span in line:
                            if isinstance(span, dict) and span.get("tag") == "text":
                                parts.append(span.get("text") or "")
                    text = "\n".join(parts).strip()
            except Exception:
                text = ""

            sender = getattr(event, "sender", None)
            open_id = None
            if sender is not None:
                sid = getattr(sender, "sender_id", None)
                if sid is not None:
                    open_id = getattr(sid, "open_id", None)
            if open_id:
                state["last_open_id"] = open_id

            # Image messages: download to inbox/, queue as a pending include,
            # tell the user we're ready for a follow-up text prompt.
            if open_id and msg_type == "image":
                try:
                    img_content = json.loads(content_raw or "{}")
                except Exception:
                    img_content = {}
                image_key = img_content.get("image_key")
                message_id = getattr(msg, "message_id", None) or \
                             getattr(msg, "messageId", None)
                if not image_key or not message_id:
                    lark_send_text(open_id,
                                   "[bridge] 收到 image 但缺少 image_key / message_id, 没法下载")
                    return
                p = download_image(message_id, image_key)
                if not p:
                    lark_send_text(open_id,
                                   "[bridge] 图片下载失败，请稍后再试或换文字描述")
                    return
                entry = {"path": str(p), "content": "",
                         "size": p.stat().st_size,
                         "truncated": False, "kind": "image"}
                state.setdefault("pending_includes", []).append(entry)
                save_state(state)
                n = len(state["pending_includes"])
                lark_send_text(
                    open_id,
                    f"📷 收到截图，已存到 `{p.name}` ({p.stat().st_size} bytes)。\n"
                    f"已排队 {n} 个附件，下一条文字 prompt 会一起带上。\n"
                    f"比如发：`这张图里的报错是什么意思？` 或 `根据截图改一下我现在的代码`")
                return

            # Non-image, non-text/post messages: respond with a friendly hint
            # instead of silently dropping (was a real source of "为什么没反应").
            if open_id and msg_type and msg_type not in ("text", "post"):
                lark_send_text(
                    open_id,
                    f"[bridge] 暂不支持 `{msg_type}` 类型消息。\n"
                    f"  · 文字 / 富文本 / 图片 已支持\n"
                    f"  · 其他文件：用 `/include <path>` 引用 Mac 上文件\n"
                    f"  · 命令清单：`/help`")
                return

            if not text or not open_id:
                return
            state["last_prompt"] = text
            save_state(state)
            log(f"<- {open_id[:14]}...({msg_type}): {text[:80]}")
            q.put(Job(open_id=open_id, text=text))
        except Exception as exc:
            log(f"on_message error: {exc}")

    def on_card_action(data: "P2CardActionTrigger") -> "P2CardActionTriggerResponse":
        """Card button click handler. Must return a toast within 3 seconds."""
        try:
            payload = json.loads(lark.JSON.marshal(data))
            evt = payload.get("event") or {}
            action = evt.get("action") or {}
            value = action.get("value") or {}
            # Feishu sometimes serializes value as JSON-string; normalize to dict
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    value = {"raw": value}
            operator = evt.get("operator") or {}
            open_id = operator.get("open_id") or operator.get("union_id")
            a = value.get("a") if isinstance(value, dict) else None
            log(f"card action: {a} from {(open_id or '?')[:14]}...  raw={value}")

            toast_type = "info"
            toast_text = f"已收到: {a or 'unknown'}"
            if a == "retry":
                text = state.get("last_prompt") or "(no last prompt)"
                q.put(Job(open_id=open_id, text=text))
                toast_text = "🔁 已加入队列：重试上一条 prompt"
                toast_type = "success"
            elif a == "new":
                state.pop("chat_id", None)
                save_state(state)
                toast_text = "🆕 chat 已清空，下一条会开新会话"
                toast_type = "success"
            elif a == "cancel":
                with proc_lock:
                    if current_proc is not None:
                        try:
                            current_proc.send_signal(signal.SIGINT)
                            toast_text = "⏹ 已发送 SIGINT"
                            toast_type = "success"
                        except Exception as exc:
                            toast_text = f"取消失败: {exc}"
                            toast_type = "error"
                    else:
                        toast_text = "当前没有正在跑的 agent"
                        toast_type = "warning"
            elif a == "pin":
                toast_text = "📌 置顶功能尚未实现，留个名先"
                toast_type = "warning"
            else:
                toast_text = f"未知动作: {a}"
                toast_type = "warning"
            return P2CardActionTriggerResponse({
                "toast": {"type": toast_type, "content": toast_text},
            })
        except Exception as exc:
            log(f"card action error: {exc}")
            return P2CardActionTriggerResponse({
                "toast": {"type": "error", "content": f"bridge error: {exc}"},
            })

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card_action)
               .build())

    log(f"SDK ws client starting (app_id={app_id})")
    cli = lark.ws.Client(app_id, app_secret,
                         event_handler=handler,
                         log_level=lark.LogLevel.INFO)
    cli.start()  # blocks


def start_subprocess_event_loop(state: dict, q: "queue.Queue[Job]") -> None:
    """Legacy fallback: subscribe events via `lark-cli event +subscribe`.

    Used only when LARK_APP_SECRET is not provided. Card buttons WILL show
    a red 200340 toast in this mode because lark-cli cannot return a toast.
    """
    log("(fallback) subscribing via lark-cli subprocess; buttons will show 200340")
    proc = subprocess.Popen(
        ["lark-cli", "event", "+subscribe", "--as", "bot",
         "--event-types", "im.message.receive_v1,card.action.trigger",
         "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        event_type = ((evt.get("header") or {}).get("event_type") or
                      evt.get("event_type") or "")
        if event_type == "card.action.trigger":
            handle_card_action(evt, state, q)
            continue
        text = extract_text(evt)
        open_id = extract_open_id(evt)
        if open_id:
            state["last_open_id"] = open_id
        if not text or not open_id:
            continue
        state["last_prompt"] = text
        save_state(state)
        log(f"<- {open_id[:14]}...: {text[:80]}")
        q.put(Job(open_id=open_id, text=text))


def main() -> None:
    state = load_state()
    state.setdefault("model", DEFAULT_MODEL)
    save_state(state)
    log(f"db: {_db.DB_PATH}")
    log(f"chat_id={state.get('chat_id')} model={state.get('model')} workspace={WORKSPACE}")

    q: "queue.Queue[Job]" = queue.Queue()
    threading.Thread(target=worker, args=(state, q), daemon=True).start()

    creds = load_secrets()
    if creds.get("LARK_APP_ID") and creds.get("LARK_APP_SECRET"):
        try:
            init_lark_client(creds["LARK_APP_ID"], creds["LARK_APP_SECRET"])
        except Exception as exc:
            log(f"SDK client init failed ({exc}); will use lark-cli api subprocess for cards/messages")
        try:
            start_sdk_event_loop(state, q,
                                 creds["LARK_APP_ID"], creds["LARK_APP_SECRET"])
            return
        except Exception as exc:
            log(f"SDK event loop failed ({exc}); falling back to lark-cli subprocess")
    start_subprocess_event_loop(state, q)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
