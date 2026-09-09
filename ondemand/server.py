#!/usr/bin/env python3
"""有人听 Phase-2 on-demand API: search → titles → transcript + TTS cache.

Happy path uses mechanical mcp_client (streamable HTTP) — no agent queue worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import mcp_client

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
POSTS = DATA / "posts"
AUDIO_CACHE = BASE / "cache" / "audio"
QUEUE = BASE / "queue"
WEB = BASE / "web"

EDGE_TTS = Path(
    os.environ.get("EDGE_TTS_BIN")
    or shutil.which("edge-tts")
    or "/workspace/.venv-tts/bin/edge-tts"
)
VOICE = "zh-CN-YunxiNeural"
RATE = "-5%"
MAX_CHARS_MVP = 3000
MCP_FETCH_TIMEOUT = 45.0

logger = logging.getLogger("ondemand")

for d in (DATA, POSTS, AUDIO_CACHE, QUEUE, WEB):
    d.mkdir(parents=True, exist_ok=True)

authors_path = DATA / "authors.json"
topics_path = DATA / "topics_index.json"
if not authors_path.exists():
    authors_path.write_text("[]\n", encoding="utf-8")
if not topics_path.exists():
    topics_path.write_text('{"topics":[]}\n', encoding="utf-8")

app = FastAPI(title="听生财", version="0.5.0")

_prefetch_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp-prefetch")
_prefetch_lock = threading.Lock()
_prefetch_inflight: set[str] = set()

_tts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts-bg")
_tts_lock = threading.Lock()
_tts_inflight: set[str] = set()
_tts_errors: dict[str, str] = {}


def _load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _authors() -> list[dict]:
    data = _load_json(authors_path, [])
    return data if isinstance(data, list) else []


def _topics() -> list[dict]:
    data = _load_json(topics_path, {"topics": []})
    if isinstance(data, dict):
        return data.get("topics") or []
    if isinstance(data, list):
        return data
    return []


def _post_path(entity_type: str, entity_id: str) -> Path:
    return POSTS / f"{entity_type}__{entity_id}.json"


def _audio_path(entity_type: str, entity_id: str) -> Path:
    return AUDIO_CACHE / f"{entity_type}__{entity_id}.mp3"


def _audio_url(entity_type: str, entity_id: str) -> str:
    return f"/cache/audio/{entity_type}__{entity_id}.mp3"


def _fuzzy_contains(hay: str, needle: str) -> bool:
    if not needle:
        return True
    return needle.casefold() in (hay or "").casefold()


def _kick_prefetch(author_id: str, limit: int = 5) -> None:
    """Fire-and-forget Top-N body prefetch for an author."""
    author_id = str(author_id or "").strip()
    if not author_id or not mcp_client.is_configured():
        return
    key = f"{author_id}:{limit}"
    with _prefetch_lock:
        if key in _prefetch_inflight:
            return
        _prefetch_inflight.add(key)

    def _run() -> None:
        try:
            mcp_client.prefetch_author_topics(author_id, limit=limit, timeout=MCP_FETCH_TIMEOUT)
        except Exception as e:
            logger.warning("prefetch author=%s failed: %s", author_id, type(e).__name__)
        finally:
            with _prefetch_lock:
                _prefetch_inflight.discard(key)

    _prefetch_pool.submit(_run)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "ting-shengcai",
        "port": 8766,
        "edge_tts": EDGE_TTS.exists(),
        "authors": len(_authors()),
        "topics": len(_topics()),
        "mcp_configured": bool(mcp_client.is_configured()),
    }


def _normalize_topic_list_item(t: dict) -> dict:
    et = t.get("entityType") or t.get("entity_type")
    eid = t.get("entityId") or t.get("entity_id")
    return {
        "entityType": et,
        "entityId": eid,
        "title": t.get("title") or "",
        "authorName": t.get("authorName") or t.get("author_name") or "",
        "authorId": str(t.get("authorId") or t.get("author_id") or ""),
        "gmtCreate": t.get("gmtCreate") or t.get("gmt_create") or "",
        "hasBody": bool(t.get("hasBody")) or _post_path(str(et), str(eid)).exists(),
        "hasAudio": bool(t.get("hasAudio")) or _audio_path(str(et), str(eid)).exists(),
    }


def _local_search(q: str) -> tuple[list[dict], list[dict]]:
    """Fuzzy fallback against local authors.json + topics_index.json."""
    authors = _authors()
    topics = _topics()
    matched_authors: list[dict] = []
    for a in authors:
        name = str(a.get("name") or "")
        if q and not _fuzzy_contains(name, q):
            continue
        matched_authors.append(
            {
                "id": a.get("id") or a.get("unionUserId") or a.get("union_user_id"),
                "name": name,
                "avatar": a.get("avatar") or a.get("avatarUrl") or "",
                "topicCount": a.get("topicCount")
                or a.get("topic_count")
                or a.get("count")
                or 0,
            }
        )
    author_ids = {str(a["id"]) for a in matched_authors if a.get("id") is not None}
    matched_topics: list[dict] = []
    for t in topics:
        title = str(t.get("title") or "")
        author_name = str(t.get("authorName") or t.get("author_name") or "")
        author_id = str(t.get("authorId") or t.get("author_id") or "")
        hit = (
            _fuzzy_contains(title, q)
            or _fuzzy_contains(author_name, q)
            or (author_id and author_id in author_ids)
        )
        if not hit:
            continue
        matched_topics.append(_normalize_topic_list_item(t))
    matched_topics.sort(
        key=lambda x: (
            0 if _fuzzy_contains(str(x.get("authorName") or ""), q) else 1,
            str(x.get("gmtCreate") or ""),
        ),
        reverse=False,
    )
    return matched_authors, matched_topics


@app.get("/api/search")
def search(q: str = "") -> dict:
    q = (q or "").strip()
    if not q:
        return {"authors": [], "topics": [], "q": q, "source": "empty"}

    matched_authors: list[dict] = []
    matched_topics: list[dict] = []
    source = "local"
    live_err = None

    if mcp_client.is_configured():
        source = "live"
        # Live authors via userSearch → upsert authors.json
        try:
            live_authors = mcp_client.upsert_authors_from_user_search(
                q, page=1, page_size=10, timeout=MCP_FETCH_TIMEOUT
            )
            for a in live_authors:
                matched_authors.append(
                    {
                        "id": a.get("id") or a.get("unionUserId"),
                        "name": a.get("name") or "",
                        "avatar": a.get("avatar") or "",
                        "topicCount": a.get("topicCount") or 0,
                    }
                )
        except Exception as e:
            live_err = f"userSearch: {type(e).__name__}: {e}"[:300]
            logger.warning("live userSearch failed: %s", type(e).__name__)

        # Live topics via searchTopic(keyword=q)
        try:
            raw = mcp_client.search_topic(
                keyword=q, page=1, page_size=10, timeout=MCP_FETCH_TIMEOUT
            )
            hits = mcp_client._iter_topic_hits(raw)
            # merge titles into index
            for item in hits:
                entry = mcp_client.normalize_topic_hit(item)
                if not entry:
                    continue
                matched_topics.append(_normalize_topic_list_item(entry))
            # persist into topics_index (best-effort, reuse merge helpers)
            if hits:
                try:
                    if topics_path.exists():
                        idx = _load_json(topics_path, {"topics": []})
                    else:
                        idx = {"topics": []}
                    topics_list = idx.get("topics") if isinstance(idx, dict) else []
                    if not isinstance(topics_list, list):
                        topics_list = []
                    for item in hits:
                        entry = mcp_client.normalize_topic_hit(item)
                        if entry:
                            mcp_client._merge_topic_entries(topics_list, entry)
                    idx = {"topics": topics_list}
                    topics_path.write_text(
                        json.dumps(idx, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except Exception as e:
                    logger.warning("merge search topics into index failed: %s", type(e).__name__)
        except Exception as e:
            msg = f"searchTopic: {type(e).__name__}: {e}"[:300]
            live_err = f"{live_err}; {msg}" if live_err else msg
            logger.warning("live searchTopic failed: %s", type(e).__name__)

        # Merge local fuzzy hits (dedupe)
        local_authors, local_topics = _local_search(q)
        seen_a = {str(a.get("id")) for a in matched_authors if a.get("id") is not None}
        for a in local_authors:
            aid = str(a.get("id")) if a.get("id") is not None else ""
            if aid and aid not in seen_a:
                matched_authors.append(a)
                seen_a.add(aid)
        seen_t = {
            (str(t.get("entityType")), str(t.get("entityId")))
            for t in matched_topics
            if t.get("entityId") is not None
        }
        for t in local_topics:
            key = (str(t.get("entityType")), str(t.get("entityId")))
            if key[1] == "None":
                continue
            if key not in seen_t:
                matched_topics.append(t)
                seen_t.add(key)

        if live_err and not matched_authors and not matched_topics:
            # hard fallback
            matched_authors, matched_topics = _local_search(q)
            source = "local_fallback"
    else:
        matched_authors, matched_topics = _local_search(q)
        source = "local"

    if matched_authors:
        aid = matched_authors[0].get("id")
        if aid is not None:
            _kick_prefetch(str(aid), limit=5)

    out: dict[str, Any] = {
        "authors": matched_authors,
        "topics": matched_topics,
        "q": q,
        "source": source,
    }
    if live_err:
        out["liveError"] = live_err
    return out


@app.get("/api/browse")
def browse(kind: str = "hot", page: int = 1, pageSize: int = 10) -> dict:
    """Browse 热门榜 (hot) or 精华帖 (digested) via searchTopic."""
    kind = (kind or "hot").strip().lower()
    if kind not in ("hot", "digested"):
        raise HTTPException(status_code=400, detail={"error": "kind must be hot|digested"})
    page = max(1, int(page or 1))
    page_size = max(1, min(int(pageSize or 10), 10))
    if not mcp_client.is_configured():
        raise HTTPException(status_code=503, detail={"error": "MCP not configured"})
    try:
        meta = mcp_client.merge_browse_page_into_index(
            kind, page=page, page_size=page_size, timeout=MCP_FETCH_TIMEOUT
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"error": str(e)[:300], "kind": kind, "page": page},
        ) from e
    topics = [_normalize_topic_list_item(t) for t in (meta.get("topics") or [])]
    return {
        "kind": kind,
        "page": page,
        "pageSize": page_size,
        "topics": topics,
        "hasMore": bool(meta.get("hasMore")),
        "total": meta.get("total"),
    }


@app.get("/api/authors/{union_user_id}/topics")
def author_topics(union_user_id: str, page: int = 1, pageSize: int = 10, live: int = 1) -> dict:
    """List author titles. live=1 (default): pull one MCP searchTopic page and merge into index."""
    page = max(1, int(page or 1))
    page_size = max(1, min(int(pageSize or 10), 10))
    live_meta = None
    if live and mcp_client.is_configured():
        try:
            live_meta = mcp_client.merge_author_page_into_index(
                str(union_user_id), page=page, page_size=page_size, timeout=MCP_FETCH_TIMEOUT
            )
        except Exception as e:
            live_meta = {"error": str(e)[:300], "hasMore": False}

    topics = _topics()
    out = []
    for t in topics:
        author_id = str(t.get("authorId") or t.get("author_id") or "")
        if author_id != str(union_user_id):
            continue
        et = t.get("entityType") or t.get("entity_type")
        eid = t.get("entityId") or t.get("entity_id")
        out.append(
            {
                "entityType": et,
                "entityId": eid,
                "title": t.get("title") or "",
                "authorName": t.get("authorName") or t.get("author_name") or "",
                "authorId": author_id,
                "gmtCreate": t.get("gmtCreate") or t.get("gmt_create") or "",
                "hasBody": bool(t.get("hasBody")) or _post_path(str(et), str(eid)).exists(),
                "hasAudio": bool(t.get("hasAudio")) or _audio_path(str(et), str(eid)).exists(),
            }
        )
    # newest first when gmtCreate present
    out.sort(
        key=lambda x: int(x["gmtCreate"] or 0) if str(x.get("gmtCreate") or "").isdigit() else 0,
        reverse=True,
    )
    has_more = bool(live_meta.get("hasMore")) if isinstance(live_meta, dict) else False
    page_topics = []
    if isinstance(live_meta, dict):
        for t in live_meta.get("topics") or []:
            if not isinstance(t, dict):
                continue
            et = t.get("entityType")
            eid = t.get("entityId")
            page_topics.append(
                {
                    "entityType": et,
                    "entityId": eid,
                    "title": t.get("title") or "",
                    "authorName": t.get("authorName") or "",
                    "authorId": t.get("authorId") or union_user_id,
                    "gmtCreate": t.get("gmtCreate") or "",
                    "hasBody": bool(t.get("hasBody")) or _post_path(str(et), str(eid)).exists(),
                    "hasAudio": bool(t.get("hasAudio")) or _audio_path(str(et), str(eid)).exists(),
                }
            )
    return {
        "authorId": union_user_id,
        "topics": out,
        "pageTopics": page_topics,
        "page": page,
        "pageSize": page_size,
        "hasMore": has_more,
        "live": live_meta,
    }


def _queue_path(entity_type: str, entity_id: str) -> Path:
    return QUEUE / f"{entity_type}__{entity_id}.json"


def _status_path(entity_type: str, entity_id: str) -> Path:
    return QUEUE / f"{entity_type}__{entity_id}.status.json"


def _sync_fetch_topic(entity_type: str, entity_id: str, title: str = "", author_name: str = "") -> dict[str, Any]:
    """Blocking MCP fetch + save. Raises RuntimeError / PermissionError etc."""
    if not mcp_client.is_configured():
        raise RuntimeError("MCP not configured; run: python mcp_login.py")
    return mcp_client.fetch_topic(
        entity_type,
        entity_id,
        title=title,
        author_name=author_name,
        timeout=MCP_FETCH_TIMEOUT,
    )


@app.post("/api/topics/{entity_type}/{entity_id}/fetch")
def fetch_topic(entity_type: str, entity_id: str, title: str = "", authorName: str = "") -> JSONResponse:
    """Sync MCP body fetch (same as GET miss path)."""
    if _post_path(entity_type, entity_id).exists():
        return JSONResponse({"status": "ready", "entityType": entity_type, "entityId": entity_id})
    try:
        _sync_fetch_topic(entity_type, entity_id, title=title, author_name=authorName)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "error": str(e)[:300],
                "entityType": entity_type,
                "entityId": entity_id,
            },
        )
    return JSONResponse({"status": "ready", "entityType": entity_type, "entityId": entity_id})


@app.get("/api/topics/{entity_type}/{entity_id}/status")
def topic_status(entity_type: str, entity_id: str) -> dict:
    if _post_path(entity_type, entity_id).exists():
        return {"status": "ready", "entityType": entity_type, "entityId": entity_id}
    sp = _status_path(entity_type, entity_id)
    if sp.exists():
        st = _load_json(sp, {})
        return {
            "status": st.get("status") or "queued",
            "error": st.get("error"),
            "entityType": entity_type,
            "entityId": entity_id,
        }
    if _queue_path(entity_type, entity_id).exists():
        return {"status": "queued", "entityType": entity_type, "entityId": entity_id}
    return {"status": "missing", "entityType": entity_type, "entityId": entity_id}


@app.get("/api/topics/{entity_type}/{entity_id}")
def get_topic(entity_type: str, entity_id: str, title: str = "", authorName: str = "") -> JSONResponse:
    path = _post_path(entity_type, entity_id)
    if not path.exists():
        try:
            _sync_fetch_topic(entity_type, entity_id, title=title, author_name=authorName)
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={
                    "needFetch": True,
                    "error": str(e)[:300],
                    "entityType": entity_type,
                    "entityId": entity_id,
                },
            )
        path = _post_path(entity_type, entity_id)
        if not path.exists():
            return JSONResponse(
                status_code=502,
                content={
                    "needFetch": True,
                    "error": "fetch completed but post file missing",
                    "entityType": entity_type,
                    "entityId": entity_id,
                },
            )
    post = _load_json(path, {})
    text = post.get("text") or post.get("content") or post.get("body") or ""
    title_out = post.get("title") or title or ""
    author_name = post.get("authorName") or post.get("author_name") or authorName or ""
    has_audio = _audio_path(entity_type, entity_id).exists()
    return JSONResponse(
        {
            "title": title_out,
            "authorName": author_name,
            "text": text,
            "hasAudio": has_audio,
            "entityType": entity_type,
            "entityId": entity_id,
            "audioUrl": _audio_url(entity_type, entity_id) if has_audio else None,
        }
    )


@app.post("/api/prefetch")
def prefetch(authorId: str = "", limit: int = 8) -> dict:
    """Background: searchTopic pages + fetch missing bodies (Top N)."""
    author_id = (authorId or "").strip()
    if not author_id:
        raise HTTPException(status_code=400, detail={"error": "authorId required"})
    if not mcp_client.is_configured():
        raise HTTPException(status_code=503, detail={"error": "MCP not configured; run python mcp_login.py"})
    limit = max(1, min(int(limit or 8), 20))
    _kick_prefetch(author_id, limit=limit)
    return {"ok": True, "authorId": author_id, "limit": limit, "started": True}


def _run_edge_tts(text: str, out_mp3: Path) -> None:
    """Generate mp3 via edge-tts; truncate long text for MVP if needed."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    note = ""
    if len(text) > MAX_CHARS_MVP:
        text = text[:MAX_CHARS_MVP]
        note = f"\n\n（语音为前 {MAX_CHARS_MVP} 字 MVP 截断）"
        text = text + note

    if not EDGE_TTS.exists():
        raise FileNotFoundError(f"edge-tts not found: {EDGE_TTS}")

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(text)
        tmp_path = tf.name

    try:
        cmd = [
            str(EDGE_TTS),
            "--voice",
            VOICE,
            f"--rate={RATE}",
            "--file",
            tmp_path,
            "--write-media",
            str(out_mp3),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0 or not out_mp3.exists() or out_mp3.stat().st_size == 0:
            # Fallback: --text with truncated content
            short = text[:1500]
            cmd2 = [
                str(EDGE_TTS),
                "--voice",
                VOICE,
                f"--rate={RATE}",
                "--text",
                short,
                "--write-media",
                str(out_mp3),
            ]
            proc2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=180)
            if proc2.returncode != 0 or not out_mp3.exists():
                raise RuntimeError(
                    f"edge-tts failed: {proc.stderr or proc.stdout or proc2.stderr}"
                )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.post("/api/topics/{entity_type}/{entity_id}/tts")
