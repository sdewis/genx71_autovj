// GenX71 AutoVJ PWA Client

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

let currentPreviewPostId = null;
let pollTimer = null;

// --- Navigation ---
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tab;
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${target}`).classList.add("active");

    if (target === "posts") loadPosts();
    if (target === "tracks") loadTracks();
    if (target === "admin") { loadSettings(); loadBlacklist(); }
  });
});

// --- Status & Polling ---
async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();

    const dPill = document.getElementById("daemon-pill");
    dPill.textContent = data.daemon_running ? "Daemon: ON" : "Daemon: OFF";
    dPill.className = `pill ${data.daemon_running ? "pill-ok" : "pill-off"}`;

    const cPill = document.getElementById("cookie-pill");
    cPill.textContent = data.cookie_health.healthy ? "TikTok: Ready" : "TikTok: Need Cookies";
    cPill.className = `pill ${data.cookie_health.healthy ? "pill-ok" : "pill-warn"}`;

    const nextEl = document.getElementById("stat-next-run");
    if (data.next_scheduled_run) {
      const dt = new Date(data.next_scheduled_run);
      nextEl.textContent = dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } else {
      nextEl.textContent = "Paused";
    }

    document.getElementById("stat-avail").textContent = `${data.stats.available_tracks}/${data.stats.total_tracks}`;
    document.getElementById("stat-wildcards").textContent = data.stats.available_wildcards;
    document.getElementById("stat-posted").textContent = `${data.stats.published_count}/${data.stats.total_posts}`;

    if (data.generation && data.generation.running && !pollTimer) {
      startProgressPolling();
    }
  } catch (e) {
    console.error("Status refresh failed", e);
  }
}

async function triggerGeneration(opts = {}) {
  const eraAppropriate = document.getElementById("gen-era-appropriate").checked;
  const template = document.getElementById("gen-template").value;
  const payload = {
    template: eraAppropriate ? null : template,
    era_appropriate: eraAppropriate,
    ...opts
  };

  const res = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!res.ok) {
    const err = await res.json();
    alert(err.detail || "Failed to start generation");
    return;
  }
  startProgressPolling();
}

function startProgressPolling() {
  const box = document.getElementById("gen-progress-box");
  const fill = document.getElementById("gen-progress-fill");
  const msg = document.getElementById("gen-progress-msg");
  box.classList.remove("hidden");

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const res = await fetch("/api/generate/status");
    const st = await res.json();
    fill.style.width = `${st.progress}%`;
    msg.textContent = st.message;

    if (!st.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      refreshStatus();
      if (st.last_post) {
        showPreview(st.last_post);
      } else if (st.error) {
        alert("Generation Error: " + st.error);
      }
    }
  }, 1000);
}

function showPreview(post) {
  currentPreviewPostId = post.id;
  const card = document.getElementById("preview-card");
  card.classList.remove("hidden");
  document.getElementById("preview-title").textContent = `${post.artist} - ${post.title}`;
  document.getElementById("preview-badge").textContent = post.chart_info || post.year;

  const videoUrl = `/output/${post.video_path.split("/").pop()}`;
  const vid = document.getElementById("preview-video");
  vid.src = videoUrl;
  document.getElementById("btn-download-preview").href = videoUrl;
}

document.getElementById("gen-era-appropriate").addEventListener("change", (e) => {
  document.getElementById("gen-template").disabled = e.target.checked;
});

document.getElementById("btn-gen-random").addEventListener("click", () => triggerGeneration({ force_wildcard: false }));
document.getElementById("btn-gen-wildcard").addEventListener("click", () => triggerGeneration({ force_wildcard: true }));

document.getElementById("btn-publish-preview").addEventListener("click", async () => {
  if (!currentPreviewPostId) return;
  await publishPost(currentPreviewPostId);
});

async function publishPost(postId) {
  const res = await fetch(`/api/posts/${postId}/publish`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) {
    alert("TikTok Upload Error: " + (data.detail || "Unknown error"));
  } else {
    alert(data.message);
    loadPosts();
    refreshStatus();
  }
}

// --- Posts History ---
async function loadPosts() {
  const res = await fetch("/api/posts");
  const data = await res.json();
  const list = document.getElementById("posts-list");
  list.innerHTML = "";

  if (!data.posts.length) {
    list.innerHTML = '<p class="muted">No videos generated yet. Head to Studio to create your first GenX hit!</p>';
    return;
  }

  data.posts.forEach((p) => {
    const el = document.createElement("div");
    el.className = "item-card";
    const statusCls = p.status === "posted" ? "badge-posted" : p.status === "failed" ? "badge-failed" : "";
    el.innerHTML = `
      <div class="item-info">
        <div class="item-title">${p.artist} — ${p.title}</div>
        <div class="item-sub">${p.chart_info} • Template: ${p.template_used}</div>
        <span class="badge ${statusCls}">${p.status.toUpperCase()}</span>
      </div>
      <div class="item-actions">
        ${p.video_url ? `<button class="btn-small" onclick="previewFromList('${p.video_url}', '${p.artist.replace(/'/g, "\\'")}', '${p.title.replace(/'/g, "\\'")}', '${p.chart_info}', ${p.id})">▶️</button>` : ""}
        <button class="btn-small" onclick="publishPost(${p.id})">🚀 Post</button>
      </div>
    `;
    list.appendChild(el);
  });
}

window.previewFromList = (url, artist, title, badge, id) => {
  currentPreviewPostId = id;
  document.querySelector('[data-tab="studio"]').click();
  const card = document.getElementById("preview-card");
  card.classList.remove("hidden");
  document.getElementById("preview-title").textContent = `${artist} - ${title}`;
  document.getElementById("preview-badge").textContent = badge;
  document.getElementById("preview-video").src = url;
  document.getElementById("btn-download-preview").href = url;
};

document.getElementById("btn-refresh-posts").addEventListener("click", loadPosts);

// --- Tracks Catalog ---
async function loadTracks(query = "") {
  const res = await fetch(`/api/tracks?q=${encodeURIComponent(query)}`);
  const data = await res.json();
  const list = document.getElementById("tracks-list");
  list.innerHTML = "";

  data.tracks.forEach((t) => {
    const el = document.createElement("div");
    el.className = "item-card";
    const badges = [];
    if (t.is_wildcard) badges.push('<span class="badge badge-wildcard">WILDCARD</span>');
    if (t.posted) badges.push('<span class="badge badge-posted">POSTED</span>');
    if (t.blacklisted) badges.push('<span class="badge badge-failed">BLOCKED</span>');

    el.innerHTML = `
      <div class="item-info">
        <div class="item-title">${t.artist} — ${t.title}</div>
        <div class="item-sub">${t.year} • ${t.chart_region} #${t.chart_peak || "?"} • ${t.genre}</div>
        <div style="margin-top:4px; display:flex; gap:6px;">${badges.join("")}</div>
      </div>
      <div class="item-actions">
        <button class="btn-small" onclick="renderSpecificTrack(${t.id})">🎬 Render</button>
        <button class="btn-small" onclick="blacklistTrack('${t.artist.replace(/'/g, "\\'")}', '${t.title.replace(/'/g, "\\'")}')">🚫</button>
      </div>
    `;
    list.appendChild(el);
  });
}

window.renderSpecificTrack = (trackId) => {
  document.querySelector('[data-tab="studio"]').click();
  triggerGeneration({ track_id: trackId });
};

window.blacklistTrack = async (artist, title) => {
  if (!confirm(`Blacklist ${artist} - ${title}?`)) return;
  await fetch("/api/blacklist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ artist, title, reason: "Admin UI" })
  });
  loadTracks(document.getElementById("track-search").value);
  refreshStatus();
};

document.getElementById("track-search").addEventListener("input", (e) => {
  loadTracks(e.target.value);
});

document.getElementById("add-track-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    artist: document.getElementById("new-artist").value,
    title: document.getElementById("new-title").value,
    year: parseInt(document.getElementById("new-year").value, 10),
    chart_peak: document.getElementById("new-peak").value ? parseInt(document.getElementById("new-peak").value, 10) : null,
    is_wildcard: document.getElementById("new-wildcard").checked ? 1 : 0
  };
  await fetch("/api/tracks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  e.target.reset();
  loadTracks();
  refreshStatus();
});

// --- Admin Settings & Cookies ---
async function loadSettings() {
  const res = await fetch("/api/settings");
  const { settings } = await res.json();

  document.getElementById("set-sched-enabled").checked = settings.schedule_enabled === "true";
  document.getElementById("set-post-times").value = settings.daily_post_times || "12:30,19:15";
  document.getElementById("set-jitter").value = settings.jitter_minutes || "30";
  document.getElementById("set-wildcard").value = settings.wildcard_chance || "25";
  document.getElementById("wildcard-val").textContent = settings.wildcard_chance || "25";
  document.getElementById("set-template").value = settings.default_template || "vinyl";
  const eraAppropriate = settings.era_appropriate_template === "true";
  document.getElementById("set-era-appropriate").checked = eraAppropriate;
  document.getElementById("set-template").disabled = eraAppropriate;
  document.getElementById("set-wm-opacity").value = settings.watermark_opacity || "0.40";
  document.getElementById("wm-val").textContent = settings.watermark_opacity || "0.40";
  document.getElementById("set-auto-publish").checked = settings.auto_publish === "true";
}

document.getElementById("set-era-appropriate").addEventListener("change", (e) => {
  document.getElementById("set-template").disabled = e.target.checked;
});

document.getElementById("set-wildcard").addEventListener("input", (e) => {
  document.getElementById("wildcard-val").textContent = e.target.value;
});

document.getElementById("set-wm-opacity").addEventListener("input", (e) => {
  document.getElementById("wm-val").textContent = e.target.value;
});

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    settings: {
      schedule_enabled: document.getElementById("set-sched-enabled").checked ? "true" : "false",
      daily_post_times: document.getElementById("set-post-times").value,
      jitter_minutes: document.getElementById("set-jitter").value,
      wildcard_chance: document.getElementById("set-wildcard").value,
      default_template: document.getElementById("set-template").value,
      era_appropriate_template: document.getElementById("set-era-appropriate").checked ? "true" : "false",
      watermark_opacity: document.getElementById("set-wm-opacity").value,
      auto_publish: document.getElementById("set-auto-publish").checked ? "true" : "false"
    }
  };
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  alert("Schedule & settings saved!");
  refreshStatus();
});

function setCookieStatus(message, level = "muted") {
  const el = document.getElementById("cookie-status-msg");
  el.textContent = message;
  el.className = level === "ok" ? "status-ok" : level === "warn" ? "status-warn" : level === "err" ? "status-err" : "muted";
}

async function importCookieFile(file) {
  const fileNameEl = document.getElementById("cookies-file-name");
  const cookiesInput = document.getElementById("cookies-input");
  if (!file) return;

  if (!file.name.toLowerCase().endsWith(".json")) {
    setCookieStatus("Only .json cookie files are supported.", "err");
    return;
  }

  const maxBytes = 2 * 1024 * 1024;
  if (file.size > maxBytes) {
    setCookieStatus("Cookie file is too large (max 2 MB).", "err");
    return;
  }

  const kb = (file.size / 1024).toFixed(1);
  fileNameEl.textContent = `${file.name} (${kb} KB)`;
  setCookieStatus("Reading and validating cookie file...", "muted");

  try {
    const content = await file.text();
    cookiesInput.value = content;

    let res = await fetch("/api/cookies/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, content })
    });

    // Fallback if backend has not been restarted yet for /api/cookies/import
    if (res.status === 404) {
      res = await fetch("/api/cookies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookies_json: content })
      });
    }

    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      setCookieStatus(data.detail || `Failed to import cookie file (HTTP ${res.status}).`, "err");
      return;
    }

    const health = data.health || { healthy: Boolean(data.ok), reason: "Cookies saved." };
    const level = health.healthy ? "ok" : "warn";
    const countPart = typeof data.imported_count === "number"
      ? `Imported ${data.imported_count} TikTok cookie(s) from ${data.filename || file.name}. `
      : `Loaded ${file.name}. `;
    setCookieStatus(`${countPart}${health.reason}`, level);
    refreshStatus();
  } catch (err) {
    setCookieStatus(`Failed to read or upload cookie file: ${err.message || err}`, "err");
  }
}

const cookiesDropzone = document.getElementById("cookies-dropzone");
const cookiesFileInput = document.getElementById("cookies-file-input");

cookiesDropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    cookiesFileInput.click();
  }
});

cookiesFileInput.addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (file) {
    importCookieFile(file);
  }
  cookiesFileInput.value = "";
});

cookiesDropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  cookiesDropzone.classList.add("drag-over");
});

cookiesDropzone.addEventListener("dragleave", () => {
  cookiesDropzone.classList.remove("drag-over");
});

cookiesDropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  cookiesDropzone.classList.remove("drag-over");
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) {
    importCookieFile(file);
  }
});

document.getElementById("btn-save-cookies").addEventListener("click", async () => {
  const cookies_json = document.getElementById("cookies-input").value;
  try {
    const res = await fetch("/api/cookies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies_json })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setCookieStatus(data.detail || `Invalid cookie data (HTTP ${res.status}).`, "err");
      return;
    }
    const health = data.health || { healthy: Boolean(data.ok), reason: "Cookies saved." };
    setCookieStatus(health.reason, health.healthy ? "ok" : "warn");
    refreshStatus();
  } catch (err) {
    setCookieStatus(`Failed to save cookies: ${err.message || err}`, "err");
  }
});

async function loadBlacklist() {
  const res = await fetch("/api/blacklist");
  const data = await res.json();
  const box = document.getElementById("blacklist-items");
  box.innerHTML = "";
  if (!data.blacklist.length) {
    box.innerHTML = '<p class="muted">No blacklisted tracks.</p>';
    return;
  }
  data.blacklist.forEach((b) => {
    const el = document.createElement("div");
    el.className = "item-card";
    el.innerHTML = `
      <div class="item-info">
        <div class="item-title">${b.artist} ${b.title ? "— " + b.title : "(All Tracks)"}</div>
      </div>
      <button class="btn-small" onclick="unblock(${b.id})">Restore</button>
    `;
    box.appendChild(el);
  });
}

window.unblock = async (id) => {
  await fetch(`/api/blacklist/${id}`, { method: "DELETE" });
  loadBlacklist();
  refreshStatus();
};

// Initial load
refreshStatus();
setInterval(refreshStatus, 15000);
