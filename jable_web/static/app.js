const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const form = document.querySelector("#download-form");
const formMessage = document.querySelector("#form-message");
const stateLabels = { idle: "空闲", running: "运行中", completed: "已完成", failed: "失败" };

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

async function refreshStatus() {
  try {
    const task = await requestJson("/api/status");
    const state = task.state || "idle";
    document.querySelector("#task-code").textContent = task.code || "暂无任务";
    const badge = document.querySelector("#task-state");
    badge.textContent = stateLabels[state] || state;
    badge.className = `status-pill ${state}`;
    document.querySelector("#progress-bar").className = state === "running" ? "running" : state;
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
    document.querySelector("#media-count").textContent = data.items.length;
    if (!data.items.length) {
      container.innerHTML = '<div class="empty-state">媒体库暂时没有成品</div>';
      return;
    }
    container.replaceChildren(...data.items.map((item) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "media-row";
      row.dataset.filename = item.name;
      row.innerHTML = `<span class="media-code"></span><span class="media-meta"></span><span class="row-action">查看</span>`;
      row.querySelector(".media-code").textContent = item.code;
      row.querySelector(".media-meta").textContent = `${formatBytes(item.size)} · ${formatDate(item.modified_at)}`;
      row.addEventListener("click", () => showMedia(item.name));
      return row;
    }));
  } catch (error) {
    container.innerHTML = `<div class="empty-state"></div>`;
    container.firstElementChild.textContent = error.message;
  }
}

async function showMedia(filename) {
  const dialog = document.querySelector("#media-dialog");
  const details = document.querySelector("#detail-grid");
  document.querySelector("#detail-name").textContent = filename;
  details.innerHTML = '<div class="empty-state">正在读取媒体信息…</div>';
  dialog.showModal();
  try {
    const item = await requestJson(`/api/media/${encodeURIComponent(filename)}`);
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
    document.querySelector("#download-link").href = `/download/${encodeURIComponent(filename)}`;
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
    form.reset();
    refreshStatus();
  } catch (error) {
    formMessage.textContent = error.message;
  }
});

document.querySelector("#refresh-media")?.addEventListener("click", refreshMedia);
document.querySelector("#close-dialog")?.addEventListener("click", () => document.querySelector("#media-dialog").close());
document.querySelector("#media-dialog")?.addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

refreshStatus();
refreshMedia();
window.setInterval(refreshStatus, 1500);
