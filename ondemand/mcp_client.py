#!/usr/bin/env python3
"""Mechanical Shengcai MCP client (OAuth Bearer + streamable HTTP tools/call).

No LLM. Read-only tools: topicDetail / searchTopic / userSearch.
Tokens: secrets/mcp_tokens.json (never logged).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("mcp_client")

BASE = Path(__file__).resolve().parent
SECRETS_DIR = BASE / "secrets"
TOKENS_PATH = SECRETS_DIR / "mcp_tokens.json"
POSTS = BASE / "data" / "posts"
INDEX = BASE / "data" / "topics_index.json"
AUTHORS = BASE / "data" / "authors.json"

MCP_URL = "https://mcp.scys.com/shengcai-web/mcp"
AS_ISSUER = "https://mcp.scys.com/mcp-oauth"
AUTHORIZE_URL = f"{AS_ISSUER}/authorize"
TOKEN_URL = f"{AS_ISSUER}/token"
REGISTER_URL = f"{AS_ISSUER}/register"
RESOURCE = MCP_URL
SCOPE = "mcp"
DEFAULT_REDIRECT = "http://127.0.0.1:8767/callback"
DEFAULT_TIMEOUT = 45.0

_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# HTML helpers (same idea as worker/process_job.py)
# ---------------------------------------------------------------------------


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = html.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def strip_html(s: str) -> str:
    if not s:
        return ""
    p = _HTMLStripper()
    try:
        p.feed(s)
        return p.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", "", s)


# ---------------------------------------------------------------------------
# Token storage (never print/log secrets)
# ---------------------------------------------------------------------------


def tokens_path() -> Path:
    return TOKENS_PATH


def is_configured() -> bool:
    """True if an access_token (or refresh_token) is present."""
    data = load_tokens()
    if not data:
        return False
    return bool(data.get("access_token") or data.get("refresh_token"))


def load_tokens() -> dict[str, Any]:
    with _token_lock:
        if not TOKENS_PATH.exists():
            return {}
        try:
            data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


def save_tokens(data: dict[str, Any]) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    # ensure gitignore exists
    gi = SECRETS_DIR / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n!.gitignore\n", encoding="utf-8")
    with _token_lock:
        tmp = TOKENS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(TOKENS_PATH)
        try:
            TOKENS_PATH.chmod(0o600)
        except OSError:
            pass


def _token_expired(data: dict[str, Any], skew: float = 60.0) -> bool:
    exp = data.get("expires_at")
    if exp is None:
        # no expiry recorded — treat as possibly valid
        return False
    try:
        return time.time() >= float(exp) - skew
    except (TypeError, ValueError):
        return False


def refresh_access_token(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Refresh access_token using refresh_token. Updates store. Raises on failure."""
    data = dict(data or load_tokens())
    refresh = data.get("refresh_token")
    if not refresh:
        raise RuntimeError("no refresh_token; run: python mcp_login.py")
    client_id = data.get("client_id")
    form: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": str(refresh),
        "resource": RESOURCE,
    }
    if client_id:
        form["client_id"] = str(client_id)
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            TOKEN_URL,
            data=form,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"token refresh failed (HTTP {r.status_code})")
    body = r.json()
    access = body.get("access_token")
    if not access:
        raise RuntimeError("token refresh: no access_token in response")
    data["access_token"] = access
    if body.get("refresh_token"):
        data["refresh_token"] = body["refresh_token"]
    if body.get("token_type"):
        data["token_type"] = body["token_type"]
    if body.get("scope"):
        data["scope"] = body["scope"]
    expires_in = body.get("expires_in")
    if expires_in is not None:
        try:
            data["expires_at"] = time.time() + float(expires_in)
        except (TypeError, ValueError):
            pass
    save_tokens(data)
    return data


def get_valid_access_token() -> str:
    data = load_tokens()
    if not data.get("access_token") and not data.get("refresh_token"):
        raise RuntimeError("MCP not configured; run: python mcp_login.py")
    if data.get("access_token") and not _token_expired(data):
        return str(data["access_token"])
    if data.get("refresh_token"):
        data = refresh_access_token(data)
        return str(data["access_token"])
    raise RuntimeError("access_token expired and no refresh_token; run: python mcp_login.py")


# ---------------------------------------------------------------------------
# JSON-RPC streamable HTTP (minimal path)
# ---------------------------------------------------------------------------


