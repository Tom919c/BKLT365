const state = { dates: [], date: "", candidates: [], previews: [], busy: false };

const $ = (id) => document.getElementById(id);
const loginView = $("login-view");
const appView = $("app-view");
const statusBox = $("status");

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function setStatus(text, isError = false) {
  statusBox.textContent = text || "";
  statusBox.classList.toggle("error", isError);
}

function setBusy(busy) {
  state.busy = busy;
  ["sync", "submit", "date-select"].forEach((id) => { $(id).disabled = busy; });
}

async function api(path, options = {}) {
  const config = { credentials: "same-origin", ...options };
  if (config.body && typeof config.body !== "string") {
    config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("登录已失效，请重新登录");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `操作失败（${response.status}）`);
  return data;
}

function showLogin() {
  appView.hidden = true;
  loginView.hidden = false;
  $("password").value = "";
  $("password").focus();
}

function showApp() {
  loginView.hidden = true;
  appView.hidden = false;
}

function updateSelectedCount() {
  const count = state.candidates.filter((item) => item.selected).length;
  $("selected-count").textContent = `已选 ${count} 张`;
}

function markSelectionChanged() {
  state.previews = [];
  $("preview-section").hidden = true;
  updateSelectedCount();
}

function renderCandidates() {
  const container = $("candidates");
  container.replaceChildren();
  if (!state.candidates.length) {
    container.appendChild(node("p", "muted", "这个日期暂无投稿。可点击“同步飞书”获取最新投稿。"));
    updateSelectedCount();
    return;
  }
  state.candidates.forEach((item) => {
    const card = node("article", `candidate-card${item.selected ? " selected" : ""}`);
    const image = node("img");
    image.src = item.image_url;
    image.alt = `${item.photographer}《${item.title}》`;
    image.loading = "lazy";
    card.appendChild(image);

    const body = node("div", "candidate-body");
    body.appendChild(node("h3", "", `《${item.title}》`));
    body.appendChild(node("p", "candidate-meta", `摄影师：${item.photographer}${item.handle ? ` · ${item.handle}` : ""}`));

    const label = node("label", "select-label");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = item.selected;
    checkbox.setAttribute("aria-label", `选择《${item.title}》`);
    checkbox.addEventListener("change", () => {
      item.selected = checkbox.checked;
      card.classList.toggle("selected", item.selected);
      markSelectionChanged();
    });
    label.appendChild(checkbox);
    label.appendChild(node("span", "", "选为入选"));
    body.appendChild(label);

    if (item.problems && item.problems.length) {
      const problems = node("ul", "problems");
      item.problems.forEach((problem) => problems.appendChild(node("li", "", problem)));
      body.appendChild(problems);
    }
    card.appendChild(body);
    container.appendChild(card);
  });
  updateSelectedCount();
}

function renderPreviews() {
  const container = $("previews");
  container.replaceChildren();
  state.previews.forEach((item) => {
    const card = node("article", "preview-card");
    card.appendChild(node("h3", "", item.post_title || `《${item.title}》`));
    card.appendChild(node("p", "candidate-meta", `摄影师：${item.photographer}`));
    if (item.published) {
      card.appendChild(node("p", "published-badge", `已发布${item.published_at ? ` · ${item.published_at}` : ""}`));
    }
    if (!item.ok) {
      card.appendChild(node("p", "error", item.error || "文案生成失败"));
    } else {
      card.appendChild(node("pre", "", item.content));
      if (item.warnings && item.warnings.length) {
        const warnings = node("ul", "problems");
        item.warnings.forEach((warning) => warnings.appendChild(node("li", "", warning)));
        card.appendChild(warnings);
      }
      const actions = node("div", "preview-actions");
      const fillButton = node("button", "secondary", item.published ? "已发布，不能再填入" : "仅填入后台预览（不发布）");
      fillButton.type = "button";
      fillButton.disabled = item.published;
      const publishButton = node("button", "primary danger", item.published ? "已发布" : "确认发布到小红书");
      publishButton.type = "button";
      publishButton.disabled = true;
      if (!item.published) {
        fillButton.addEventListener("click", () => fillPlatformPreview(item, fillButton, publishButton, card));
        publishButton.addEventListener("click", () => publishPlatformNote(item, publishButton, fillButton, card));
      }
      actions.appendChild(fillButton);
      actions.appendChild(publishButton);
      card.appendChild(actions);
      const hint = node(
        "p",
        "hint",
        item.published
          ? "这篇已记录为发布成功，不能重复发布。"
          : "先填入后台核对，核对无误后才能点“确认发布”。"
      );
      card.appendChild(hint);
      if (!item.published) {
        refreshDraftState(item, publishButton, hint);
      }
    }
    container.appendChild(card);
  });
  $("preview-section").hidden = false;
}

async function refreshDraftState(item, publishButton, hint) {
  try {
    const data = await api(
      `/api/platform-draft?date=${encodeURIComponent(state.date)}&id=${encodeURIComponent(item.id)}`
    );
    if (data.matches) {
      publishButton.disabled = false;
      publishButton.textContent = "后台草稿已核对，确认发布";
      hint.textContent = "后台草稿与这篇一致，可以核对后确认发布。";
      return;
    }
    hint.textContent = data.message || "后台草稿还没有准备好，请先填入预览。";
  } catch (error) {
    hint.textContent = error.message;
  }
}

