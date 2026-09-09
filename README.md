# 听生财（ting-shengcai）

把[生财有术](https://scys.com)长帖变成**可连播听书页**：搜作者 / 热门榜 / 精华帖 → 点开拉正文 → TTS → 待听连播。

> **非生财官方产品。** 自托管小工具，方便边听边刷长文。

## 用 AI 一键装（推荐给 Codex）

大多数人不必手敲命令：把仓库丢给 **Codex / Cursor / Claude Code**，让 AI 读 `AGENTS.md` 自动装环境。

1. 打开 [PROMPT_FOR_CODEX.md](./PROMPT_FOR_CODEX.md)，整段复制发给你的 AI  
2. AI 装依赖并运行 `mcp_login.py`  
3. **你只需扫码/登录授权生财账号**  
4. AI 启动后打开 http://127.0.0.1:8766/

详细机器步骤见 [AGENTS.md](./AGENTS.md)。

## 功能

- 搜作者、搜标题（MCP `userSearch` / `searchTopic`）
- 热门榜、精华帖浏览 + 翻页
- 按需拉正文 + edge-tts 语音缓存
- 待听列表（**存在你自己的浏览器 localStorage**，不跨用户共享）
- 底栏播放器：进度条、上一首 / 下一首、倍速

## 快速开始

```bash
cd ondemand
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install edge-tts

# 生财 MCP 登录（浏览器 OAuth，token 只存在本机 secrets/）
python mcp_login.py

# 启动
bash start.sh
# 打开 http://127.0.0.1:8766/
```

默认 `server.py` 会找 `/workspace/.venv-tts/bin/edge-tts`。本机请改成你的 `edge-tts` 路径，例如：

```python
EDGE_TTS = Path(shutil.which("edge-tts") or ".venv/bin/edge-tts")
```

## 重要说明

1. **不要把 MCP token / 会员全文 / mp3 推进公开仓库。** `secrets/`、`cache/audio/`、`data/posts/` 已在 `.gitignore`。
2. 每人自建一份服务 + **各自**完成生财 MCP 登录；不要把挂着自己账号的临时公网隧道当「大家共用官网」。
3. 「待听」是浏览器本地数据；服务器只会缓存拉过的正文/音频，磁盘需自己清理。
4. 合规：遵守生财与平台规范；本仓库默认只交代码。

## 目录

- `ondemand/` — 按需版（推荐）
- `offline-player/` — 早期本地合集播放器骨架（需自备 mp3）

## License

MIT
