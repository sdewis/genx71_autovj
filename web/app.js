// GenX71 AutoVJ PWA Client

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

let currentPreviewPostId = null;
let pollTimer = null;
let publishPollTimer = null;
let settingsReady = null;

// --- Navigation ---
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tab;
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${target}`).classList.add("active");

    if (target === "queue") loadQueue();
    if (target === "posts") loadPosts();
    if (target === "tracks") loadTracks();
    if (target === "admin") { loadSettings(); loadBlacklist(); loadVoiceProfiles(); }
  });
});

// --- Status & Polling ---
function updateQueueNavBadge(count) {
  const badge = document.getElementById("queue-nav-badge");
  if (!badge) return;
  const n = Number(count) || 0;
  badge.textContent = String(n);
  badge.classList.toggle("hidden", n <= 0);
}

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
    updateQueueNavBadge(data.stats.queued_count);

    if (data.generation && data.generation.running && !pollTimer) {
      startProgressPolling();
    }
    if (data.publish && data.publish.running && !publishPollTimer) {
      startPublishPolling();
    }
  } catch (e) {
    console.error("Status refresh failed", e);
  }
}

async function triggerGeneration(opts = {}) {
  if (settingsReady) {
    await settingsReady.catch(() => {});
  }
  const eraAppropriate = document.getElementById("gen-era-appropriate").checked;
  const autoQueue = document.getElementById("gen-auto-queue").checked;
  const ttsEnabled = Boolean(document.getElementById("gen-tts-enabled")?.checked);
  const voiceRegion = document.getElementById("gen-voice-region")?.value || null;
  const customHookText = (document.getElementById("gen-custom-hook")?.value || "").trim() || null;
  const template = document.getElementById("gen-template").value;
  const payload = {
    template: eraAppropriate ? null : template,
    era_appropriate: eraAppropriate,
    auto_queue: autoQueue,
    tts_enabled: ttsEnabled,
    voice_region: ttsEnabled ? voiceRegion : null,
    custom_hook_text: ttsEnabled ? customHookText : null,
    ...opts
  };

  const res = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.detail || "Failed to start generation");
    return;
  }

  if (data.queued_tts && data.post) {
    const customInput = document.getElementById("gen-custom-hook");
    if (customInput) customInput.value = "";
    const box = document.getElementById("gen-progress-box");
    const fill = document.getElementById("gen-progress-fill");
    const msg = document.getElementById("gen-progress-msg");
    if (box && fill && msg) {
      box.classList.remove("hidden");
      fill.style.width = "15%";
      msg.textContent = `🎙️ Queued "${data.post.artist} — ${data.post.title}" as TTS pending (${(data.post.voice_region || "uk").toUpperCase()} voice). Track progress in History.`;
    }
    refreshStatus();
    loadPosts();
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
        if (st.last_post.status === "scheduled") {
          loadQueue();
        }
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

function syncTemplateControls(template, eraAppropriate) {
  const tpl = template || "vinyl";
  const era = Boolean(eraAppropriate);
  ["gen-template", "set-template"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.value = tpl;
      el.disabled = era;
    }
  });
  ["gen-era-appropriate", "set-era-appropriate"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.checked = era;
  });
}

async function persistTemplateControls(template, eraAppropriate) {
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        settings: {
          default_template: template || "vinyl",
          era_appropriate_template: eraAppropriate ? "true" : "false",
        }
      })
    });
  } catch (e) {
    console.error("Failed to save template settings", e);
  }
}

document.getElementById("gen-era-appropriate").addEventListener("change", (e) => {
  const tpl = document.getElementById("gen-template").value;
  syncTemplateControls(tpl, e.target.checked);
  persistTemplateControls(tpl, e.target.checked);
});

document.getElementById("gen-template").addEventListener("change", (e) => {
  const era = document.getElementById("gen-era-appropriate").checked;
  syncTemplateControls(e.target.value, era);
  persistTemplateControls(e.target.value, era);
});

function syncAutoQueueControls(autoQueue) {
  const checked = Boolean(autoQueue);
  ["gen-auto-queue", "set-auto-queue"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.checked = checked;
  });
}

async function persistAutoQueueSetting(autoQueue) {
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        settings: {
          auto_queue_on_generate: autoQueue ? "true" : "false",
        }
      })
    });
  } catch (e) {
    console.error("Failed to save auto-queue setting", e);
  }
}

document.getElementById("gen-auto-queue").addEventListener("change", (e) => {
  syncAutoQueueControls(e.target.checked);
  persistAutoQueueSetting(e.target.checked);
});

function syncTtsEnabledControls(enabled) {
  const checked = Boolean(enabled);
  ["gen-tts-enabled", "set-tts-enabled"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.checked = checked;
  });
  const optsBox = document.getElementById("gen-tts-options");
  if (optsBox) optsBox.classList.toggle("hidden", !checked);
}

async function persistTtsEnabledSetting(enabled) {
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        settings: { tts_enabled: enabled ? "true" : "false" }
      })
    });
  } catch (e) {
    console.error("Failed to save TTS enabled setting", e);
  }
}

document.getElementById("gen-tts-enabled")?.addEventListener("change", (e) => {
  syncTtsEnabledControls(e.target.checked);
  persistTtsEnabledSetting(e.target.checked);
});

document.getElementById("btn-gen-random").addEventListener("click", () => triggerGeneration({ force_wildcard: false }));
document.getElementById("btn-gen-wildcard").addEventListener("click", () => triggerGeneration({ force_wildcard: true }));

document.getElementById("btn-publish-preview").addEventListener("click", async () => {
  if (!currentPreviewPostId) return;
  await publishPost(currentPreviewPostId);
});

document.getElementById("btn-queue-preview").addEventListener("click", async () => {
  if (!currentPreviewPostId) return;
  await queuePostForNext(currentPreviewPostId);
});

function setPublishButtonsDisabled(disabled) {
  const previewBtn = document.getElementById("btn-publish-preview");
  if (previewBtn) previewBtn.disabled = disabled;
  document.querySelectorAll(".btn-publish-post").forEach((btn) => {
    btn.disabled = disabled;
  });
}

function renderPublishProgress(progress, message) {
  const pct = Math.max(0, Math.min(100, Number(progress) || 0));
  const pairs = [
    ["publish-preview-progress-box", "publish-preview-progress-fill", "publish-preview-progress-msg"],
    ["publish-queue-progress-box", "publish-queue-progress-fill", "publish-queue-progress-msg"],
    ["publish-posts-progress-box", "publish-posts-progress-fill", "publish-posts-progress-msg"],
  ];
  pairs.forEach(([boxId, fillId, msgId]) => {
    const box = document.getElementById(boxId);
    const fill = document.getElementById(fillId);
    const msg = document.getElementById(msgId);
    if (!box || !fill || !msg) return;
    box.classList.remove("hidden");
    fill.style.width = `${pct}%`;
    msg.textContent = message || "Uploading to TikTok...";
  });
}

function startPublishPolling() {
  setPublishButtonsDisabled(true);
  renderPublishProgress(5, "Starting TikTok upload...");

  if (publishPollTimer) clearInterval(publishPollTimer);
  publishPollTimer = setInterval(async () => {
    try {
      const res = await fetch("/api/publish/status");
      const st = await res.json();
      const prefix = st.label ? `${st.label}: ` : "";
      renderPublishProgress(st.progress, `${prefix}${st.message}`);

      if (!st.running) {
        clearInterval(publishPollTimer);
        publishPollTimer = null;
        setPublishButtonsDisabled(false);
        loadPosts();
        loadQueue();
        refreshStatus();
        if (st.error) {
          alert("TikTok Upload Error: " + st.error);
        }
      }
    } catch (e) {
      console.error("Publish status poll failed", e);
    }
  }, 1000);
}

async function publishPost(postId) {
  try {
    const res = await fetch(`/api/posts/${postId}/publish`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("TikTok Upload Error: " + (data.detail || `HTTP ${res.status}`));
      return;
    }
    startPublishPolling();
  } catch (err) {
    alert("TikTok Upload Error: " + (err.message || err));
  }
}

// --- Scheduled Post Queue ---
let currentQueueItems = [];
let draggedQueuePostId = null;

async function queuePostForNext(postId, front = false) {
  try {
    const res = await fetch(`/api/posts/${postId}/queue?front=${front ? "true" : "false"}`, {
      method: "POST",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Queue Error: " + (data.detail || `HTTP ${res.status}`));
      return;
    }
    updateQueueNavBadge(data.queue_length);
    loadPosts();
    loadQueue();
    refreshStatus();
    alert(`Queued at position #${data.queue_position} for the next scheduled post.`);
  } catch (err) {
    alert("Queue Error: " + (err.message || err));
  }
}
window.queuePostForNext = queuePostForNext;

