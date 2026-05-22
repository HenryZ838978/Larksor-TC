<p align="center">
  <img src="assets/logo.png" alt="Larksor-TC" width="180">
</p>

<h1 align="center">Larksor-TC</h1>

<p align="center">
  <strong>L</strong>ark · <strong>C</strong>ursor · <strong>T</strong>erminal · <strong>C</strong>onnect
</p>

<p align="center">
  内网 Mac 上的 Cursor + Opus 4.7，<br>
  通过飞书在任何地方驱动它
</p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-internal_alpha-orange">
  <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-blue">
  <img alt="platform" src="https://img.shields.io/badge/platform-macOS-lightgrey">
  <img alt="runtime" src="https://img.shields.io/badge/runtime-launchd%20%2B%20caffeinate-green">
  <img alt="license" src="https://img.shields.io/badge/license-internal_use-red">
</p>

---

## 是什么

<p align="center">
  <img src="assets/architecture.svg" alt="Architecture" width="900">
</p>

跑在你 Mac 上的桥接进程，通过 **lark-oapi 长连接** 接飞书、通过 **cursor-cli** 调你已付费的 Cursor 配额。

- Mac 只发起 outbound 连接 → **不需要公网 IP / VPN / 反向代理**
- 飞书是公司放行的合规通道 → **内外网都通**
- 用的是你 Cursor 团队订阅的 Opus 4.7 配额 → **零额外采购**

---

## 长什么样

<details open>
<summary>飞书里实际的对话卡片（点击折叠）</summary>

<p align="center">
  <img src="assets/feishu-card.svg" alt="Feishu Card Preview" width="720">
</p>

> 上面是 SVG mockup，可以用真截图替换 `assets/feishu-card.svg`

</details>

---

## 装机

<p align="center">
  <img src="assets/install-flow.svg" alt="Install Flow" width="880">
</p>

### Step 1 · 你做（5 分钟，浏览器里）

