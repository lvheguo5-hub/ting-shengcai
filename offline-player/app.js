(function () {
  "use strict";

  const STORAGE_KEY = "yiren-audiobook-player-v2";
  const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];

  const TRACKS = [
    {
      file: "01_心力与AI三品质_短帖.mp3",
      title:
        "有人问带团队赚钱怎么保持心力？我当场踢走影响心情的那种人 / AI时代，我觉得这三个品质能帮你成为超级潜力股",
    },
    {
      file: "02_超级标预告与跑通内容_短帖.mp3",
      title:
        "明天应该会发的超级标，是全网首个百万级赚钱机会 / 还是建议要自己先跑通内容，这是成本最低的方式",
    },
    {
      file: "03_负罪感问答.mp3",
      title: "别因做得少有负罪感，负罪感比做得少更坏",
    },
    {
      file: "04_启动困难症.mp3",
      title: "聊聊我的启动困难症，还有几个亲测有效的笨办法",
    },
    {
      file: "05_远程连接100概念.mp3",
      title: "远程连接与开发环境 100 个必备概念 —— 写给不懂技术原理的人",
    },
    {
      file: "06_AI团队与超级个体.mp3",
      title: "生财圈主的创业实战感悟：AI团队、超级个体与赚钱选择",
    },
    {
      file: "07_起步与AI三判断.mp3",
      title:
        "别焦虑起步难，多数大佬都是从赚第一块钱起步的 / 我对AI时代的3个核心判断：10人小团队、老板当开发者、抓信息处理",
    },
    {
      file: "08_非洲AMA.mp3",
      title: "生财销冠团非洲AMA分享：八位数学费、投资心得与创业思路",
    },
    {
      file: "09_自己当客户.mp3",
      title: "没项目没客户？先拿自己当第一个AI客户就够了",
    },
    {
      file: "10_别藏AI能力.mp3",
      title: "别让你的AI能力藏着，主动展示才能抓住机会",
    },
    {
      file: "11_mini航海手册思考.mp3",
      title: "开放近期所有mini航海手册，也想和大家聊聊mini航海后续的思考",
    },
    {
      file: "12_mini航海道歉.mp3",
      title: "针对mini航海报名的吐槽，我和团队道歉，后续努力给大家更好的体验",
    },
  ];

  const audio = document.getElementById("audio");
  const viewHome = document.getElementById("view-home");
  const viewPlaylist = document.getElementById("view-playlist");
  const trackList = document.getElementById("track-list");
  const playerBar = document.getElementById("player-bar");
  const playerTitle = document.getElementById("player-title");
  const seek = document.getElementById("seek");
  const timeCurrent = document.getElementById("time-current");
  const timeDuration = document.getElementById("time-duration");
  const btnPlay = document.getElementById("btn-play");
  const btnPrev = document.getElementById("btn-prev");
  const btnNext = document.getElementById("btn-next");
  const btnBack = document.getElementById("btn-back");
  const btnRate = document.getElementById("btn-rate");
  const searchInput = document.getElementById("search-input");
  const btnClearSearch = document.getElementById("btn-clear-search");
  const searchEmpty = document.getElementById("search-empty");

  let index = 0;
  let seeking = false;
  let resumeTime = 0;
  let readyToResume = false;
  let rate = 1;
  let query = "";

  function formatTime(sec) {
    if (!isFinite(sec) || sec < 0) return "0:00";
    const s = Math.floor(sec);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m + ":" + String(r).padStart(2, "0");
  }

  function formatRate(r) {
    return Number.isInteger(r) ? r + "x" : String(r) + "x";
  }

  function loadState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (typeof data.index === "number" && data.index >= 0 && data.index < TRACKS.length) {
        index = data.index;
      }
      if (typeof data.time === "number" && data.time >= 0) {
        resumeTime = data.time;
      }
      if (typeof data.rate === "number" && RATES.indexOf(data.rate) !== -1) {
        rate = data.rate;
      }
    } catch (_) {
      /* ignore */
    }
  }

  function saveState() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          index: index,
          time: audio.currentTime || 0,
          rate: rate,
        })
      );
    } catch (_) {
      /* ignore */
    }
  }

  function applyRate() {
    audio.playbackRate = rate;
    btnRate.textContent = formatRate(rate);
    btnRate.setAttribute("aria-label", "倍速 " + formatRate(rate));
  }

  function trackSrc(i) {
    return "audio/" + encodeURIComponent(TRACKS[i].file);
  }

  function filteredIndexes() {
    const q = query.trim().toLowerCase();
    if (!q) {
      return TRACKS.map(function (_, i) {
        return i;
      });
    }
    return TRACKS.reduce(function (acc, t, i) {
      if (t.title.toLowerCase().indexOf(q) !== -1) acc.push(i);
      return acc;
    }, []);
  }

  function renderList() {
    const ids = filteredIndexes();
    trackList.innerHTML = "";
    searchEmpty.classList.toggle("hidden", ids.length > 0);
    ids.forEach(function (i) {
      const t = TRACKS[i];
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "track-row" + (i === index ? " playing" : "");
      btn.setAttribute("data-index", String(i));
      btn.innerHTML =
        '<span class="track-num">' +
        String(i + 1).padStart(2, "0") +
        "</span>" +
        '<span class="track-title">' +
        t.title +
        "</span>" +
        '<span class="track-eq" aria-hidden="true">♪</span>';
      btn.addEventListener("click", function () {
        playAt(i, 0, true);
      });
      li.appendChild(btn);
      trackList.appendChild(li);
    });
  }

  function highlightList() {
    const rows = trackList.querySelectorAll(".track-row");
    rows.forEach(function (row) {
      const i = Number(row.getAttribute("data-index"));
      row.classList.toggle("playing", i === index);
    });
  }

  function updatePlayButton() {
    const playing = !audio.paused;
    btnPlay.textContent = playing ? "⏸" : "▶";
    btnPlay.setAttribute("aria-label", playing ? "暂停" : "播放");
  }

  function showPlayer() {
    playerBar.hidden = false;
  }

  function loadTrack(i, autoplay) {
    index = ((i % TRACKS.length) + TRACKS.length) % TRACKS.length;
    const t = TRACKS[index];
    playerTitle.textContent = t.title;
    playerTitle.title = t.title;
    audio.src = trackSrc(index);
    audio.load();
    applyRate();
    highlightList();
    showPlayer();
    saveState();
    if (autoplay) {
      const p = audio.play();
      if (p && typeof p.catch === "function") {
        p.catch(function () {
          updatePlayButton();
        });
      }
    }
    updatePlayButton();
  }

  function playAt(i, time, autoplay) {
    resumeTime = time || 0;
    readyToResume = resumeTime > 0;
    loadTrack(i, autoplay);
  }

  function showHome() {
    viewHome.classList.remove("hidden");
    viewPlaylist.classList.add("hidden");
  }

  function showPlaylist() {
    viewHome.classList.add("hidden");
    viewPlaylist.classList.remove("hidden");
    renderList();
  }

  document.querySelector('.author-card[data-author="yiren"]').addEventListener("click", showPlaylist);
  btnBack.addEventListener("click", showHome);

  searchInput.addEventListener("input", function () {
    query = searchInput.value || "";
    btnClearSearch.hidden = !query.trim();
    renderList();
  });
  btnClearSearch.addEventListener("click", function () {
    searchInput.value = "";
    query = "";
    btnClearSearch.hidden = true;
    renderList();
    searchInput.focus();
  });

  btnRate.addEventListener("click", function () {
    const i = RATES.indexOf(rate);
    rate = RATES[(i + 1) % RATES.length];
    applyRate();
    saveState();
  });

  btnPlay.addEventListener("click", function () {
    if (!audio.src) {
      playAt(index, resumeTime, true);
      return;
    }
    if (audio.paused) {
      audio.play().catch(function () {});
    } else {
      audio.pause();
    }
  });

  btnPrev.addEventListener("click", function () {
    if (audio.currentTime > 3) {
      audio.currentTime = 0;
      saveState();
      return;
    }
    playAt(index - 1, 0, true);
  });

  btnNext.addEventListener("click", function () {
    playAt(index + 1, 0, true);
  });

  audio.addEventListener("play", updatePlayButton);
  audio.addEventListener("pause", function () {
    updatePlayButton();
    saveState();
  });

  audio.addEventListener("ended", function () {
    playAt(index + 1, 0, true);
  });

  audio.addEventListener("ratechange", function () {
    if (audio.playbackRate !== rate) {
      audio.playbackRate = rate;
    }
  });

  audio.addEventListener("loadedmetadata", function () {
    seek.max = audio.duration || 0;
    timeDuration.textContent = formatTime(audio.duration);
    applyRate();
    if (readyToResume && resumeTime > 0 && resumeTime < audio.duration) {
      audio.currentTime = resumeTime;
      readyToResume = false;
    }
  });

  audio.addEventListener("timeupdate", function () {
    if (seeking) return;
    seek.value = audio.currentTime || 0;
    timeCurrent.textContent = formatTime(audio.currentTime);
    if (Math.floor(audio.currentTime) % 2 === 0) {
      saveState();
    }
  });

  seek.addEventListener("pointerdown", function () {
    seeking = true;
  });
  seek.addEventListener("pointerup", function () {
    seeking = false;
  });
  seek.addEventListener("change", function () {
    audio.currentTime = Number(seek.value);
    seeking = false;
    saveState();
  });
  seek.addEventListener("input", function () {
    timeCurrent.textContent = formatTime(Number(seek.value));
  });

  window.addEventListener("beforeunload", saveState);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) saveState();
  });

  loadState();
  applyRate();
  renderList();
  playerTitle.textContent = TRACKS[index].title;
  playerTitle.title = TRACKS[index].title;
  showPlayer();
  readyToResume = resumeTime > 0;
  audio.src = trackSrc(index);
  audio.load();
})();