function renderQueueList(items) {
  currentQueueItems = items || [];
  updateQueueNavBadge(currentQueueItems.length);

  const clearBtn = document.getElementById("btn-clear-queue");
  if (clearBtn) clearBtn.classList.toggle("hidden", currentQueueItems.length === 0);

  const list = document.getElementById("queue-list");
  if (!list) return;
  list.innerHTML = "";

  if (!currentQueueItems.length) {
    list.innerHTML = '<p class="muted">The queue is empty. Use "Queue for Next Post" in Studio or "📅 Queue" in History to schedule rendered videos (otherwise the daemon auto-generates a track at the next scheduled slot).</p>';
    return;
  }

  currentQueueItems.forEach((p, idx) => {
    const el = document.createElement("div");
    el.className = "item-card queue-card";
    el.draggable = true;
    el.dataset.postId = String(p.id);

    const isFirst = idx === 0;
    const isLast = idx === currentQueueItems.length - 1;
    const safeArtist = (p.artist || "").replace(/'/g, "\\'");
    const safeTitle = (p.title || "").replace(/'/g, "\\'");
    const safeBadge = (p.chart_info || "").replace(/'/g, "\\'");

    el.innerHTML = `
      <span class="queue-pos ${isFirst ? "next-up" : ""}">#${idx + 1}</span>
      <div class="item-info">
        <div class="item-title">${p.artist} — ${p.title}</div>
        <div class="item-sub">${p.chart_info || p.year} • Template: ${p.template_used}</div>
        ${isFirst ? '<span class="badge badge-queued">NEXT UP</span>' : '<span class="badge">QUEUED</span>'}
      </div>
      <div class="item-actions">
        <button class="btn-small" title="Move up" ${isFirst ? "disabled" : ""} onclick="moveQueueItem(${idx}, -1)">⬆️</button>
        <button class="btn-small" title="Move down" ${isLast ? "disabled" : ""} onclick="moveQueueItem(${idx}, 1)">⬇️</button>
        ${p.video_url ? `<button class="btn-small" title="Preview" onclick="previewFromList('${p.video_url}', '${safeArtist}', '${safeTitle}', '${safeBadge}', ${p.id})">▶️</button>` : ""}
        <button class="btn-small btn-publish-post" title="Post now" ${publishPollTimer ? "disabled" : ""} onclick="publishPost(${p.id})">🚀</button>
        <button class="btn-small btn-danger-outline" title="Remove from queue" onclick="removeFromQueue(${p.id})">🗑️</button>
      </div>
    `;

    el.addEventListener("dragstart", (e) => {
      draggedQueuePostId = p.id;
      el.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", String(p.id));
      }
    });
    el.addEventListener("dragend", () => {
      draggedQueuePostId = null;
      el.classList.remove("dragging");
      list.querySelectorAll(".drag-over").forEach((c) => c.classList.remove("drag-over"));
    });
    el.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (draggedQueuePostId !== null && draggedQueuePostId !== p.id) {
        el.classList.add("drag-over");
      }
    });
    el.addEventListener("dragleave", () => {
      el.classList.remove("drag-over");
    });
    el.addEventListener("drop", async (e) => {
      e.preventDefault();
      el.classList.remove("drag-over");
      if (draggedQueuePostId === null || draggedQueuePostId === p.id) return;
      const ids = currentQueueItems.map((item) => item.id);
      const fromIdx = ids.indexOf(draggedQueuePostId);
      const toIdx = ids.indexOf(p.id);
      if (fromIdx === -1 || toIdx === -1) return;
      ids.splice(fromIdx, 1);
      ids.splice(toIdx, 0, draggedQueuePostId);
      await saveQueueOrder(ids);
    });

    list.appendChild(el);
  });
}