打开 [飞书开发者后台](https://open.feishu.cn/app) → 创建企业自建应用 → 命名 `<你的名字>的飞书CLI` →
启用机器人 → 勾以下权限并提交审批：

| 权限                       | 用途                           |
| -------------------------- | ------------------------------ |
| `im:message`               | 收发消息                       |
| `im:message:send_as_bot`   | 以机器人身份发消息             |
| `cardkit:card:write`       | 写流式卡片                     |

订阅事件：`im.message.receive_v1`、`card.action.trigger`。

审批通过后，从"凭证与基础信息"复制 **App ID** + **App Secret**。

### Step 2 · Cursor 做（1 分钟，全自动）

把下面这段贴到你 **Cursor IDE 的对话框**，回车：

````text
我要装 Larksor-TC。请按以下步骤帮我做完：

1. 确认 ~/larksor-tc/ 已存在（应有 bridge.py、install.sh、README.md）。
   不存在的话告诉我去哪 clone。

2. 跑 `git config user.name || whoami | head -c 20` 拿我的名字，记为 $MY_NAME。

3. 用 `open https://open.feishu.cn/app` 在浏览器打开飞书后台，
   把 README.md 里 "Step 1" 的步骤完整复述给我让我照做（边做边告诉你进度）。
   应用名建议 "${MY_NAME}的飞书CLI"。

4. 我把 App ID + App Secret 贴给你后，请用环境变量传给 installer：
   `LARK_APP_ID=<我贴的ID> LARK_APP_SECRET=<我贴的Secret> bash ~/larksor-tc/install.sh`

5. 把 install.sh 输出最后 20 行给我看。

6. 让我去飞书 DM 我刚创建的 bot 发 "hi"，30 秒后 `tail -n 20 ~/larksor-tc/bridge.log`，
   确认看到 "<- ou_..." 和 "SDK ws client starting"。如果没看到，帮我看哪一步漏了。

注意：
- App Secret 只走环境变量 + 0600 权限的 ~/larksor-tc/secrets.env
- install.sh 幂等，重复跑没事
````

完事。`launchd` 接管，**关机重启 / 合页过夜都不用管**。

---

## 命令速查

```text
# 切换
/model opus              换模型；别名 opus | sonnet | gpt5 | codex | auto
/cd ~/proj/mtk-infra     切当前 chat 的 workspace
/new                     新 chat
/resume 3                切到 /ls 列表第 3 个 chat

# 信息
/help                    全部命令
/status                  当前 chat / model / workspace
/history 5               最近 5 个 turn（含 token + 耗时）
/cost today              今日 token 用量
/ls                      最近 10 个 chat

# 操作
/include path/to/file    把 Mac 文件随下一条 prompt 一起送
/retry                   重跑上一条
/cancel                  停掉当前正在跑的 agent
/plan <prompt>           plan 模式（只读 + 计划）
/ask  <prompt>           ask 模式（Q&A 只读）

# 中文自然语言（同样生效）
换模型 opus
切到 ~/proj/foo
```

---

## 维护

| 操作                    | 命令                                                                      |
| ----------------------- | ------------------------------------------------------------------------- |
| 看实时日志              | `tail -f ~/larksor-tc/bridge.log`                                         |
| 重启                    | `launchctl kickstart -k gui/$UID/cn.modelbest.larksor-tc`                 |
| 临时停（重启 Mac 会拉起）| `launchctl bootout gui/$UID/cn.modelbest.larksor-tc`                      |
| 状态                    | `launchctl print gui/$UID/cn.modelbest.larksor-tc \| grep state`          |
| 升级（git pull 后）      | `launchctl kickstart -k gui/$UID/cn.modelbest.larksor-tc`                 |
| 卸载（保留历史）         | `bash ~/larksor-tc/uninstall.sh`                                          |
| 彻底卸载                | `bash ~/larksor-tc/uninstall.sh --purge`                                  |

---

## 配置

`~/larksor-tc/secrets.env` (0600)：

```bash
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxx
```

可选环境变量（也可写在 launchd plist 的 `EnvironmentVariables`）：

| 变量                       | 默认                          | 作用                                              |
| -------------------------- | ----------------------------- | ------------------------------------------------- |
| `BRIDGE_MODEL`             | 自动                          | 强制默认模型；不设则按 cli-config 的 maxMode 自适应 |
| `BRIDGE_WORKSPACE`         | `$HOME`                       | 全局默认 workspace                                |
| `BRIDGE_TENANT`            | `ModelBest`                   | 卡片标题里的租户名                                |
| `BRIDGE_AGENT_TIMEOUT_S`   | `900`                         | agent 单次硬超时（秒）                            |

要用 Opus 4.7 全家，`~/.cursor/cli-config.json` 必须开 Max Mode：

```json
{ "maxMode": true, "model": { "maxMode": true } }
```

---

## 排错

| 症状                                  | 处理                                                                    |
| ------------------------------------- | ----------------------------------------------------------------------- |
| 卡片 `(agent exited rc=1)` + `resource_exhausted` | `/new` 起新 chat（老 chat 多模型混杂 context 超载）          |
| 卡片 `Max Mode Required`              | 改 cli-config 把两个 `maxMode` 都设 `true`；或 `/model sonnet-thinking` |
| 标题 `thinking…` 不动                 | 换非 thinking 模型；或换 `sonnet-thinking` / `opus-thinking-high`       |
| 合页一段时间后飞书发消息没回           | 系统设置打开 "Prevent automatic sleeping when display is off"           |
| 点按钮弹红 toast 200340               | bridge 已默认走 SDK，应当不会出。出了看 `bridge.log` 找 `SDK ws client` |

---

## 不在 Phase 1 范围

- 多用户共享 bot（每人自部署）
- 接 Claude Code / Codex / DeepSeek-harness 作为 backend
- 群聊 @ 模式
- 公司级 dashboard / cost 聚合

---

## License

仅供 ModelBest 内部使用。**不要把 `secrets.env` 提交到 git**。
