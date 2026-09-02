const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

async function requestJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(payload),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (response.status === 401) window.location.href = "/login";
  if (!response.ok) throw new Error(data.detail || "保存失败");
  return data;
}

function showMessage(target, text, success = false) {
  target.textContent = text;
  target.className = `settings-message ${success ? "success" : "error"}`;
}

document.querySelector("#account-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#account-message");
  const values = new FormData(form);
  try {
    const result = await requestJson("/api/settings/account", {
      username: values.get("username"),
      new_password: values.get("new_password"),
      confirm_password: values.get("confirm_password"),
      current_password: values.get("current_password"),
    });
    showMessage(message, `${result.message}。其他已登录设备已退出。`, true);
    form.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ""; });
  } catch (error) {
    showMessage(message, error.message);
  }
});

document.querySelector("#port-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#port-message");
  if (!document.querySelector("#firewall-confirm").checked) {
    showMessage(message, "请先确认已准备放行新端口");
    return;
  }
  const values = new FormData(form);
  try {
    const result = await requestJson("/api/settings/port", {
      port: values.get("port"),
      current_password: values.get("current_password"),
    });
    if (!result.restart) {
      showMessage(message, result.message, true);
      return;
    }
    message.replaceChildren();
    const text = document.createElement("span");
    text.textContent = `${result.message}。新地址：`;
    const link = document.createElement("a");
    link.href = result.new_url;
    link.textContent = result.new_url;
    message.append(text, link);
    message.className = "settings-message success";
  } catch (error) {
    showMessage(message, error.message);
  }
});

function proxyPayload(form, clear = false) {
  const values = new FormData(form);
  return {
    proxy_url: values.get("proxy_url"),
    proxy_download: document.querySelector("#proxy-download").checked,
    current_password: values.get("current_password"),
    clear,
  };
}

function updateProxyStatus(configured, label = "") {
  const status = document.querySelector("#proxy-status");
  const clear = document.querySelector("#proxy-clear");
  status.dataset.configured = configured ? "true" : "false";
  status.replaceChildren();
  if (configured) {
    status.append("已配置：");
    const strong = document.createElement("strong");
    strong.textContent = label;
    status.append(strong, "（账号和密码已隐藏）");
  } else {
    status.textContent = "当前未配置代理";
  }
  clear.disabled = !configured;
}

document.querySelector("#proxy-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#proxy-message");
  try {
    const result = await requestJson("/api/settings/supjav-proxy", proxyPayload(form));
    updateProxyStatus(result.configured, result.proxy_label);
    showMessage(message, result.message, true);
    form.querySelector("#proxy-url").value = "";
    form.querySelector("#proxy-current-password").value = "";
  } catch (error) {
    showMessage(message, error.message);
  }
});

document.querySelector("#proxy-test")?.addEventListener("click", async () => {
  const form = document.querySelector("#proxy-form");
  const message = document.querySelector("#proxy-message");
  try {
    const result = await requestJson(
      "/api/settings/supjav-proxy/test",
      proxyPayload(form),
    );
    showMessage(message, `${result.message}：${result.proxy_label}`, true);
  } catch (error) {
    showMessage(message, error.message);
  }
});

document.querySelector("#proxy-clear")?.addEventListener("click", async () => {
  if (!window.confirm("确定清除 SupJav 专用代理吗？之后 SupJav 将恢复 VPS 直连。")) return;
  const form = document.querySelector("#proxy-form");
  const message = document.querySelector("#proxy-message");
  try {
    const result = await requestJson(
      "/api/settings/supjav-proxy",
      proxyPayload(form, true),
    );
    document.querySelector("#proxy-download").checked = false;
    updateProxyStatus(false);
    showMessage(message, result.message, true);
    form.querySelector("#proxy-url").value = "";
    form.querySelector("#proxy-current-password").value = "";
  } catch (error) {
    showMessage(message, error.message);
  }
});
