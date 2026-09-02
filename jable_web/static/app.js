const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const form = document.querySelector("#download-form");
const formMessage = document.querySelector("#form-message");
const stateLabels = { idle: "空闲", running: "运行中", searching: "搜索磁链", cancelling: "取消中", cancelled: "已取消", alternatives: "发现磁链", completed: "已完成", failed: "失败" };
let showCurrentTask = false;
let managingMedia = false;
let currentMediaItems = [];
const selectedMedia = new Set();

function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index >= 3 ? 2 : 1)} ${units[index]}`;
}

function formatDate(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(timestamp * 1000));
}

function mediaUrl(prefix, filename) {
  return `${prefix}/${filename.split("/").map(encodeURIComponent).join("/")}`;
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("浏览器未允许复制");
}

function renderMagnets(task) {
  const section = document.querySelector("#magnet-section");
  const container = document.querySelector("#magnet-list");
  const items = Array.isArray(task.magnets) ? task.magnets : [];
  if (!items.length) {
    section.hidden = true;
    container.replaceChildren();
    return;
  }
  section.hidden = false;
  container.replaceChildren(...items.map((item, index) => {
    const row = document.createElement("article");
    row.className = `magnet-row${index === 0 ? " recommended" : ""}`;

    const main = document.createElement("div");
    main.className = "magnet-main";
    const heading = document.createElement("div");
    heading.className = "magnet-heading";
    const title = document.createElement("strong");
    title.textContent = item.title || task.code;
    heading.appendChild(title);
    if (index === 0) {
      const recommended = document.createElement("span");
      recommended.className = "recommend-badge";
      recommended.textContent = "推荐";
      heading.appendChild(recommended);
    }

    const tags = document.createElement("div");
    tags.className = "magnet-tags";
    if (item.is_hd) tags.appendChild(Object.assign(document.createElement("span"), { textContent: "高清" }));
    if (item.has_subtitle) tags.appendChild(Object.assign(document.createElement("span"), { textContent: "中文字幕" }));
    const meta = document.createElement("span");
    meta.className = "magnet-meta";
    meta.textContent = `${item.size || "大小未知"} · 分享日期 ${item.share_date || "未知"}`;
    tags.appendChild(meta);
    main.append(heading, tags);

    const button = document.createElement("button");
    button.type = "button";
    button.className = index === 0 ? "primary-button copy-magnet" : "ghost-button copy-magnet";
    button.textContent = "复制磁力链接";
    button.addEventListener("click", async () => {
      const original = button.textContent;
      try {
        await copyText(item.magnet);
        button.textContent = "已复制";
      } catch (error) {
        button.textContent = error.message;
      }
      window.setTimeout(() => { button.textContent = original; }, 1800);
    });
    row.append(main, button);
    return row;
  }));
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("登录已失效");
  }
  if (!response.ok) throw new Error(payload.detail || "请求失败");
  return payload;
}

function renderIdleTask() {
  document.querySelector("#task-code").textContent = "暂无任务";
  const badge = document.querySelector("#task-state");
  badge.textContent = "空闲";
  badge.className = "status-pill idle";
  document.querySelector("#progress-bar").className = "";
  document.querySelector("#task-log").textContent = "等待新任务…";
  document.querySelector("#cancel-task").hidden = true;
  renderMagnets({ magnets: [] });
}

async function refreshStatus() {
  try {
    const task = await requestJson("/api/status");
    const state = task.state || "idle";
    if (["running", "searching", "cancelling"].includes(state)) showCurrentTask = true;
    if (!showCurrentTask) {
      renderIdleTask();
      return;
    }
    document.querySelector("#task-code").textContent = task.code || "暂无任务";
    const badge = document.querySelector("#task-state");
    badge.textContent = stateLabels[state] || state;
    badge.className = `status-pill ${state}`;
    document.querySelector("#progress-bar").className = ["running", "searching", "cancelling"].includes(state) ? "running" : state;
    const cancelButton = document.querySelector("#cancel-task");
    cancelButton.hidden = !["running", "cancelling"].includes(state);
    cancelButton.disabled = state === "cancelling";
    cancelButton.textContent = state === "cancelling" ? "取消中…" : "取消任务";
    renderMagnets(task);
    if (state === "alternatives" && window.lastAlternativesCode !== task.code) {
      window.lastAlternativesCode = task.code;
      window.requestAnimationFrame(() => {
        document.querySelector("#magnet-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
    const log = document.querySelector("#task-log");
    const visibleLogs = task.logs?.length ? [...task.logs] : [];
    if (task.progress) visibleLogs.push(task.progress);
    const nextLog = visibleLogs.length ? visibleLogs.join("\n") : "等待新任务…";
    if (log.textContent !== nextLog) {
      log.textContent = nextLog;
      window.requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    }
    if (state === "completed" && window.lastCompletedCode !== task.code) {
      window.lastCompletedCode = task.code;
      refreshMedia();
    }
  } catch (error) {
    document.querySelector("#task-log").textContent = error.message;
  }
}

async function refreshMedia() {
  const container = document.querySelector("#media-list");
  try {
    const data = await requestJson("/api/media");
    currentMediaItems = Array.isArray(data.items) ? data.items : [];
    const availableNames = new Set(currentMediaItems.map((item) => item.name));
    for (const name of selectedMedia) {
      if (!availableNames.has(name)) selectedMedia.delete(name);
    }
    document.querySelector("#media-count").textContent = Number.isInteger(data.total_count) ? data.total_count : currentMediaItems.length;
    updateMediaSelection();
    if (!currentMediaItems.length) {
      container.innerHTML = '<div class="empty-state">已完成列表暂时为空</div>';
      return;
    }
    container.replaceChildren(...currentMediaItems.map((item) => {
      const row = document.createElement("div");
      row.className = "media-row";
      row.dataset.filename = item.name;

      const select = document.createElement("label");
      select.className = "media-select";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedMedia.has(item.name);
      checkbox.setAttribute("aria-label", `选择 ${item.code}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedMedia.add(item.name);
        else selectedMedia.delete(item.name);
        updateMediaSelection();
      });
      select.appendChild(checkbox);

      const open = document.createElement("button");
      open.type = "button";
      open.className = "media-open";
      open.innerHTML = `<span class="media-code"></span><span class="media-meta"></span><span class="row-action"></span>`;
      open.querySelector(".media-code").textContent = item.code;
      open.querySelector(".media-meta").textContent = `${item.category} · ${formatBytes(item.size)} · ${formatDate(item.modified_at)}`;
      open.querySelector(".row-action").textContent = managingMedia ? "选择" : "查看";
      open.addEventListener("click", () => {
        if (!managingMedia) {
          showMedia(item.name);
          return;
        }
        checkbox.checked = !checkbox.checked;
        checkbox.dispatchEvent(new Event("change"));
      });
      row.append(select, open);
      return row;
    }));
  } catch (error) {
    container.innerHTML = `<div class="empty-state"></div>`;
    container.firstElementChild.textContent = error.message;
  }
}