async def tts(entity_type: str, entity_id: str) -> dict:
    audio = _audio_path(entity_type, entity_id)
    if audio.exists() and audio.stat().st_size > 0:
        return {"url": _audio_url(entity_type, entity_id), "cached": True}

    path = _post_path(entity_type, entity_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail={"needFetch": True, "error": "no text cache"})

    post = _load_json(path, {})
    text = post.get("text") or post.get("content") or post.get("body") or ""
    if not str(text).strip():
        raise HTTPException(status_code=404, detail={"error": "empty text"})

    # Run blocking TTS off the event loop
    try:
        await asyncio.to_thread(_run_edge_tts, str(text), audio)
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)}) from e

    if not audio.exists():
        raise HTTPException(status_code=500, detail={"error": "tts produced no file"})

    return {"url": _audio_url(entity_type, entity_id), "cached": False}



def _topic_key(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}__{entity_id}"


def _ready_payload(entity_type: str, entity_id: str) -> dict[str, Any]:
    has_body = _post_path(entity_type, entity_id).exists()
    audio = _audio_path(entity_type, entity_id)
    has_audio = audio.exists() and audio.stat().st_size > 0
    key = _topic_key(entity_type, entity_id)
    with _tts_lock:
        tts_queued = key in _tts_inflight
        err = _tts_errors.get(key)
    out: dict[str, Any] = {
        "ok": True,
        "entityType": entity_type,
        "entityId": entity_id,
        "hasBody": has_body,
        "hasAudio": has_audio,
        "ttsQueued": tts_queued,
        "audioUrl": _audio_url(entity_type, entity_id) if has_audio else None,
    }
    if err and not has_audio:
        out["error"] = err
    return out