def _parse_rpc_http_body(resp: httpx.Response) -> dict[str, Any]:
    ctype = (resp.headers.get("content-type") or "").lower()
    text = resp.text or ""
    if "text/event-stream" in ctype or text.lstrip().startswith("event:") or "data:" in text[:80]:
        # Collect last JSON-RPC message from SSE
        last: dict[str, Any] | None = None
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                last = obj
        if last is None:
            raise RuntimeError("empty SSE response from MCP")
        return last
    try:
        obj = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from MCP: {e}") from e
    if not isinstance(obj, dict):
        raise RuntimeError("unexpected MCP JSON shape")
    return obj


def _call_tool_httpx(name: str, arguments: dict[str, Any], access_token: str, timeout: float) -> Any:
    """Initialize session + tools/call via streamable HTTP JSON-RPC."""
    headers_base = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        init_id = 1
        init_body = {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "someone-listening-ondemand", "version": "0.2.0"},
            },
        }
        r = client.post(MCP_URL, headers=headers_base, json=init_body)
        if r.status_code == 401:
            raise PermissionError("MCP unauthorized")
        if r.status_code >= 400:
            raise RuntimeError(f"MCP initialize HTTP {r.status_code}")
        init_msg = _parse_rpc_http_body(r)
        if init_msg.get("error"):
            raise RuntimeError(f"MCP initialize error: {init_msg['error']}")
        session_id = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        headers = dict(headers_base)
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # notifications/initialized (no id)
        client.post(
            MCP_URL,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        call_id = 2
        call_body = {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        r2 = client.post(MCP_URL, headers=headers, json=call_body)
        if r2.status_code == 401:
            raise PermissionError("MCP unauthorized")
        if r2.status_code >= 400:
            raise RuntimeError(f"MCP tools/call HTTP {r2.status_code}")
        msg = _parse_rpc_http_body(r2)
        if msg.get("error"):
            err = msg["error"]
            raise RuntimeError(f"MCP tools/call error: {err}")
        result = msg.get("result")
        return _unwrap_tool_result(result)


async def _call_tool_sdk(name: str, arguments: dict[str, Any], access_token: str, timeout: float) -> Any:
    """Prefer MCP Python SDK Client + streamable_http with Bearer."""
    import httpx2
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {access_token}"}
    # httpx2 timeouts: connect/read/write/pool
    timeout_cfg = httpx2.Timeout(timeout, connect=min(15.0, timeout))
    async with httpx2.AsyncClient(headers=headers, timeout=timeout_cfg) as http:
        transport = streamable_http_client(MCP_URL, http_client=http)
        async with Client(transport, read_timeout_seconds=timeout) as client:
            result = await client.call_tool(name, arguments or {})
            if getattr(result, "is_error", False):
                raise RuntimeError(f"tool {name} returned isError")
            return _unwrap_sdk_result(result)


def _unwrap_sdk_result(result: Any) -> Any:
    # Prefer structured_content when present
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None) or []
    return _unwrap_content_list(content)


def _unwrap_tool_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, dict):
        if "structuredContent" in result and result["structuredContent"] is not None:
            return result["structuredContent"]
        if "content" in result:
            return _unwrap_content_list(result.get("content") or [])
        return result
    return result


def _unwrap_content_list(content: list[Any]) -> Any:
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and "text" in block:
                texts.append(str(block["text"]))
            continue
        # pydantic TextContent
        t = getattr(block, "text", None)
        typ = getattr(block, "type", None)
        if typ == "text" and t is not None:
            texts.append(str(t))
    if not texts:
        return content
    joined = "\n".join(texts).strip()
    # Try parse JSON payload (common for Shengcai tools)
    if joined.startswith("{") or joined.startswith("["):
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            pass
    return joined