function updateMediaSelection() {
  document.querySelector("#selected-media-count").textContent = `已选择 ${selectedMedia.size} 项`;
  document.querySelector("#open-delete-media").disabled = selectedMedia.size === 0;
  const selectAll = document.querySelector("#select-all-media");
  const selectedVisible = currentMediaItems.filter((item) => selectedMedia.has(item.name)).length;
  selectAll.checked = currentMediaItems.length > 0 && selectedVisible === currentMediaItems.length;
  selectAll.indeterminate = selectedVisible > 0 && selectedVisible < currentMediaItems.length;
}

function setMediaManagement(enabled) {
  managingMedia = enabled;
  document.querySelector(".library-section").classList.toggle("managing", enabled);
  document.querySelector("#media-management").hidden = !enabled;
  document.querySelector("#manage-media").hidden = enabled;
  if (!enabled) selectedMedia.clear();
  updateMediaSelection();
  refreshMedia();
}

function openDeleteMediaDialog() {
  if (!selectedMedia.size) return;
  document.querySelector("#delete-media-title").textContent = `删除已选择的 ${selectedMedia.size} 项`;
  document.querySelector("#delete-media-dialog").showModal();
}

async function performMediaAction(action) {
  const message = document.querySelector("#media-action-message");
  const dialog = document.querySelector("#delete-media-dialog");
  const items = [...selectedMedia];
  try {
    const result = await requestJson("/api/media/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ action, items }),
    });
    dialog.close();
    message.className = "media-action-message";
    message.textContent = result.message;
    setMediaManagement(false);
  } catch (error) {
    message.className = "media-action-message error";
    message.textContent = error.message;
  }
}

