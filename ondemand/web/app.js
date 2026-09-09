(() => {
  const $ = (sel) => document.querySelector(sel);
  const views = {
    home: $("#view-home"),
    results: $("#view-results"),
    reader: $("#view-reader"),
    playlist: $("#view-playlist"),
  };

  const PLAYLIST_KEY = "someone-listening-playlist-v1";
  const RATE_KEY = "someone-listening-rate-v1";

  const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];
  function formatRate(r) {
    return Number.isInteger(r) ? r + "x" : String(r) + "x";
  }
  function getSavedRate() {
    const v = parseFloat(localStorage.getItem(RATE_KEY) || "1");
    return RATES.includes(v) ? v : 1;
  }
  function setSavedRate(v) {
    const r = parseFloat(v) || 1;
    const rateVal = RATES.includes(r) ? r : 1;
    localStorage.setItem(RATE_KEY, String(rateVal));
    if (btnRate) {
      btnRate.textContent = formatRate(rateVal);
      btnRate.setAttribute("aria-label", "倍速 " + formatRate(rateVal));
    }
    if (plBtnRate) {
      plBtnRate.textContent = formatRate(rateVal);
      plBtnRate.setAttribute("aria-label", "倍速 " + formatRate(rateVal));
    }
    audio.playbackRate = rateVal;
    plAudio.playbackRate = rateVal;
  }
  function cycleRate() {
    const cur = getSavedRate();
    const i = RATES.indexOf(cur);
    const next = RATES[(i < 0 ? 0 : i + 1) % RATES.length];
    setSavedRate(next);
  }
  function applyRateTo(el) {
    const r = getSavedRate();
    try { el.playbackRate = r; } catch (_) {}
  }
  const STATUS = {
    queued: { label: "排队中", cls: "queued" },
    fetching: { label: "拉文中", cls: "fetching" },
    hasBody: { label: "有正文", cls: "has-body" },
    tts: { label: "生成语音中", cls: "tts" },
    ready: { label: "可播放", cls: "ready" },
    fail: { label: "失败", cls: "fail" },
  };

  let lastQ = "";
  let lastResults = { authors: [], topics: [] };
  let current = null; // {entityType, entityId, title, authorName}
  let lastViewBeforePlaylist = "home";
  let playlist = loadPlaylist();
  let plIndex = -1;
  let pollTimer = null;
  let authorCtx = { id: null, name: "", page: 1, hasMore: false, loading: false };
  let browseCtx = { kind: null, page: 1, hasMore: false, loading: false, topics: [] };
  let fromPlaylist = false;
  let playSession = 0; // bump to cancel stale open/play

  const audio = $("#audio");
  const btnPlay = $("#btn-play");
  const btnTts = $("#btn-tts");
  const seek = $("#seek");
  const btnRate = $("#btn-rate");

  const plAudio = $("#pl-audio");
  const plPlay = $("#pl-play");
  const plSeek = $("#pl-seek");
  const plBtnRate = $("#pl-btn-rate");

  function show(name, opts) {
    const scroll = !opts || opts.scroll !== false;
    Object.entries(views).forEach(([k, el]) => {
      el.classList.toggle("hidden", k !== name);
    });
    const readerDock = $("#player-bar");
    const plDock = $("#pl-player-bar");
    if (readerDock) readerDock.classList.toggle("hidden", name !== "reader");
    if (plDock) plDock.classList.toggle("hidden", name !== "playlist");
    if (scroll) window.scrollTo(0, 0);
    if (name === "playlist") {
      startPolling();
      renderPlaylist();
    } else {
      stopPolling();
    }
  }

  function fmt(t) {
    if (!isFinite(t)) return "0:00";
    const m = Math.floor(t / 60);
    const s = Math.floor(t % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  }

  function loadPlaylist() {
    try {
      const raw = localStorage.getItem(PLAYLIST_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch {
      return [];
    }
  }

  function savePlaylist() {
    localStorage.setItem(PLAYLIST_KEY, JSON.stringify(playlist));
  
  }

  function updatePlaylistBadge() {
    const btn = $("#btn-playlist");
    const n = playlist.length;
    btn.textContent = `待听 (${n})`;
    btn.classList.toggle("has-items", n > 0);
  }

  function itemKey(it) {
    return `${it.entityType}::${it.entityId}`;
  }

  function findIndex(et, eid) {
    return playlist.findIndex(
      (x) => String(x.entityType) === String(et) && String(x.entityId) === String(eid)
    );
  }

  function addToPlaylist(t) {
    const entityType = t.entityType || t.entity_type;
    const entityId = String(t.entityId ?? t.entity_id ?? "");
    if (!entityType || !entityId || entityId === "undefined" || entityId === "null") {
      toast("无法加入：缺少帖子 ID");
      console.warn("addToPlaylist missing id", t);
      return false;
    }
    if (findIndex(entityType, entityId) >= 0) {
      toast("已在待听列表");
      return false;
    }
    const item = {
      entityType,
      entityId,
      title: t.title || "",
      authorName: t.authorName || "",
      status: "queued",
      hasBody: false,
      hasAudio: false,
      audioUrl: null,
      error: null,
      addedAt: Date.now(),
    };
    playlist.push(item);
    savePlaylist();
    renderPlaylist();
    toast("已加入待听");
    // fire-and-forget prepare
    prepareItem(item).catch(() => {});
    return true;
  }

  function removeFromPlaylist(et, eid) {
    const i = findIndex(et, eid);
    if (i < 0) return;
    const wasPlaying = plIndex === i;
    playlist.splice(i, 1);
    if (plIndex === i) {
      plAudio.pause();
      plIndex = -1;
      $("#pl-now").textContent = "未在播放";
      plPlay.textContent = "▶";
      plPlay.disabled = true;
    } else if (plIndex > i) {
      plIndex -= 1;
    }
    savePlaylist();
    renderPlaylist();
    if (wasPlaying) {
      // try next ready
      playFromIndex(i, true);
    }
  }

  function clearPlaylist() {
    if (!playlist.length) return;
    if (!confirm("清空全部待听？")) return;
    playlist = [];
    plIndex = -1;
    plAudio.pause();
    plAudio.removeAttribute("src");
    $("#pl-now").textContent = "未在播放";
    plPlay.disabled = true;
    plPlay.textContent = "▶";
    savePlaylist();
    renderPlaylist();
  }

  function toast(msg) {
    // lightweight: reuse hint style via temporary element
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.style.cssText =
        "position:fixed;left:50%;bottom:100px;transform:translateX(-50%);z-index:50;" +
        "background:#243044;color:#e7ecf3;padding:10px 16px;border-radius:999px;" +
        "font-size:.85rem;border:1px solid #2a3548;opacity:0;transition:opacity .2s;pointer-events:none;";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = "1";
    clearTimeout(el._t);
    el._t = setTimeout(() => {
      el.style.opacity = "0";
    }, 1600);
  }

  function statusMeta(item) {
    const st = item.status || "queued";
    return STATUS[st] || STATUS.queued;
  }

  function deriveStatus(item, ready) {
    if (ready && ready.error && !ready.hasAudio) {
      item.error = ready.error;
      item.status = "fail";
      return;
    }
    item.error = null;
    item.hasBody = !!ready?.hasBody;
    item.hasAudio = !!ready?.hasAudio;
    item.audioUrl = ready?.audioUrl || null;
    if (ready?.hasAudio) {
      item.status = "ready";
    } else if (ready?.ttsQueued || item.status === "tts") {
      item.status = "tts";
    } else if (ready?.hasBody) {
      item.status = "hasBody";
    } else if (item.status === "fetching") {
      item.status = "fetching";
    } else {
      item.status = item.status || "queued";
    }
  }

  async function prepareItem(item) {
    item.status = item.hasBody ? "tts" : "fetching";
    renderPlaylist();
    const { ok, data } = await api("/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entityType: item.entityType,
        entityId: item.entityId,
        title: item.title,
        authorName: item.authorName,
        withAudio: true,
      }),
    });
    if (!ok && data) {
      item.status = "fail";
      item.error = data.error || data.detail?.error || `HTTP error`;
      savePlaylist();
      renderPlaylist();
      return data;
    }
    deriveStatus(item, data);
    // if body ready but no audio yet and tts queued, keep polling
    if (data?.hasBody && !data?.hasAudio) {
      item.status = data.ttsQueued ? "tts" : "hasBody";
    }
    savePlaylist();
    renderPlaylist();
    return data;
  }

  async function pollItem(item) {
    const et = encodeURIComponent(item.entityType);
    const eid = encodeURIComponent(item.entityId);
    const { ok, data } = await api(`/api/topics/${et}/${eid}/ready`);
    if (!ok) return;
    const prev = item.status;
    deriveStatus(item, data);
    if (item.status !== prev || item.audioUrl !== (data.audioUrl || null)) {
      savePlaylist();
      renderPlaylist();
    }
  }

  async function pollAll() {
    const pending = playlist.filter((x) => x.status !== "ready" && x.status !== "fail");
    await Promise.all(pending.map((it) => pollItem(it).catch(() => {})));
  }

  function startPolling() {
    stopPolling();
    pollAll();
    pollTimer = setInterval(pollAll, 2500);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function renderPlaylist() {
    updatePlaylistBadge();
    const ul = $("#playlist-items");
    const empty = $("#pl-empty");
    ul.innerHTML = "";
    if (!playlist.length) {
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    playlist.forEach((item, idx) => {
      const meta = statusMeta(item);
      const li = document.createElement("li");
      if (idx === plIndex) li.classList.add("playing");
      li.innerHTML = `
        <div class="pl-item-main" role="button" tabindex="0">
          <div class="pl-item-top">
            <div class="t">${escapeHtml(item.title || "（无标题）")}</div>
            <span class="status-badge ${meta.cls}">${meta.label}</span>
          </div>
          <div class="pl-item-meta">${escapeHtml(item.authorName || "")}${
            item.error ? " · " + escapeHtml(item.error) : ""
          }</div>
        </div>
        <div class="pl-item-actions">
          <button type="button" class="btn-mini danger" data-act="rm">移除</button>
        </div>`;
      const main = li.querySelector(".pl-item-main");
      const go = () => playAndOpenFromPlaylist(idx);
      main.addEventListener("click", go);
      main.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          go();
        }
      });
      li.querySelector('[data-act="rm"]').addEventListener("click", (e) => {
        e.stopPropagation();
        removeFromPlaylist(item.entityType, item.entityId);
      });
      ul.appendChild(li);
    });
  }

  function nextReadyIndex(from, inclusive) {
    const n = playlist.length;
    if (!n) return -1;
    const start = inclusive ? from : from + 1;
    for (let i = start; i < n; i++) {
      if (playlist[i].status === "ready" && playlist[i].audioUrl) return i;
    }
    return -1;
  }

  function prevReadyIndex(from) {
    for (let i = from - 1; i >= 0; i--) {
      if (playlist[i].status === "ready" && playlist[i].audioUrl) return i;
    }
    return -1;
  }

  async function playFromIndex(idx, autoSkip) {
    if (idx < 0 || idx >= playlist.length) {
      plPlay.disabled = true;
      $("#pl-now").textContent = "未在播放";
      return;
    }
    const session = ++playSession;
    fromPlaylist = false;
    stopReaderAudio();
    let i = idx;
    let item = playlist[i];
    if (item.status !== "ready" || !item.audioUrl) {
      // try prepare then wait a bit / skip
      await prepareItem(item).catch(() => {});
      await pollItem(item).catch(() => {});
      if (item.status !== "ready" || !item.audioUrl) {
        if (autoSkip) {
          const ni = nextReadyIndex(i, false);
          if (ni >= 0) return playFromIndex(ni, true);
          toast("暂无可播放项（仍在准备）");
          plIndex = -1;
          $("#pl-now").textContent = "未在播放";
          plPlay.disabled = true;
          plPlay.textContent = "▶";
          return;
        }
        toast("该项尚未可播放，已触发准备");
        return;
      }
    }
    if (session !== playSession) return;
    plIndex = i;
    item = playlist[i];
    $("#pl-now").textContent = `正在播放：${item.title || "（无标题）"}`;
    plAudio.src = item.audioUrl;
    applyRateTo(plAudio);
    plAudio.load();
    const want = getSavedRate();
    plAudio.playbackRate = want;
    plPlay.disabled = false;
    plSeek.disabled = false;
    try {
      await plAudio.play();
      if (session !== playSession) {
        plAudio.pause();
        return;
      }
      plPlay.textContent = "⏸";
    } catch {
      plPlay.textContent = "▶";
    }
    renderPlaylist();
  }

  function renderResults(data, opts) {
    lastResults = data;
    const authorsEl = $("#authors");
    const topicsEl = $("#topics");
    const authorsSec = $("#authors-section");
    const statusEl = $("#results-status");
    authorsEl.innerHTML = "";
    topicsEl.innerHTML = "";

    const authors = data.authors || [];
    const topics = data.topics || [];
    const browsing = !!browseCtx.kind;

    if (statusEl && !opts?.loading) statusEl.classList.add("hidden");

    if (authorsSec) authorsSec.classList.toggle("hidden", browsing || !authors.length);

    if (!authors.length) {
      authorsEl.innerHTML = browsing ? "" : `<p class="hint">无匹配作者</p>`;
    } else {
      authors.forEach((a) => {
        const div = document.createElement("div");
        div.className = "author-card";
        div.innerHTML = `
          <img src="${escapeHtml(a.avatar || "")}" alt="" loading="lazy"
               onerror="this.style.visibility='hidden'" />
          <div>
            <div class="name">${escapeHtml(a.name)}</div>
            <div class="sub">${a.topicCount || 0} 篇 · ID ${escapeHtml(a.id)}</div>
          </div>`;
        div.addEventListener("click", () => openAuthor(a.id, a.name, 1));
        authorsEl.appendChild(div);
      });
    }

    if (!topics.length) {
      topicsEl.innerHTML = `<li class="hint" style="cursor:default">${opts?.loading ? "加载中…" : "无匹配标题"}</li>`;
    } else {
      topics.forEach((t) => {
        const li = document.createElement("li");
        const bodyMark = t.hasBody
          ? `<span class="dot-ok">有正文</span>`
          : `<span class="dot-no">待缓存</span>`;
        li.innerHTML = `
          <div class="topic-row">
            <div class="main">
              <div class="t">${escapeHtml(t.title)}</div>
              <div class="s">${escapeHtml(t.authorName)} · ${bodyMark}</div>
            </div>
            <button type="button" class="btn-mini add-q">加入待听</button>
          </div>`;
        li.querySelector(".main").addEventListener("click", () => openTopic(t));
        li.querySelector(".add-q").addEventListener("click", (e) => {
          e.stopPropagation();
          addToPlaylist(t);
        });
        topicsEl.appendChild(li);
      });
    }
    const keepScroll = !!(opts && opts.keepScroll);
    // 翻页：不要滚回整页顶部；只把列表标题对齐到视口上方
    show("results", { scroll: !keepScroll });
    if (keepScroll) {
      const anchor = $("#topics-heading") || $("#pager");
      if (anchor) {
        try {
          anchor.scrollIntoView({ block: "start", behavior: "auto" });
        } catch (_) {}
      }
    }
    updatePager();
  }

  async function doSearch(q) {
    lastQ = (q || "").trim();
    if (!lastQ) return;
    $("#q").value = lastQ;
    browseCtx = { kind: null, page: 1, hasMore: false, loading: false, topics: [] };
    authorCtx = { id: null, name: "", page: 1, hasMore: false, loading: false };
    const heading = $("#topics-heading");
    if (heading) heading.textContent = "标题";
    const { ok, data } = await api(`/api/search?q=${encodeURIComponent(lastQ)}`);
    if (!ok) {
      alert("搜索失败");
      return;
    }
    renderResults(data);
  }

  function updatePager() {
    const pager = $("#pager");
    const prev = $("#pager-prev");
    const next = $("#pager-next");
    const label = $("#pager-label");
    if (!pager || !prev || !next || !label) return;

    let page = 1;
    let hasMore = false;
    let loading = false;
    let active = false;

    if (browseCtx.kind) {
      active = true;
      page = browseCtx.page || 1;
      hasMore = !!browseCtx.hasMore;
      loading = !!browseCtx.loading;
    } else if (authorCtx.id) {
      active = true;
      page = authorCtx.page || 1;
      hasMore = !!authorCtx.hasMore;
      loading = !!authorCtx.loading;
    }

    pager.classList.toggle("hidden", !active);
    if (!active) return;
    label.textContent = loading ? `第 ${page} 页 · 加载中` : `第 ${page} 页`;
    prev.disabled = loading || page <= 1;
    next.disabled = loading || !hasMore;
  }

  async function openAuthor(id, name, page = 1) {
    if (authorCtx.loading) return;
    browseCtx = { kind: null, page: 1, hasMore: false, loading: false, topics: [] };
    const heading = $("#topics-heading");
    if (heading) heading.textContent = "标题";
    authorCtx.loading = true;
    authorCtx.id = id;
    authorCtx.name = name || authorCtx.name || "";
    authorCtx.page = page;
    updatePager();
    const statusEl = $("#results-status");
    if (statusEl) {
      statusEl.textContent = "加载中…";
      statusEl.classList.remove("hidden");
    }
    // stay on results if already there; first open scrolls once via render
    const already = !views.results.classList.contains("hidden");
    try {
      const { ok, data } = await api(
        `/api/authors/${encodeURIComponent(id)}/topics?page=${page}&pageSize=10&live=1`
      );
      if (!ok) {
        alert("加载作者标题失败");
        return;
      }
      authorCtx.hasMore = !!data.hasMore;
      authorCtx.page = page;
      // Prefer this page's live topics when API provides pageTopics; else slice/fallback to list
      let topics = data.pageTopics || data.live?.topics || null;
      if (!topics || !topics.length) {
        // older cumulative response: take a window
        const all = (data.topics || []).map((t) => ({
          ...t,
          authorName: t.authorName || authorCtx.name,
        }));
        const start = (page - 1) * 10;
        topics = all.slice(start, start + 10);
        if (!topics.length) topics = all.slice(0, 10);
        // if cumulative grew, hasMore may still be true from live
      } else {
        topics = topics.map((t) => ({
          ...t,
          authorName: t.authorName || authorCtx.name,
        }));
      }
      const authors = (lastResults.authors || []).filter((a) => String(a.id) === String(id));
      if (!authors.length) authors.push({ id, name: authorCtx.name });
      renderResults(
        { authors, topics, q: authorCtx.name },
        { keepScroll: already && page > 1 }
      );
    } finally {
      authorCtx.loading = false;
      if (statusEl) statusEl.classList.add("hidden");
      updatePager();
    }
  }

  async function openBrowse(kind, page = 1) {
    if (browseCtx.loading) return;
    browseCtx.loading = true;
    browseCtx.kind = kind;
    browseCtx.page = page;
    authorCtx = { id: null, name: "", page: 1, hasMore: false, loading: false };
    const labels = { hot: "热门榜", digested: "精华帖" };
    const heading = $("#topics-heading");
    if (heading) heading.textContent = labels[kind] || "标题";

    // immediate feedback so 精华帖 doesn't feel dead
    document.querySelectorAll(".chip-browse").forEach((b) => {
      b.classList.toggle("loading", b.dataset.browse === kind);
    });
    const statusEl = $("#results-status");
    const alreadyOnResults = !views.results.classList.contains("hidden");
    const keepScroll = alreadyOnResults && page > 1;
    if (!alreadyOnResults || page === 1) {
      browseCtx.topics = [];
    }
    renderResults(
      { authors: [], topics: page === 1 ? [] : browseCtx.topics || [], q: labels[kind] || kind },
      { keepScroll, loading: true }
    );
    if (statusEl) {
      statusEl.textContent = `正在加载${labels[kind] || ""}…`;
      statusEl.classList.remove("hidden");
    }
    updatePager();

    try {
      const { ok, data } = await api(
        `/api/browse?kind=${encodeURIComponent(kind)}&page=${page}&pageSize=10`
      );
      if (!ok) {
        alert((data && (data.detail?.error || data.error || data.detail)) || "加载失败");
        if (typeof data?.detail === "object" && data.detail?.error) {
          // already alerted
        }
        return;
      }
      browseCtx.hasMore = !!data.hasMore;
      browseCtx.page = page;
      browseCtx.topics = (data.topics || []).slice();
      renderResults(
        { authors: [], topics: browseCtx.topics, q: labels[kind] || kind },
        { keepScroll }
      );
    } finally {
      browseCtx.loading = false;
      document.querySelectorAll(".chip-browse").forEach((b) => b.classList.remove("loading"));
      if (statusEl) statusEl.classList.add("hidden");
      updatePager();
    }
  }

  function stopPlaylistAudio() {
    try {
      plAudio.pause();
      plAudio.removeAttribute("src");
      plAudio.load();
    } catch (_) {}
    plPlay.textContent = "▶";
  }

  function stopReaderAudio() {
    try {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    } catch (_) {}
    btnPlay.textContent = "▶";
  }

  function resetPlayer() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    btnPlay.textContent = "▶";
    btnPlay.disabled = true;
    seek.value = 0;
    seek.max = 0;
    seek.disabled = true;
    $("#cur").textContent = "0:00";
    $("#dur").textContent = "0:00";
    const pt = $("#player-title");
    if (pt) pt.textContent = "未选择曲目";
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function applyTopicPayload(data) {
    $("#r-empty").classList.add("hidden");
    $("#r-body").classList.remove("hidden");
    $("#r-body").textContent = data.text || "";
    if (data.title) $("#r-title").textContent = data.title;
    if (data.authorName) {
      current.authorName = data.authorName;
      $("#r-meta").textContent = `${data.authorName} · ${current.entityType}/${current.entityId}`;
    }
    btnTts.disabled = false;
    if (data.hasAudio && data.audioUrl) {
      loadAudio(data.audioUrl);
      btnTts.textContent = "播放语音（已缓存）";
    } else {
      btnTts.textContent = "生成/播放语音";
    }
  }


  function setReaderPlaylistNav(on) {
    const prev = $("#btn-prev");
    const next = $("#btn-next");
    if (prev) prev.disabled = !on;
    if (next) next.disabled = !on;
  }

  async function playReaderUrl(url, session) {
    if (!url || session !== playSession) return false;
    stopPlaylistAudio();
    loadAudio(url);
    btnTts.textContent = "播放语音（已缓存）";
    try {
      await audio.play();
      if (session !== playSession) {
        audio.pause();
        return false;
      }
      btnPlay.textContent = "⏸";
      return true;
    } catch (_) {
      btnPlay.textContent = "▶";
      return false;
    }
  }

  async function waitAndPlayItem(item, session) {
    if (item.audioUrl) return playReaderUrl(item.audioUrl, session);
    toast("语音准备中…");
    prepareItem(item).catch(() => {});
    for (let n = 0; n < 30; n++) {
      if (session !== playSession) return false;
      await pollItem(item).catch(() => {});
      if (item.audioUrl) return playReaderUrl(item.audioUrl, session);
      if (item.status === "fail") {
        toast(item.error || "准备失败");
        return false;
      }
      await sleep(1200);
    }
    toast("语音仍在准备，稍后再点标题");
    return false;
  }

  async function playAndOpenFromPlaylist(idx) {
    if (idx < 0 || idx >= playlist.length) return;
    const session = ++playSession;
    const item = playlist[idx];
    fromPlaylist = true;
    plIndex = idx;
    lastViewBeforePlaylist = "playlist";
    stopPlaylistAudio();
    setReaderPlaylistNav(true);
    // open text only — single play path below (avoid double audio.play)
    await openTopic(item, { autoPlay: false, fromPlaylist: true, session });
    if (session !== playSession) return;
    await waitAndPlayItem(item, session);
    if (session === playSession) renderPlaylist();
  }

  async function openTopic(t, opts) {
    opts = opts || {};
    const session = opts.session || ++playSession;
    if (!opts.fromPlaylist) {
      fromPlaylist = false;
      setReaderPlaylistNav(false);
    }
    // never let 待听 dock keep sounding under the reader
    stopPlaylistAudio();
    current = {
      entityType: t.entityType,
      entityId: String(t.entityId),
      title: t.title || "",
      authorName: t.authorName || "",
    };
    resetPlayer();
    $("#r-title").textContent = current.title || "（无标题）";
    const pt = $("#player-title");
    if (pt) pt.textContent = current.title || "（无标题）";
    $("#r-meta").textContent = current.authorName
      ? `${current.authorName} · ${current.entityType}/${current.entityId}`
      : `${current.entityType}/${current.entityId}`;
    $("#r-body").textContent = "";
    $("#r-body").classList.add("hidden");
    $("#r-empty").classList.remove("hidden");
    $("#r-empty").textContent = "正在从生财拉取正文…";
    btnTts.disabled = true;
    btnTts.textContent = "生成/播放语音";
    show("reader");

    const q = `?title=${encodeURIComponent(current.title)}&authorName=${encodeURIComponent(current.authorName)}`;
    const base = `/api/topics/${encodeURIComponent(current.entityType)}/${encodeURIComponent(current.entityId)}`;

    for (let i = 0; i < 3; i++) {
      $("#r-empty").textContent = i === 0 ? "正在从生财拉取正文…" : `重试拉取（${i + 1}/3）…`;
      const { ok, status, data } = await api(base + q);
      if (session !== playSession) return;
      if (ok && status === 200 && data && data.text != null && !data.needFetch) {
        await applyTopicPayload(data);
        if (opts.autoPlay && data.hasAudio && data.audioUrl) {
          await playReaderUrl(data.audioUrl, session);
        }
        return;
      }
      const err = (data && data.error) || `HTTP ${status}`;
      if (status === 502 || status === 503) {
        $("#r-empty").textContent = "拉取失败：" + err;
        if (i < 2) await sleep(1500);
        else return;
        continue;
      }
      $("#r-empty").textContent = "拉取失败：" + err;
      return;
    }
  }

  function loadAudio(url) {
    audio.src = url;
    audio.load();
    applyRateTo(audio);
    btnPlay.disabled = false;
    seek.disabled = false;
  }

  async function ensureTtsAndPlay() {
    if (!current) return;
    stopPlaylistAudio();
    const session = ++playSession;
    btnTts.disabled = true;
    const prev = btnTts.textContent;
    btnTts.textContent = "生成中…";
    try {
      const { ok, data } = await api(
        `/api/topics/${encodeURIComponent(current.entityType)}/${encodeURIComponent(current.entityId)}/tts`,
        { method: "POST" }
      );
      if (!ok || !data.url) {
        alert((data && (data.detail?.error || data.error)) || "语音生成失败");
        btnTts.textContent = prev;
        btnTts.disabled = false;
        return;
      }
      if (session !== playSession) return;
      await playReaderUrl(data.url, session);
      btnTts.textContent = data.cached ? "播放语音（已缓存）" : "播放语音";
    } catch (e) {
      alert("语音请求异常");
      btnTts.textContent = prev;
    } finally {
      btnTts.disabled = false;
    }
  }

  // --- events ---
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    doSearch($("#q").value);
  });
  document.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.browse) {
        openBrowse(btn.dataset.browse, 1);
        return;
      }
      doSearch(btn.dataset.q);
    });
  });
  $("#back-home").addEventListener("click", () => show("home"));
  $("#back-results").addEventListener("click", () => {
    if (fromPlaylist) {
      show("playlist");
      return;
    }
    if (lastResults.topics?.length || lastResults.authors?.length) show("results");
    else show("home");
  });
  $("#btn-add-queue").addEventListener("click", () => {
    if (!current) return;
    addToPlaylist(current);
  });
  $("#btn-playlist").addEventListener("click", () => {
    const visible = Object.entries(views).find(([, el]) => !el.classList.contains("hidden"));
    if (visible && visible[0] !== "playlist") lastViewBeforePlaylist = visible[0];
    show("playlist");
  });
  $("#back-from-playlist").addEventListener("click", () => {
    show(lastViewBeforePlaylist || "home");
  });
  $("#pl-clear").addEventListener("click", clearPlaylist);
  $("#pl-prepare-all").addEventListener("click", () => {
    playlist.forEach((it) => {
      if (it.status !== "ready") prepareItem(it).catch(() => {});
    });
    toast("已触发全部准备");
  });

  btnTts.addEventListener("click", ensureTtsAndPlay);
  btnPlay.addEventListener("click", () => {
    if (audio.paused) {
      stopPlaylistAudio();
      audio.play();
      btnPlay.textContent = "⏸";
    } else {
      audio.pause();
      btnPlay.textContent = "▶";
    }
  });
  btnRate.addEventListener("click", () => cycleRate());
  $("#btn-prev").addEventListener("click", () => {
    if (!fromPlaylist) return;
    if (plIndex > 0) playAndOpenFromPlaylist(plIndex - 1);
    else toast("已经是第一首");
  });
  $("#btn-next").addEventListener("click", () => {
    if (!fromPlaylist) return;
    if (plIndex + 1 < playlist.length) playAndOpenFromPlaylist(plIndex + 1);
    else toast("已经是最后一首");
  });
  audio.addEventListener("timeupdate", () => {
    seek.value = audio.currentTime || 0;
    $("#cur").textContent = fmt(audio.currentTime);
  });
  audio.addEventListener("loadedmetadata", () => {
    seek.max = audio.duration || 0;
    $("#dur").textContent = fmt(audio.duration);
    applyRateTo(audio);
  });
  audio.addEventListener("play", () => {
    applyRateTo(audio);
    btnPlay.textContent = "⏸";
  });
  audio.addEventListener("pause", () => {
    btnPlay.textContent = "▶";
  });
  audio.addEventListener("ended", () => {
    btnPlay.textContent = "▶";
    if (!fromPlaylist) return;
    // sequential next in 待听 (prepare on the fly) so 连播 works
    const next = plIndex + 1;
    if (next < playlist.length) playAndOpenFromPlaylist(next);
    else toast("待听已播完");
  });
  seek.addEventListener("input", () => {
    audio.currentTime = parseFloat(seek.value) || 0;
  });

  // playlist player
  plPlay.addEventListener("click", () => {
    if (!plAudio.src) {
      const i = nextReadyIndex(0, true);
      if (i >= 0) playFromIndex(i, true);
      else toast("暂无可播放项");
      return;
    }
    if (plAudio.paused) {
      stopReaderAudio();
      ++playSession;
      plAudio.play();
      plPlay.textContent = "⏸";
    } else {
      plAudio.pause();
      plPlay.textContent = "▶";
    }
  });
  $("#pl-next").addEventListener("click", () => {
    const i = nextReadyIndex(plIndex < 0 ? -1 : plIndex, false);
    if (i >= 0) playFromIndex(i, true);
    else toast("后面没有可播放项");
  });
  $("#pl-prev").addEventListener("click", () => {
    const i = prevReadyIndex(plIndex < 0 ? playlist.length : plIndex);
    if (i >= 0) playFromIndex(i, false);
    else toast("前面没有可播放项");
  });
  plBtnRate.addEventListener("click", () => cycleRate());
  plAudio.addEventListener("timeupdate", () => {
    plSeek.value = plAudio.currentTime || 0;
    $("#pl-cur").textContent = fmt(plAudio.currentTime);
  });
  plAudio.addEventListener("loadedmetadata", () => {
    plSeek.max = plAudio.duration || 0;
    $("#pl-dur").textContent = fmt(plAudio.duration);
    applyRateTo(plAudio);
  });
  plAudio.addEventListener("play", () => {
    applyRateTo(plAudio);
    plPlay.textContent = "⏸";
  });
  plAudio.addEventListener("pause", () => {
    plPlay.textContent = "▶";
  });
  plAudio.addEventListener("ended", () => {
    plPlay.textContent = "▶";
    const i = nextReadyIndex(plIndex, false);
    if (i >= 0) playFromIndex(i, true);
  });
  plSeek.addEventListener("input", () => {
    plAudio.currentTime = parseFloat(plSeek.value) || 0;
  });

  $("#pager-prev").addEventListener("click", () => {
    if (browseCtx.kind) {
      if (browseCtx.page > 1) openBrowse(browseCtx.kind, browseCtx.page - 1);
      return;
    }
    if (authorCtx.id && authorCtx.page > 1) {
      openAuthor(authorCtx.id, authorCtx.name, authorCtx.page - 1);
    }
  });
  $("#pager-next").addEventListener("click", () => {
    if (browseCtx.kind) {
      if (browseCtx.hasMore) openBrowse(browseCtx.kind, browseCtx.page + 1);
      return;
    }
    if (authorCtx.id && authorCtx.hasMore) {
      openAuthor(authorCtx.id, authorCtx.name, authorCtx.page + 1);
    }
  });

  // init rate
  setSavedRate(getSavedRate());

  updatePlaylistBadge();
  // warm-poll pending items even on home (light)
  if (playlist.some((x) => x.status !== "ready" && x.status !== "fail")) {
    pollAll();
  }
  show("home");
})();