async function loadQueue() {
  try {
    const res = await fetch("/api/queue");
    const data = await res.json();
    const nextEl = document.getElementById("queue-next-run");
    if (nextEl) {
      if (data.next_scheduled_run) {
        const dt = new Date(data.next_scheduled_run);
        nextEl.textContent = dt.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
      } else {
        nextEl.textContent = "Scheduler Paused";
      }
    }
    renderQueueList(data.queue || []);
  } catch (e) {
    console.error("Failed to load queue", e);
  }
}

async function saveQueueOrder(postIds) {
  try {
    const res = await fetch("/api/queue/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ post_ids: postIds }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      renderQueueList(data.queue || []);
    }
  } catch (e) {
    console.error("Failed to reorder queue", e);
  }
}

window.moveQueueItem = async (index, delta) => {
  const targetIdx = index + delta;
  if (targetIdx < 0 || targetIdx >= currentQueueItems.length) return;
  const ids = currentQueueItems.map((item) => item.id);
  const [moved] = ids.splice(index, 1);
  ids.splice(targetIdx, 0, moved);
  await saveQueueOrder(ids);
};

window.removeFromQueue = async (postId) => {
  try {
    const res = await fetch(`/api/queue/${postId}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      renderQueueList(data.queue || []);
      loadPosts();
      refreshStatus();
    }
  } catch (e) {
    console.error("Failed to remove from queue", e);
  }
};

document.getElementById("btn-refresh-queue").addEventListener("click", loadQueue);
document.getElementById("btn-clear-queue").addEventListener("click", async () => {
  if (!confirm("Remove all posts from the scheduled queue? (Rendered videos will remain in History.)")) return;
  await fetch("/api/queue", { method: "DELETE" });
  renderQueueList([]);
  loadPosts();
  refreshStatus();
});

// --- Posts History ---
let postsAutoPollTimer = null;

async function loadPosts() {
  const res = await fetch("/api/posts");
  const data = await res.json();
  const list = document.getElementById("posts-list");
  list.innerHTML = "";

  if (!data.posts.length) {
    list.innerHTML = '<p class="muted">No videos generated yet. Head to Studio to create your first GenX hit!</p>';
    return;
  }

  let hasActiveJobs = false;

  data.posts.forEach((p) => {
    const el = document.createElement("div");
    el.className = "item-card";
    const status = p.status || "rendered";
    if (status === "tts_pending" || status === "rendering") {
      hasActiveJobs = true;
    }

    const statusCls =
      status === "posted" ? "badge-posted"
      : (status === "failed" || status === "tts_failed") ? "badge-failed"
      : status === "scheduled" ? "badge-queued"
      : status === "tts_pending" ? "badge-tts-pending"
      : status === "rendering" ? "badge-rendering"
      : "";
    const statusText =
      status === "scheduled" ? `QUEUED${p.queue_position ? ` #${p.queue_position}` : ""}`
      : status === "tts_pending" ? "TTS PENDING"
      : status === "tts_failed" ? "TTS FAILED"
      : status === "rendering" ? "RENDERING"
      : status.toUpperCase();

    const safeArtist = (p.artist || "").replace(/'/g, "\\'");
    const safeTitle = (p.title || "").replace(/'/g, "\\'");
    const safeBadge = (p.chart_info || "").replace(/'/g, "\\'");
    const voiceTag = p.voice_region ? ` • 🎙️ ${p.voice_region.toUpperCase()}` : "";
    const hookHtml = p.hook_text ? `<div class="tts-hook-preview">"${p.hook_text}"</div>` : "";
    const errHtml = p.error_message && (status === "tts_failed" || status === "failed")
      ? `<div class="item-sub" style="color:var(--danger); margin-top:4px;">${p.error_message}</div>`
      : "";

    let actionsHtml = "";
    const deleteBtn = status !== "rendering"
      ? `<button class="btn-small btn-danger-outline" title="Delete render and unlock track for re-rendering" onclick="deletePostRender(${p.id}, '${safeArtist}', '${safeTitle}')">🗑️ Delete</button>`
      : "";

    if (status === "tts_failed") {
      actionsHtml = `
        <button class="btn-small" onclick="retryPostTTS(${p.id})">🔄 Retry TTS</button>
        <button class="btn-small" onclick="renderPostWithoutTTS(${p.id})">🎬 No TTS</button>
        ${deleteBtn}
      `;
    } else if (status === "tts_pending" || status === "rendering") {
      actionsHtml = `
        <button class="btn-small" onclick="renderPostWithoutTTS(${p.id})" title="Skip TTS and render now">⏭️ Skip TTS</button>
        ${deleteBtn}
      `;
    } else {
      const queueBtn = status === "scheduled"
        ? `<button class="btn-small" onclick="document.querySelector('[data-tab=&quot;queue&quot;]').click()">📅 Queued</button>`
        : `<button class="btn-small" onclick="queuePostForNext(${p.id})">📅 Queue</button>`;
      actionsHtml = `
        ${p.video_url ? `<button class="btn-small" onclick="previewFromList('${p.video_url}', '${safeArtist}', '${safeTitle}', '${safeBadge}', ${p.id})">▶️</button>` : ""}
        ${p.video_url ? queueBtn : ""}
        ${p.video_url ? `<button class="btn-small btn-publish-post" ${publishPollTimer ? "disabled" : ""} onclick="publishPost(${p.id})">🚀 Post</button>` : ""}
        ${deleteBtn}
      `;
    }

    el.innerHTML = `
      <div class="item-info">
        <div class="item-title">${p.artist} — ${p.title}</div>
        <div class="item-sub">${p.chart_info} • Template: ${p.template_used}${voiceTag}</div>
        <span class="badge ${statusCls}">${statusText}</span>
        ${hookHtml}
        ${errHtml}
      </div>
      <div class="item-actions">
        ${actionsHtml}
      </div>
    `;
    list.appendChild(el);
  });

  if (hasActiveJobs && !postsAutoPollTimer) {
    postsAutoPollTimer = setInterval(() => {
      const postsTab = document.getElementById("tab-posts");
      if (postsTab && postsTab.classList.contains("active")) {
        loadPosts();
      }
    }, 8000);
  } else if (!hasActiveJobs && postsAutoPollTimer) {
    clearInterval(postsAutoPollTimer);
    postsAutoPollTimer = null;
  }
}

window.deletePostRender = async (postId, artist, title) => {
  const label = `${artist || "Track"} — ${title || ""}`.trim();
  if (!confirm(`Delete render for "${label}"?\n\nThis removes the rendered video file and unlocks the track so you can re-render it.`)) {
    return;
  }
  try {
    const res = await fetch(`/api/posts/${postId}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Delete Error: " + (data.detail || `HTTP ${res.status}`));
      return;
    }
    if (currentPreviewPostId === postId) {
      currentPreviewPostId = null;
      const card = document.getElementById("preview-card");
      const vid = document.getElementById("preview-video");
      if (vid) {
        vid.pause();
        vid.removeAttribute("src");
        vid.load();
      }
      if (card) card.classList.add("hidden");
    }
    loadPosts();
    loadQueue();
    refreshStatus();
    if (typeof loadTracks === "function") loadTracks();
  } catch (err) {
    alert("Delete Error: " + (err.message || err));
  }
};

