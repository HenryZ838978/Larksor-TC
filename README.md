<p align="center">
  <img src="assets/logo.png" alt="Larksor-TC" width="220">
</p>

<h1 align="center">Larksor-TC</h1>

<p align="center">
  <b>L</b>ark&nbsp;·&nbsp;<b>C</b>ursor&nbsp;·&nbsp;<b>T</b>erminal&nbsp;·&nbsp;<b>C</b>onnect
</p>

<p align="center">
  Drive your Mac's <a href="https://cursor.com">Cursor</a> + <b>Opus 4.7 Thinking High</b> from <a href="https://www.larksuite.com">Feishu / Lark</a> chat.<br>
  Anywhere with a phone. Same seat, same bill, same model.
</p>

<p align="center">
  <img alt="status"   src="https://img.shields.io/badge/status-alpha-orange?style=flat-square">
  <img alt="python"   src="https://img.shields.io/badge/python-3.9%2B-blue?style=flat-square">
  <img alt="platform" src="https://img.shields.io/badge/platform-macOS-lightgrey?style=flat-square">
  <img alt="runtime"  src="https://img.shields.io/badge/launchd-autostart-green?style=flat-square">
  <img alt="stars"    src="https://img.shields.io/github/stars/HenryZ838978/Larksor-TC?style=social">
</p>

---

## ⚡ 30-second pitch

<table align="center">
<tr>
<td align="center" width="33%">
  <h3>📱</h3>
  <b>你</b><br>
  <sub>手机 / iPad / 客户现场<br>外网</sub>
</td>
<td align="center" width="34%">
  <h3>↔️</h3>
  <b>飞书 DM</b><br>
  <sub>合规通道<br>已被公司放行</sub>
</td>
<td align="center" width="33%">
  <h3>🖥️</h3>
  <b>你的工位 Mac</b><br>
  <sub>Cursor 团队配额<br>Opus 4.7 Thinking High</sub>
</td>
</tr>
</table>

<p align="center">
  <i>从你已经付费的 Cursor seat，把 Opus 延伸到任何你能开飞书的设备。</i>
</p>

---

## 👀 What it looks like

<p align="center">
  <img src="assets/feishu-card.svg" alt="Feishu card preview" width="640">
</p>

<sub align="center">

▾ 流式打字机回答 · ▾ thinking 折叠面板 · ▾ tool calls 折叠面板 · ▾ token + 耗时元数据  
(SVG mock; 真截图欢迎 PR)

</sub>

---

## 🏗 Architecture

<p align="center">
  <img src="assets/architecture.svg" alt="Architecture" width="820">
</p>

```
[ phone/ipad ]  ──DM──►  [ Feishu WSS ]  ──events──►  [ your Mac ]
                                                          │
   ◄──── streaming CardKit replies ◄── lark-oapi SDK ◄── bridge.py
                                                          │
                                                          └─► cursor-cli
                                                              (Opus 4.7)
```

Mac 只发起 **outbound** 连接 — 不需要公网 IP、不需要 VPN、不需要反向代理。

---

## 🚀 Quick start

<p align="center">
  <img src="assets/install-flow.svg" alt="Install flow" width="820">
</p>

```bash
# 1. clone
git clone https://github.com/HenryZ838978/Larksor-TC.git ~/larksor-tc

# 2. 去 https://open.feishu.cn/app 创建一个 "<你的名字>的飞书CLI" bot，
#    勾 im:message + im:message:send_as_bot + cardkit:card:write，
#    订阅 im.message.receive_v1 + card.action.trigger，记下 App ID + Secret

# 3. install
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx \
LARK_APP_SECRET=xxxxxxxxxxxxxxxx \
bash ~/larksor-tc/install.sh

# 4. 在飞书 DM 你刚建的那个 bot 发 "hi"，应当收到流式卡片
```

懒得自己跑命令？把 README 里 "Step 2" 那段贴给你 Cursor IDE 的对话框，它会自动跑完。

---

## 🎯 Why this exists

|                                          | Without Larksor-TC          | With Larksor-TC              |
| ---------------------------------------- | --------------------------- | ---------------------------- |
| 用 Opus 4.7 Thinking High（不在工位 Mac 上时） | $$$ Anthropic 直连 / 限速 third-party | 已付费的 Cursor seat，0 元增量 |
| 内网代码 + 私有数据                          | VPN + 远程桌面 + 跨网代理      | 飞书 DM，公司本来就放行           |
| 离开工位时 agent 跑到一半中断                | 等下班                       | 手机点重试 / 切模型 / 换 chat   |
| 给桌面同事 demo                            | 凑过来看屏幕                  | 群里贴飞书消息                 |