def _kick_tts(entity_type: str, entity_id: str) -> bool:
    """Enqueue background TTS if not already cached/inflight. Returns True if queued."""
    audio = _audio_path(entity_type, entity_id)
    if audio.exists() and audio.stat().st_size > 0:
        return False
    path = _post_path(entity_type, entity_id)
    if not path.exists():
        return False
    post = _load_json(path, {})
    text_body = post.get("text") or post.get("content") or post.get("body") or ""
    if not str(text_body).strip():
        return False
    key = _topic_key(entity_type, entity_id)
    with _tts_lock:
        if key in _tts_inflight:
            return True
        _tts_inflight.add(key)
        _tts_errors.pop(key, None)

    def _run() -> None:
        try:
            _run_edge_tts(str(text_body), audio)
            with _tts_lock:
                _tts_errors.pop(key, None)
        except Exception as e:
            logger.warning("bg tts %s failed: %s", key, type(e).__name__)
            with _tts_lock:
                _tts_errors[key] = str(e)[:300]
        finally:
            with _tts_lock:
                _tts_inflight.discard(key)

    _tts_pool.submit(_run)
    return True


@app.get("/api/topics/{entity_type}/{entity_id}/ready")
def topic_ready(entity_type: str, entity_id: str) -> dict:
    """Lightweight prepare-status for playlist polling."""
    return _ready_payload(entity_type, entity_id)