window.retryPostTTS = async (postId) => {
  const res = await fetch(`/api/posts/${postId}/retry-tts`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || "Failed to retry TTS");
    return;
  }
  loadPosts();
  refreshStatus();
};

window.renderPostWithoutTTS = async (postId) => {
  const res = await fetch(`/api/posts/${postId}/render-no-tts`, { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || "Failed to start fallback render");
    return;
  }
  loadPosts();
  refreshStatus();
};

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
  const defaultTemplate = settings.default_template || "auto";
  const eraAppropriate = settings.era_appropriate_template === "true";
  syncTemplateControls(defaultTemplate, eraAppropriate);
  syncAutoQueueControls(settings.auto_queue_on_generate === "true");
  syncTtsEnabledControls(settings.tts_enabled === "true");
  document.getElementById("set-wm-opacity").value = settings.watermark_opacity || "0.65";
  document.getElementById("wm-val").textContent = settings.watermark_opacity || "0.65";
  document.getElementById("set-auto-publish").checked = settings.auto_publish === "true";
}

document.getElementById("set-era-appropriate").addEventListener("change", (e) => {
  const tpl = document.getElementById("set-template").value;
  syncTemplateControls(tpl, e.target.checked);
  persistTemplateControls(tpl, e.target.checked);
});