---

## 🧰 What's inside

- 🎴 **CardKit v2 流式卡片**：打字机式 token 输出，markdown 代码高亮，10/s update QPS  
- 💭 **Thinking 折叠面板**：opus / sonnet thinking 模型的 reasoning 默认收起，点开看  
- 🔧 **Tool calls 折叠面板**：实时显示 `shell` / `read` / `edit` / `grep` / `glob` / `mcp` 等工具调用，运行中展开，结束自动折叠  
- 📷 **图片消息**：手机截图 DM 给 bot → 自动落 `~/larksor-tc/inbox/` → 下一条 prompt 自动带上，opus 多模态读图  
- 📁 **Per-chat workspace**：`/cd ~/proj/foo` 或自然语言 "切到 ~/proj/foo"，每个 chat 独立工作目录  
- 🪙 **Token + cost 追踪**：SQLite 持久化每个 turn 的 tokens + 耗时；`/cost today` / `/history N`  
- 💬 **Chat 管理**：`/ls` / `/resume <N>` / `/new`，标题自动用首条 prompt 前 40 字  
- ⚙️ **launchd 自启 + caffeinate**：开机自动起，跑期间防 idle-sleep  
- 🚦 **熔断**：15 分钟硬超时，`resource_exhausted` 友好提示，agent 崩了卡片有报错不空挂  
- 🌐 **lark-oapi SDK 直连**：避免 `lark-cli` subprocess 冷启动延迟，按钮 callback 能正确返回 toast  

---

## 📜 Commands cheat sheet

```text
# 切换
/model opus              换模型；别名 opus | sonnet | gpt5 | codex | auto
/cd ~/proj/mtk-infra     切当前 chat 的 workspace
/new                     新 chat
/resume 3                切到 /ls 列表第 3 个 chat

# 信息
/help     /status     /history 5     /cost today     /ls

# 操作
/include path/to/file    把 Mac 文件随下一条 prompt 一起送
/retry                   重跑上一条
/cancel                  停掉当前正在跑的 agent
/plan <prompt>           plan 模式（只读 + 计划）
/ask  <prompt>           ask 模式（Q&A 只读）

# 中文自然语言（同样生效）
换模型 opus      切到 ~/proj/foo      用 sonnet 模型
```

---

## 🔧 Maintenance

```bash
tail -f ~/larksor-tc/bridge.log                           # 实时日志
launchctl kickstart -k gui/$UID/cn.modelbest.larksor-tc   # 重启
launchctl bootout    gui/$UID/cn.modelbest.larksor-tc     # 临时停
bash ~/larksor-tc/uninstall.sh                            # 卸载
bash ~/larksor-tc/uninstall.sh --purge                    # 卸载 + 清历史
```

要用 **Opus 4.7** 全家，`~/.cursor/cli-config.json` 必须开 Max Mode：

```json
{ "maxMode": true, "model": { "maxMode": true } }
```

---

## 🗺 Roadmap

| Phase | Status      | Highlights                                                                 |
| ----- | ----------- | -------------------------------------------------------------------------- |
| 1     | ✅ alpha     | cursor-cli backend, 单用户自部署, mdou skill 内部分发                        |
| 2     | 🚧 planned  | 接 Claude Code / Codex / DeepSeek-harness 作为可替换 backend；群聊 @ 模式 |
| 3     | 🌱 maybe    | 公司级 cost dashboard, 多人协作 chat, Cloud Agent handoff                 |

---

## 🙏 Credits & Inspiration

- [Cursor](https://cursor.com) — the IDE this thing borrows compute from  
- [Lark Open Platform](https://open.feishu.cn) — the rails this runs on  
- [`@HenryZ838978/deepseek-harness`](https://github.com/HenryZ838978/deepseek-harness) — sibling project; same dark humor, different beast  
- [ModelBest](https://modelbest.cn) — for letting me dogfood this internally  

<p align="center">
  <sub>
    ⭐ <b>If this saved you a single train ride back to the office, star the repo.</b> That's all I'm asking.<br>
    PRs / issues welcome. License: internal alpha, do not commit your <code>secrets.env</code>.
  </sub>
</p>