@app.post("/api/prepare")
async def prepare(body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:
    """Fetch body sync (if needed), kick TTS in background, return readiness.

    Body: {entityType, entityId, title?, authorName?, withAudio?: bool}
    """
    if not isinstance(body, dict):
        body = {}
    entity_type = str(body.get("entityType") or body.get("entity_type") or "").strip()
    entity_id = str(body.get("entityId") or body.get("entity_id") or "").strip()
    title = str(body.get("title") or "")
    author_name = str(body.get("authorName") or body.get("author_name") or "")
    with_audio = bool(body.get("withAudio", True))

    if not entity_type or not entity_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "entityType and entityId required"},
        )

    has_body = _post_path(entity_type, entity_id).exists()
    if not has_body:
        try:
            await asyncio.to_thread(
                _sync_fetch_topic, entity_type, entity_id, title, author_name
            )
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "hasBody": False,
                    "hasAudio": False,
                    "ttsQueued": False,
                    "error": str(e)[:300],
                    "entityType": entity_type,
                    "entityId": entity_id,
                },
            )
        has_body = _post_path(entity_type, entity_id).exists()
        if not has_body:
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "hasBody": False,
                    "hasAudio": False,
                    "ttsQueued": False,
                    "error": "fetch completed but post file missing",
                    "entityType": entity_type,
                    "entityId": entity_id,
                },
            )

    audio = _audio_path(entity_type, entity_id)
    has_audio = audio.exists() and audio.stat().st_size > 0
    tts_queued = False
    if with_audio and not has_audio:
        tts_queued = _kick_tts(entity_type, entity_id)

    payload = _ready_payload(entity_type, entity_id)
    payload["ok"] = True
    if tts_queued:
        payload["ttsQueued"] = True
    return JSONResponse(payload)


# Static mounts — API routes registered above first
app.mount("/cache/audio", StaticFiles(directory=str(AUDIO_CACHE)), name="audio")
app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8766,
        reload=False,
        app_dir=str(BASE),
    )