def call_tool(name: str, arguments: dict[str, Any] | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Call an MCP tool; refresh token once on 401. Sync API."""
    arguments = arguments or {}
    token = get_valid_access_token()

    def _once(access: str) -> Any:
        # Prefer SDK; fall back to raw httpx JSON-RPC
        try:
            return asyncio.run(_call_tool_sdk(name, arguments, access, timeout))
        except RuntimeError as e:
            # nested event loop (e.g. already in async) — use httpx path
            if "asyncio.run()" in str(e) or "running event loop" in str(e).lower():
                return _call_tool_httpx(name, arguments, access, timeout)
            # SDK failed for other reasons — try httpx
            logger.warning("SDK tool call failed (%s); falling back to httpx JSON-RPC", type(e).__name__)
            return _call_tool_httpx(name, arguments, access, timeout)
        except PermissionError:
            raise
        except Exception as e:
            logger.warning("SDK tool call failed (%s); falling back to httpx JSON-RPC", type(e).__name__)
            return _call_tool_httpx(name, arguments, access, timeout)

    try:
        return _once(token)
    except PermissionError:
        data = refresh_access_token()
        return _once(str(data["access_token"]))


# ---------------------------------------------------------------------------
# High-level read helpers
# ---------------------------------------------------------------------------


def topic_detail(entity_type: str, entity_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return call_tool(
        "topicDetail",
        {"entityType": entity_type, "entityId": str(entity_id)},
        timeout=timeout,
    )


def search_topic(
    *,
    keyword: str = "",
    author_id: str = "",
    page: int = 1,
    page_size: int = 10,
    is_hot: bool | None = None,
    is_digested: bool | None = None,
    display_mode: int = 1,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    # Shengcai MCP searchTopic: pageSize max 10; author filter is targetUserId
    # Only pass isHot/isDigested when True — never send false.
    page_size = max(1, min(int(page_size), 10))
    args: dict[str, Any] = {
        "pageIndex": int(page),
        "pageSize": page_size,
        "displayMode": int(display_mode),
    }
    if keyword:
        args["keyword"] = keyword
    if author_id:
        args["targetUserId"] = str(author_id)
    if is_hot is True:
        args["isHot"] = True
    if is_digested is True:
        args["isDigested"] = True
    return call_tool("searchTopic", args, timeout=timeout)


def user_search(
    keyword: str,
    *,
    page: int = 1,
    page_size: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    page_size = max(1, min(int(page_size), 10))
    return call_tool(
        "userSearch",
        {
            "keyword": keyword,
            "pageIndex": int(page),
            "pageSize": page_size,
        },
        timeout=timeout,
    )



def mcp_to_post(entity_type: str, entity_id: str, mcp: Any, *, title: str = "", author_name: str = "") -> dict[str, Any]:
    """Normalize topicDetail payload to posts JSON schema."""
    if not isinstance(mcp, dict):
        raise RuntimeError("topicDetail returned non-object")
    topic = mcp.get("topicDTO") or mcp
    if not isinstance(topic, dict):
        topic = mcp
    user = mcp.get("topicUserDTO") or {}
    if not isinstance(user, dict):
        user = {}
    feishu = topic.get("articleContentContainFeishuDoc")
    if isinstance(feishu, str) and feishu.strip():
        text = feishu.strip()
    else:
        text = strip_html(str(topic.get("articleContent") or ""))
    out_title = topic.get("showTitle") or title or ""
    out_author = user.get("name") or author_name or ""
    author_id = str(user.get("unionUserId") or user.get("userId") or "")
    detail_url = (
        mcp.get("detailUrl")
        or topic.get("detailUrl")
        or f"https://scys.com/articleDetail/{entity_type}/{entity_id}"
    )
    return {
        "entityType": entity_type,
        "entityId": str(entity_id),
        "title": out_title,
        "authorName": out_author,
        "authorId": author_id,
        "text": text,
        "detailUrl": detail_url,
        "gmtCreate": topic.get("gmtCreate"),
    }


def _update_index(post: dict[str, Any]) -> None:
    if INDEX.exists():
        try:
            idx = json.loads(INDEX.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            idx = {"topics": []}
    else:
        idx = {"topics": []}
    topics = idx.get("topics") if isinstance(idx, dict) else None
    if not isinstance(topics, list):
        topics = []
        idx = {"topics": topics}
    et, eid = post["entityType"], str(post["entityId"])
    found = False
    for t in topics:
        if t.get("entityType") == et and str(t.get("entityId")) == eid:
            t["hasBody"] = True
            if post.get("title"):
                t["title"] = post["title"]
            if post.get("authorName"):
                t["authorName"] = post["authorName"]
            if post.get("authorId"):
                t["authorId"] = post["authorId"]
            if post.get("gmtCreate") is not None:
                t["gmtCreate"] = post["gmtCreate"]
            found = True
            break
    if not found:
        entry = {
            "entityType": et,
            "entityId": eid,
            "title": post.get("title") or "",
            "authorName": post.get("authorName") or "",
            "authorId": post.get("authorId") or "",
            "hasBody": True,
            "hasAudio": False,
        }
        if post.get("gmtCreate") is not None:
            entry["gmtCreate"] = post["gmtCreate"]
        topics.append(entry)
    idx["topics"] = topics
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_post(post: dict[str, Any]) -> Path:
    POSTS.mkdir(parents=True, exist_ok=True)
    path = POSTS / f"{post['entityType']}__{post['entityId']}.json"
    # drop internal-only fields from on-disk schema if desired — keep gmtCreate out of file
    disk = {
        "entityType": post["entityType"],
        "entityId": str(post["entityId"]),
        "title": post.get("title") or "",
        "authorName": post.get("authorName") or "",
        "authorId": post.get("authorId") or "",
        "text": post.get("text") or "",
        "detailUrl": post.get("detailUrl") or "",
    }
    path.write_text(json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _update_index(post)
    return path


def post_path(entity_type: str, entity_id: str) -> Path:
    return POSTS / f"{entity_type}__{entity_id}.json"


def fetch_topic(
    entity_type: str,
    entity_id: str,
    *,
    title: str = "",
    author_name: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Sync: topicDetail → save posts JSON → return post dict. Raises on failure."""
    mcp = topic_detail(entity_type, entity_id, timeout=timeout)
    post = mcp_to_post(entity_type, entity_id, mcp, title=title, author_name=author_name)
    if not (post.get("text") or "").strip():
        raise RuntimeError("topicDetail returned empty text")
    save_post(post)
    return post


def _iter_topic_hits(payload: Any) -> list[dict[str, Any]]:
    """Best-effort extract topic list from searchTopic response."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("list", "records", "topics", "data", "items", "result"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            for key2 in ("list", "records", "topics", "items"):
                val2 = val.get(key2)
                if isinstance(val2, list):
                    return [x for x in val2 if isinstance(x, dict)]
    # single topic-like object
    if payload.get("entityId") or payload.get("entity_id") or payload.get("topicDTO"):
        return [payload]
    return []


def _topic_keys(item: dict[str, Any]) -> tuple[str, str, str, str]:
    topic = item.get("topicDTO") if isinstance(item.get("topicDTO"), dict) else item
    user = item.get("topicUserDTO") if isinstance(item.get("topicUserDTO"), dict) else {}
    et = str(topic.get("entityType") or topic.get("entity_type") or item.get("entityType") or "xq_topic")
    eid = str(
        topic.get("entityId")
        or topic.get("entity_id")
        or topic.get("topicId")
        or item.get("entityId")
        or ""
    )
    title = str(topic.get("showTitle") or topic.get("title") or item.get("title") or "")
    author = str(
        user.get("name")
        or item.get("authorName")
        or topic.get("authorName")
        or ""
    )
    return et, eid, title, author





def _iter_user_hits(payload: Any) -> list[dict[str, Any]]:
    """Best-effort extract user list from userSearch response."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("list", "records", "users", "data", "items", "result"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            for key2 in ("list", "records", "users", "items"):
                val2 = val.get(key2)
                if isinstance(val2, list):
                    return [x for x in val2 if isinstance(x, dict)]
    if payload.get("unionUserId") or payload.get("userId") or payload.get("name"):
        return [payload]
    return []


def normalize_user_hit(u: dict[str, Any]) -> dict[str, Any] | None:
    uid = str(u.get("unionUserId") or u.get("userId") or u.get("id") or "")
    if not uid:
        return None
    return {
        "unionUserId": uid,
        "id": uid,
        "name": str(u.get("name") or u.get("nickName") or ""),
        "avatar": str(u.get("avatar") or u.get("avatarUrl") or ""),
        "topicCount": u.get("topicCount") or u.get("count") or 0,
        "digestedTopicCount": u.get("digestedTopicCount") or 0,
        "fansCount": u.get("fansCount") or 0,
        "identityTag": u.get("identityTag") or "",
    }


def normalize_topic_hit(item: dict[str, Any]) -> dict[str, Any] | None:
    et, eid, title, author = _topic_keys(item)
    if not eid:
        return None
    topic = item.get("topicDTO") if isinstance(item.get("topicDTO"), dict) else item
    user = item.get("topicUserDTO") if isinstance(item.get("topicUserDTO"), dict) else {}
    aid = str(user.get("unionUserId") or item.get("authorId") or "")
    aname = author or str(user.get("name") or "")
    gmt = topic.get("gmtCreate") if isinstance(topic, dict) else None
    audio_dir = BASE / "cache" / "audio"
    return {
        "entityType": et,
        "entityId": eid,
        "title": title,
        "authorName": aname,
        "authorId": aid,
        "gmtCreate": gmt,
        "hasBody": post_path(et, eid).exists(),
        "hasAudio": (audio_dir / f"{et}__{eid}.mp3").exists(),
    }


def _payload_total(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return None
    total = raw.get("total") or raw.get("totalCount")
    data = raw.get("data")
    if total is None and isinstance(data, dict):
        total = data.get("total") or data.get("totalCount")
    return total


def _merge_topic_entries(topics: list[dict[str, Any]], entry: dict[str, Any]) -> bool:
    """Merge entry into topics list. Returns True if newly added."""
    et, eid = entry["entityType"], str(entry["entityId"])
    for t in topics:
        if t.get("entityType") == et and str(t.get("entityId")) == eid:
            for k, v in entry.items():
                if k in ("hasBody", "hasAudio"):
                    t[k] = bool(t.get(k)) or bool(v)
                elif v is not None and v != "":
                    t[k] = v
            return False
    topics.append(entry)
    return True


def merge_author_page_into_index(author_id: str, *, page: int = 1, page_size: int = 10, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Fetch one searchTopic page for author; merge titles into index (no body fetch)."""
    author_id = str(author_id)
    page_size = max(1, min(int(page_size), 10))
    raw = search_topic(author_id=author_id, page=page, page_size=page_size, timeout=timeout)
    hits = _iter_topic_hits(raw)
    total = None
    if isinstance(raw, dict):
        total = raw.get("total") or raw.get("totalCount")
        data = raw.get("data")
        if total is None and isinstance(data, dict):
            total = data.get("total") or data.get("totalCount")
    if INDEX.exists():
        try:
            idx = json.loads(INDEX.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            idx = {"topics": []}
    else:
        idx = {"topics": []}
    topics = idx.get("topics") if isinstance(idx, dict) else []
    if not isinstance(topics, list):
        topics = []
    added = 0
    topics_out: list[dict[str, Any]] = []
    audio_dir = BASE / "cache" / "audio"
    for item in hits:
        et, eid, title, author = _topic_keys(item)
        if not eid:
            continue
        topic = item.get("topicDTO") if isinstance(item.get("topicDTO"), dict) else item
        user = item.get("topicUserDTO") if isinstance(item.get("topicUserDTO"), dict) else {}
        aid = str(user.get("unionUserId") or author_id)
        aname = author or str(user.get("name") or "")
        gmt = topic.get("gmtCreate") if isinstance(topic, dict) else None
        entry = {
            "entityType": et,
            "entityId": eid,
            "title": title,
            "authorName": aname,
            "authorId": aid,
            "gmtCreate": gmt,
            "hasBody": post_path(et, eid).exists(),
            "hasAudio": (audio_dir / f"{et}__{eid}.mp3").exists(),
        }
        found = False
        for t in topics:
            if t.get("entityType") == et and str(t.get("entityId")) == eid:
                for k, v in entry.items():
                    if k in ("hasBody", "hasAudio"):
                        t[k] = bool(t.get(k)) or bool(v)
                    elif v is not None and v != "":
                        t[k] = v
                found = True
                break
        if not found:
            topics.append(entry)
            added += 1
        topics_out.append(entry)
    idx["topics"] = topics
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    has_more = len(hits) >= page_size
    if total is not None:
        try:
            has_more = int(page) * page_size < int(total)
        except (TypeError, ValueError):
            pass
    return {
        "authorId": author_id,
        "page": int(page),
        "pageSize": page_size,
        "topics": topics_out,
        "added": added,
        "hasMore": has_more,
        "total": total,
    }

def prefetch_author_topics(author_id: str, *, limit: int = 8, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """searchTopic by author, fetch missing bodies (sequential, capped)."""
    author_id = str(author_id)
    limit = max(1, min(int(limit), 20))
    page_size = max(limit, 10)
    raw = search_topic(author_id=author_id, page=1, page_size=page_size, timeout=timeout)
    hits = _iter_topic_hits(raw)
    fetched = 0
    skipped = 0
    errors: list[str] = []
    for item in hits:
        if fetched >= limit:
            break
        et, eid, title, author = _topic_keys(item)
        if not eid:
            continue
        if post_path(et, eid).exists():
            skipped += 1
            # still record index metadata
            _update_index(
                {
                    "entityType": et,
                    "entityId": eid,
                    "title": title,
                    "authorName": author,
                    "authorId": author_id,
                    "gmtCreate": (item.get("topicDTO") or item).get("gmtCreate")
                    if isinstance(item.get("topicDTO") or item, dict)
                    else None,
                }
            )
            continue
        try:
            fetch_topic(et, eid, title=title, author_name=author, timeout=timeout)
            fetched += 1
        except Exception as e:
            errors.append(f"{et}/{eid}: {type(e).__name__}")
            if len(errors) >= 5:
                break
    return {
        "authorId": author_id,
        "hits": len(hits),
        "fetched": fetched,
        "skipped": skipped,
        "errors": errors,
    }


def upsert_authors_from_user_search(
    keyword: str,
    *,
    page: int = 1,
    page_size: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """userSearch → merge into authors.json. Returns matched authors (normalized)."""
    raw = user_search(keyword, page=page, page_size=page_size, timeout=timeout)
    users = _iter_user_hits(raw)
    if not users:
        return []
    existing = []
    if AUTHORS.exists():
        try:
            existing = json.loads(AUTHORS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = []
    if not isinstance(existing, list):
        existing = []
    by_id = {str(a.get("unionUserId") or a.get("id") or ""): a for a in existing if isinstance(a, dict)}
    out: list[dict] = []
    for u in users:
        entry = normalize_user_hit(u)
        if not entry:
            continue
        uid = entry["unionUserId"]
        merged = {**by_id.get(uid, {}), **{k: v for k, v in entry.items() if v or k == "unionUserId"}}
        by_id[uid] = merged
        out.append(
            {
                "id": uid,
                "unionUserId": uid,
                "name": merged.get("name") or "",
                "avatar": merged.get("avatar") or "",
                "topicCount": merged.get("topicCount") or 0,
            }
        )
    AUTHORS.parent.mkdir(parents=True, exist_ok=True)
    AUTHORS.write_text(json.dumps(list(by_id.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def merge_browse_page_into_index(
    kind: str,
    *,
    page: int = 1,
    page_size: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch hot or digested searchTopic page; merge titles into index."""
    kind = (kind or "").strip().lower()
    if kind not in ("hot", "digested"):
        raise ValueError("kind must be hot|digested")
    page_size = max(1, min(int(page_size), 10))
    kwargs: dict[str, Any] = {"page": page, "page_size": page_size, "timeout": timeout}
    if kind == "hot":
        kwargs["is_hot"] = True
    else:
        kwargs["is_digested"] = True
    raw = search_topic(**kwargs)
    hits = _iter_topic_hits(raw)
    total = _payload_total(raw)
    if INDEX.exists():
        try:
            idx = json.loads(INDEX.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            idx = {"topics": []}
    else:
        idx = {"topics": []}
    topics = idx.get("topics") if isinstance(idx, dict) else []
    if not isinstance(topics, list):
        topics = []
    added = 0
    topics_out: list[dict[str, Any]] = []
    for item in hits:
        entry = normalize_topic_hit(item)
        if not entry:
            continue
        if _merge_topic_entries(topics, entry):
            added += 1
        topics_out.append(entry)
    idx["topics"] = topics
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    has_more = len(hits) >= page_size
    if total is not None:
        try:
            has_more = int(page) * page_size < int(total)
        except (TypeError, ValueError):
            pass
    return {
        "kind": kind,
        "page": int(page),
        "pageSize": page_size,
        "topics": topics_out,
        "added": added,
        "hasMore": has_more,
        "total": total,
    }




__all__ = [
    "MCP_URL",
    "TOKENS_PATH",
    "is_configured",
    "load_tokens",
    "save_tokens",
    "get_valid_access_token",
    "call_tool",
    "topic_detail",
    "search_topic",
    "user_search",
    "fetch_topic",
    "save_post",
    "prefetch_author_topics",
    "upsert_authors_from_user_search",
    "merge_author_page_into_index",
    "merge_browse_page_into_index",
    "normalize_user_hit",
    "normalize_topic_hit",
    "_iter_topic_hits",
    "_iter_user_hits",
]