document.getElementById("set-template").addEventListener("change", (e) => {
  const era = document.getElementById("set-era-appropriate").checked;
  syncTemplateControls(e.target.value, era);
  persistTemplateControls(e.target.value, era);
});

document.getElementById("set-auto-queue").addEventListener("change", (e) => {
  syncAutoQueueControls(e.target.checked);
  persistAutoQueueSetting(e.target.checked);
});

document.getElementById("set-tts-enabled")?.addEventListener("change", (e) => {
  syncTtsEnabledControls(e.target.checked);
  persistTtsEnabledSetting(e.target.checked);
});

document.getElementById("set-wildcard").addEventListener("input", (e) => {
  document.getElementById("wildcard-val").textContent = e.target.value;
});

document.getElementById("set-wm-opacity").addEventListener("input", (e) => {
  document.getElementById("wm-val").textContent = e.target.value;
});

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const defaultTemplate = document.getElementById("set-template").value;
  const eraAppropriate = document.getElementById("set-era-appropriate").checked;
  const autoQueue = document.getElementById("set-auto-queue").checked;
  const ttsEnabled = Boolean(document.getElementById("set-tts-enabled")?.checked);
  const payload = {
    settings: {
      schedule_enabled: document.getElementById("set-sched-enabled").checked ? "true" : "false",
      daily_post_times: document.getElementById("set-post-times").value,
      jitter_minutes: document.getElementById("set-jitter").value,
      wildcard_chance: document.getElementById("set-wildcard").value,
      default_template: defaultTemplate,
      era_appropriate_template: eraAppropriate ? "true" : "false",
      auto_queue_on_generate: autoQueue ? "true" : "false",
      tts_enabled: ttsEnabled ? "true" : "false",
      watermark_opacity: document.getElementById("set-wm-opacity").value,
      auto_publish: document.getElementById("set-auto-publish").checked ? "true" : "false"
    }
  };
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  syncTemplateControls(defaultTemplate, eraAppropriate);
  syncAutoQueueControls(autoQueue);
  syncTtsEnabledControls(ttsEnabled);
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