async function loadDates(preferredDate) {
  const data = await api("/api/dates");
  state.dates = data.dates || [];
  const select = $("date-select");
  select.replaceChildren();
  state.dates.forEach((item) => {
    const option = node("option", "", `${item.date}（${item.count} 张，已选 ${item.selected}）`);
    option.value = item.date;
    select.appendChild(option);
  });
  state.date = preferredDate || state.date || (state.dates[0] && state.dates[0].date) || "";
  if (state.date && state.dates.some((item) => item.date === state.date)) select.value = state.date;
  if (select.value) state.date = select.value;
  if (state.date) await loadCandidates();
  else {
    state.candidates = [];
    renderCandidates();
    setStatus("暂无投稿，请点击“同步飞书”。");
  }
}

async function loadCandidates() {
  if (!state.date) return;
  setBusy(true);
  setStatus("正在读取投稿…");
  try {
    const data = await api(`/api/candidates?date=${encodeURIComponent(state.date)}`);
    state.candidates = data.candidates || [];
    state.previews = [];
    $("preview-section").hidden = true;
    renderCandidates();
    const unparsed = (data.unparsed || []).length;
    setStatus(`共 ${state.candidates.length} 张投稿${unparsed ? `；另有 ${unparsed} 个文件命名无法识别` : ""}`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function submitSelection() {
  const selected = state.candidates.filter((item) => item.selected).map((item) => item.id);
  if (!selected.length && !window.confirm("当前未选照片，将清空这一天的入选结果。继续吗？")) return;
  setBusy(true);
  setStatus("正在保存入选并生成预览…");
  try {
    const data = await api("/api/selection", {
      method: "POST",
      body: { date: state.date, selected },
    });
    state.previews = data.previews || [];
    renderPreviews();
    setStatus(`已保存 ${data.selected_count} 张入选，预览已生成；尚未填入或发布到小红书。`);
    if (state.previews.length) $("preview-section").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function syncFeishu() {
  setBusy(true);
  setStatus("正在从飞书读取并下载新投稿，请稍候…");
  try {
    const data = await api("/api/sync", { method: "POST", body: {} });
    setStatus(`同步完成：新增下载 ${data.downloaded} 张，跳过 ${(data.skipped || []).length} 个（已存在或非图片）。`);
    await loadDates();
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function fillPlatformPreview(item, fillButton, publishButton, card) {
  fillButton.disabled = true;
  fillButton.textContent = "正在填入，未发布…";
  publishButton.disabled = true;
  let output = card.querySelector(".operation-result");
  if (!output) {
    output = node("div", "operation-result");
    card.appendChild(output);
  }
  output.textContent = "正在检查登录态和空白草稿，然后填入小红书后台。";
  try {
    const data = await api("/api/platform-preview", {
      method: "POST",
      body: { date: state.date, id: item.id },
    });
    output.textContent = `已填入预览，未点击发布。请核对后台内容，确认无误后再点“确认发布”。\n${data.tail || ""}`;
    fillButton.textContent = "重新填入后台预览";
    fillButton.disabled = false;
    if (!item.published) {
      publishButton.disabled = false;
      publishButton.textContent = "预览已核对，确认发布";
    }
  } catch (error) {
    output.textContent = error.message;
    fillButton.textContent = "重试填入（不发布）";
    fillButton.disabled = false;
    publishButton.disabled = true;
    publishButton.textContent = "确认发布到小红书";
  }
}

async function publishPlatformNote(item, publishButton, fillButton, card) {
  const confirmed = window.confirm(
    `即将正式发布 ${item.post_title || `《${item.title}》`} 到小红书。\n\n` +
    "发布后不可撤回，请确认后台预览中的图片、标题、正文和标签都正确。\n\n继续发布吗？"
  );
  if (!confirmed) return;
  publishButton.disabled = true;
  fillButton.disabled = true;
  publishButton.textContent = "正在发布…";
  let output = card.querySelector(".operation-result");
  if (!output) {
    output = node("div", "operation-result");
    card.appendChild(output);
  }
  output.textContent = "正在核对后台草稿并点击发布，请勿关闭页面。";
  try {
    const data = await api("/api/platform-publish", {
      method: "POST",
      body: { date: state.date, id: item.id, confirmed: true },
    });
    if (data.published) {
      item.published = true;
      publishButton.textContent = "已发布";
      publishButton.disabled = true;
      fillButton.textContent = "已发布，不能再填入";
      fillButton.disabled = true;
      output.textContent = "发布成功。" + (data.note_url ? `\n笔记链接：${data.note_url}` : "");
      setStatus(`${item.post_title || `《${item.title}》`} 已发布成功。`);
      return;
    }
    publishButton.textContent = data.needs_review ? "发布状态待人工确认" : "发布失败";
    publishButton.disabled = true;
    output.textContent = data.message || "已点击发布，但未确认成功，请到小红书后台核对，不要重复点击。";
    if (data.note_url) output.textContent += `\n笔记链接：${data.note_url}`;
    setStatus("发布结果未确认，请到小红书后台核对。", true);
  } catch (error) {
    publishButton.textContent = "重新确认发布";
    publishButton.disabled = false;
    fillButton.disabled = false;
    output.textContent = error.message;
    setStatus(error.message, true);
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("password").value } });
    showApp();
    await loadDates();
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("date-select").addEventListener("change", (event) => {
  state.date = event.target.value;
  loadCandidates();
});
$("submit").addEventListener("click", submitSelection);
$("sync").addEventListener("click", syncFeishu);
$("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: {} }).catch(() => {});
  showLogin();
});

(async function init() {
  try {
    const session = await api("/api/session");
    if (!session.authenticated) return showLogin();
    showApp();
    await loadDates();
  } catch (error) {
    showLogin();
    $("login-error").textContent = error.message;
  }
})();
