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