// --- Regional Voice Profile Studio (UK / US) ---
function setVoiceMsg(text, level = "muted") {
  const el = document.getElementById("tts-voice-msg");
  if (!el) return;
  el.textContent = text;
  el.className = level === "ok" ? "status-ok" : level === "err" ? "status-err" : "muted";
}

function renderVoiceProfileRegion(region, profile) {
  const badge = document.getElementById(`voice-${region}-badge`);
  const list = document.getElementById(`voice-${region}-list`);
  if (!badge || !list) return;

  const clips = (profile && profile.clips) || [];
  badge.textContent = clips.length ? `${clips.length} clip${clips.length > 1 ? "s" : ""} (${profile.total_duration}s)` : "No clips";
  badge.className = `badge ${clips.length ? "badge-posted" : ""}`;

  list.innerHTML = "";
  if (!clips.length) {
    list.innerHTML = `<p class="muted" style="margin:0;">Upload a 10–30s clean voice clip.</p>`;
    return;
  }

  clips.forEach((c) => {
    const row = document.createElement("div");
    row.className = "voice-clip-row";
    const safeFile = c.filename.replace(/'/g, "\\'");
    row.innerHTML = `
      <span class="voice-clip-name" title="${c.filename}">${c.filename} (${c.duration}s)</span>
      <button type="button" class="btn-small btn-danger-outline" onclick="deleteVoiceClip('${region}', '${safeFile}')">🗑️</button>
    `;
    list.appendChild(row);
  });
}

async function loadVoiceProfiles() {
  try {
    const res = await fetch("/api/tts/status");
    const data = await res.json();
    const pill = document.getElementById("tts-worker-pill");
    if (pill && data.worker) {
      if (data.worker.online) {
        pill.textContent = data.worker.busy ? "TTS Worker: Synthesizing" : "TTS Worker: Ready";
        pill.className = "pill pill-ok";
      } else {
        pill.textContent = "TTS Worker: Offline";
        pill.className = "pill pill-warn";
      }
    }
    if (data.profiles) {
      renderVoiceProfileRegion("uk", data.profiles.uk);
      renderVoiceProfileRegion("us", data.profiles.us);
    }
  } catch (e) {
    console.error("Failed to load TTS voice profiles", e);
  }
}

async function uploadVoiceFile(region, file) {
  if (!file) return;
  const denoise = Boolean(document.getElementById(`voice-${region}-denoise`)?.checked);
  setVoiceMsg(
    denoise
      ? `Cleaning background noise & normalizing ${file.name} for ${region.toUpperCase()} voice...`
      : `Converting & normalizing ${file.name} for ${region.toUpperCase()} voice...`,
    "muted"
  );
  const form = new FormData();
  form.append("file", file);
  form.append("denoise", denoise ? "true" : "false");
  try {
    const res = await fetch(`/api/tts/voices/${region}`, { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setVoiceMsg(data.detail || `Upload failed (HTTP ${res.status})`, "err");
      return;
    }
    const cleanTag = data.clip && data.clip.denoised ? ", noise cleaned" : "";
    setVoiceMsg(`Saved ${region.toUpperCase()} clip: ${data.clip.filename} (${data.clip.duration}s${cleanTag})`, "ok");
    if (data.profiles) {
      renderVoiceProfileRegion("uk", data.profiles.uk);
      renderVoiceProfileRegion("us", data.profiles.us);
    }
  } catch (err) {
    setVoiceMsg(`Voice upload failed: ${err.message || err}`, "err");
  }
}

document.getElementById("voice-uk-input")?.addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  if (f) uploadVoiceFile("uk", f);
  e.target.value = "";
});