async function showMedia(filename) {
  const dialog = document.querySelector("#media-dialog");
  const details = document.querySelector("#detail-grid");
  document.querySelector("#detail-name").textContent = filename;
  details.innerHTML = '<div class="empty-state">正在读取媒体信息…</div>';
  dialog.showModal();
  try {
    const item = await requestJson(mediaUrl("/api/media", filename));
    const resolution = item.width && item.height ? `${item.width}×${item.height}` : "未知";
    const fields = [
      ["视频", `${item.video_codec} / ${resolution}`],
      ["音频", item.audio_codec],
      ["时长", item.duration_text],
      ["大小", formatBytes(item.size)],
    ];
    details.replaceChildren(...fields.flatMap(([label, value]) => {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      return [dt, dd];
    }));
    document.querySelector("#download-link").href = mediaUrl("/download", filename);
  } catch (error) {
    details.innerHTML = '<div class="empty-state"></div>';
    details.firstElementChild.textContent = error.message;
  }
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  formMessage.textContent = "";
  const code = new FormData(form).get("code");
  try {
    const result = await requestJson("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ code }),
    });
    formMessage.textContent = `${result.code} 已加入任务`;
    showCurrentTask = true;
    form.reset();
    refreshStatus();
  } catch (error) {
    formMessage.textContent = error.message;
  }
});

document.querySelector("#cancel-task")?.addEventListener("click", () => {
  document.querySelector("#cancel-task-dialog").showModal();
});
document.querySelector("#close-cancel-task")?.addEventListener("click", () => document.querySelector("#cancel-task-dialog").close());
document.querySelector("#dismiss-cancel-task")?.addEventListener("click", () => document.querySelector("#cancel-task-dialog").close());
document.querySelector("#confirm-cancel-task")?.addEventListener("click", async () => {
  const button = document.querySelector("#confirm-cancel-task");
  button.disabled = true;
  button.textContent = "正在取消…";
  try {
    await requestJson("/api/tasks/cancel", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
    });
    document.querySelector("#cancel-task-dialog").close();
    showCurrentTask = false;
    await refreshStatus();
  } catch (error) {
    formMessage.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "确认取消";
  }
});

document.querySelector("#refresh-media")?.addEventListener("click", refreshMedia);
document.querySelector("#manage-media")?.addEventListener("click", () => setMediaManagement(true));
document.querySelector("#cancel-manage-media")?.addEventListener("click", () => setMediaManagement(false));
document.querySelector("#select-all-media")?.addEventListener("change", (event) => {
  selectedMedia.clear();
  if (event.currentTarget.checked) currentMediaItems.forEach((item) => selectedMedia.add(item.name));
  updateMediaSelection();
  refreshMedia();
});
document.querySelector("#open-delete-media")?.addEventListener("click", openDeleteMediaDialog);
document.querySelector("#hide-selected-media")?.addEventListener("click", () => performMediaAction("hide"));
document.querySelector("#delete-selected-media")?.addEventListener("click", () => performMediaAction("delete"));
document.querySelector("#close-delete-media")?.addEventListener("click", () => document.querySelector("#delete-media-dialog").close());
document.querySelector("#cancel-delete-media")?.addEventListener("click", () => document.querySelector("#delete-media-dialog").close());
document.querySelector("#close-dialog")?.addEventListener("click", () => document.querySelector("#media-dialog").close());
document.querySelector("#media-dialog")?.addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

refreshStatus();
refreshMedia();
window.setInterval(refreshStatus, 1500);
