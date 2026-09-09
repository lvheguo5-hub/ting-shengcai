# AGENTS.md — 听生财（给 Codex / Cursor / Claude Code 等 AI 看）

你是在帮用户**本机自托管**安装「听生财」：生财有术长帖 → 听书连播网页（**非官方**）。

## 成功标准（做到这些再停下）

1. `ondemand` 依赖装好（venv + fastapi/uvicorn/httpx + edge-tts）。
2. 用户已完成生财 MCP **扫码/微信授权**（你不能代劳登录；你只负责起 `mcp_login.py` 并把授权链接/窗口交给用户）。
3. `GET http://127.0.0.1:8766/api/health` 返回 `ok: true` 且 `mcp_configured: true`、`edge_tts: true`。
4. 用户能打开 `http://127.0.0.1:8766/` 看到首页（搜作者 / 热门榜 / 精华帖）。
5. **绝不**把 `ondemand/secrets/`、`data/posts/`、`cache/audio/*.mp3` 提交进 git。

## 项目是什么

| 路径 | 作用 |
|------|------|
| `ondemand/` | 推荐：按需拉帖 + TTS + 待听连播（端口 **8766**） |
| `offline-player/` | 早期本地 mp3 合集播放器骨架，可忽略 |

「待听」存在**用户浏览器** `localStorage`，不是全站共享。

后端通过生财 MCP（`mcp.scys.com`）只读拉帖；OAuth token 只存在本机 `ondemand/secrets/mcp_tokens.json`。

## 你要自动做的步骤

在仓库根目录执行（macOS / Linux）：

```bash
cd ondemand
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt edge-tts
```

确认 `edge-tts` 在 PATH（或设置环境变量）：

```bash
export EDGE_TTS_BIN="$(command -v edge-tts)"
```

### 唯一需要用户动手的一步：MCP 授权

```bash
cd ondemand
.venv/bin/python mcp_login.py
```

- 脚本会打开（或打印）授权 URL，并在 `http://127.0.0.1:8767/callback` 等回调。
- **停下来明确告诉用户**：请用浏览器完成生财/微信扫码登录；完成后终端应显示登录成功。
- 不要替用户输入密码、不要截取/打印 access_token / refresh_token。
- 若端口 8767 被占用，先帮用户释放或改文档说明冲突。

授权成功后再启动：

```bash
cd ondemand
bash start.sh
```

另开终端探测：

```bash
curl -sS http://127.0.0.1:8766/api/health
```

期望字段：`mcp_configured: true`，`edge_tts: true`。

把 `http://127.0.0.1:8766/` 发给用户即可听。

## 约束（必须遵守）

- 只使用**只读** MCP 能力路径（本应用已封装）；不要调用投锚/点赞/收藏等写入接口。
- 不要把临时 Cloudflare 隧道或你的 MCP 账号当成「给别人共用的官网」。
- 不要提交 secrets；检查 `.gitignore` 已覆盖 `ondemand/secrets/*`。
- Windows：用 PowerShell 等价命令建 venv；回调仍是本机 `127.0.0.1`。

## 用户可能说的话 → 你的动作

- 「帮我装听生财 / 跑起来」→ 按上面步骤做，停在扫码处等人。
- 「授权好了」→ 启动 `start.sh`，验 health，给本地 URL。
- 「热门/精华/搜人」→ 已内置，无需再开发即可用；坏了再查 `server.py` / `mcp_client.py`。

## 粘贴给人类用户的一句话

> 装环境和启动交给 AI；你只需要在弹出的页面里**扫码授权生财账号**，然后打开 http://127.0.0.1:8766/