document.getElementById("voice-us-input")?.addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  if (f) uploadVoiceFile("us", f);
  e.target.value = "";
});

window.deleteVoiceClip = async (region, filename) => {
  if (!confirm(`Delete ${region.toUpperCase()} voice clip "${filename}"?`)) return;
  const res = await fetch(`/api/tts/voices/${region}/${encodeURIComponent(filename)}`, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.profiles) {
    renderVoiceProfileRegion("uk", data.profiles.uk);
    renderVoiceProfileRegion("us", data.profiles.us);
    setVoiceMsg(`Deleted ${filename}.`, "muted");
  }
};

window.testVoiceProfile = async (region) => {
  const btn = document.getElementById(`btn-test-${region}`);
  const audioEl = document.getElementById("tts-test-audio");
  if (btn) btn.disabled = true;
  setVoiceMsg(`Synthesizing ${region.toUpperCase()} test clip on CPU (may take 30–90s)...`, "muted");
  try {
    const res = await fetch("/api/tts/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ region }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setVoiceMsg(data.detail || `Test synthesis failed (HTTP ${res.status})`, "err");
      return;
    }
    setVoiceMsg(`Synthesized ${data.duration}s ${region.toUpperCase()} preview in ${data.elapsed_sec}s!`, "ok");
    if (audioEl && data.audio_url) {
      audioEl.src = data.audio_url;
      audioEl.classList.remove("hidden");
      audioEl.play().catch(() => {});
    }
  } catch (err) {
    setVoiceMsg(`Test failed: ${err.message || err}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
};

// Initial load
settingsReady = loadSettings();
refreshStatus();
setInterval(refreshStatus, 15000);
