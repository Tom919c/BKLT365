(() => {
  "use strict";

  const state = {
    dashboard: null,
    week: null,
    notes: [],
    settings: null,
    currentNote: null,
    reorderGroup: null,
    reorderIds: [],
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));

  const statusLabel = {
    collected: "待入选",
    selected: "已入选",
    published: "已发布",
    unselected: "未入选",
    collecting: "收集中",
    closed: "已收束",
    upload_blocked: "上传受阻",
    upload_failed: "上传失败",
    uploaded: "已上传",
    cleanup_pending: "待清理旧文件",
    queued: "排队中",
    running: "上传中",
    failed: "失败",
    uploaded_with_cleanup_pending: "已上传·待清理",
  };

  const taskTone = {
    queued: "neutral", running: "selected", failed: "danger",
    uploaded: "published", uploaded_with_cleanup_pending: "unselected",
  };

  function todayISO() {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 10);
  }

  function toast(message, isError = false) {
    const node = $("toast");
    node.textContent = message;
    node.className = "toast" + (isError ? " error" : "");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.add("hidden"), 3800);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
      body: options.body != null && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : {};
    if (response.status === 401) {
      let message = payload.message;
      if (!message && payload.detail) {
        message = typeof payload.detail === "string" ? payload.detail : (payload.detail.message || JSON.stringify(payload.detail));
      }
      if (!path.startsWith("/api/login")) {
        showLogin(true);
      }
      throw new Error(message || (path.startsWith("/api/login") ? "密码不正确" : "登录已失效，请重新登录"));
    }
    if (!response.ok) {
      let message = payload.message;
      if (!message && payload.detail) {
        message = typeof payload.detail === "string" ? payload.detail : (payload.detail.message || JSON.stringify(payload.detail));
      }
      throw new Error(message || `请求失败（${response.status}）`);
    }
    return payload;
  }

  function showLogin(sessionLost = false) {
    $("login").classList.remove("hidden");
    $("app").classList.add("hidden");
    if (sessionLost) {
      $("login-error").textContent = "登录已失效；如已在另一台设备登录，请重新登录。";
    }
    setTimeout(() => $("password").focus(), 300);
  }

  function showApp() {
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
  }

  async function boot() {
    const session = await api("/api/session");
    if (!session.authenticated) {
      showLogin();
      return;
    }
    showApp();
    await loadDashboard();
    await loadNotes();
  }

  async function loadDashboard(week = state.week) {
    const query = week ? `?week=${encodeURIComponent(week)}` : "";
    const data = await api(`/api/dashboard${query}`);
    state.dashboard = data;
    state.week = data.week.id;
    renderWeek(data);
  }

  function renderWeek(data) {
    const week = data.week;
    $("week-chip").textContent = `${week.start_date.slice(5).replace("-", "/")}—${week.end_date.slice(5).replace("-", "/")}`;
    $("week-title").textContent = `第 ${week.start_date.replaceAll("-", ".")} 周`;
    $("week-sub").textContent = `${week.directory} · ${statusLabel[week.status] || week.status}${week.needs_sync ? " · 待重新上传" : ""}`;
    const select = $("week-select");
    select.innerHTML = data.weeks.map((item) => `<option value="${esc(item.id)}"${item.id === week.id ? " selected" : ""}>${esc(item.start_date.slice(5).replace("-", "/"))} — ${esc(item.end_date.slice(5).replace("-", "/"))} · ${esc(statusLabel[item.status] || item.status)}</option>`).join("");
    if (!$("publish-date").value) $("publish-date").value = todayISO();
    const stats = data.stats;
    $("week-stats").innerHTML = [
      ["组", stats.groups], ["照片", stats.photos], ["已入选", stats.selected], ["已发布", stats.published]
    ].map(([label, value]) => `<div class="stat"><b>${value}</b><small>${label}</small></div>`).join("");
    const alerts = [];
    if (week.selected_unpublished) {
      alerts.push(`<div class="alert danger"><strong>有 ${week.selected_unpublished} 组已入选但未发布</strong>周收束时会被显式阻断；请先发布，或取消入选后再归档。</div>`);
    }
    if (week.needs_sync) {
      alerts.push(`<div class="alert"><strong>本周需要重新同步到 123</strong>已上传后发生过入选/改名，点击“上传本周文件夹”会补齐新文件，并只清理明确对应的旧“未入选”文件。</div>`);
    }
    $("week-alerts").innerHTML = alerts.join("");
    renderGroups(data.groups);
    renderNotes(data.notes || []);
  }

  function renderGroups(groups) {
    const container = $("groups");
    $("groups-empty").classList.toggle("hidden", groups.length > 0);
    container.innerHTML = groups.map((group) => {
      const photos = group.photos || [];
      const count = photos.length;
      const countClass = count <= 1 ? "count-1" : count === 2 ? "count-2" : count === 3 ? "count-3" : count === 4 ? "count-4" : "count-many";
      const visible = photos.slice(0, count === 3 ? 3 : 4);
      const mosaic = visible.map((photo) => `
        <div class="photo" data-photo="${esc(photo.url)}" data-name="${esc(photo.name)}">
          <img src="${esc(photo.url)}" alt="${esc(group.title)}" loading="lazy">
          ${photo.missing_remote ? '<span class="more">飞书附件已移除，本地保留</span>' : ""}
        </div>`).join("") + (count > visible.length ? `<div class="photo"><div class="more">共 ${count} 张</div></div>` : "");
      const statusClass = group.status === "selected" ? "selected" : group.status === "published" ? "published" : group.status === "unselected" ? "unselected" : "collected";
      const warnings = (group.warnings || []).map((text) => `<div class="group-warn">${esc(text)}</div>`).join("");
      const quote = group.quote ? `<div class="quote">${esc(group.quote)}</div>` : `<div class="quote muted">摄影师未填写图说</div>`;
      const actions = [];
      if (group.status === "published") {
        actions.push(`<button class="btn secondary" data-action="open-note" data-id="${esc(group.note_id || "")}">查看笔记</button>`);
      } else if (group.status === "selected") {
        actions.push(`<button class="btn ghost" data-action="unselect" data-id="${esc(group.id)}">取消入选</button>`);
        actions.push(`<button class="btn ghost" data-action="edit" data-id="${esc(group.id)}">改图名/图说</button>`);
        actions.push(`<button class="btn ghost" data-action="order" data-id="${esc(group.id)}">调顺序</button>`);
      } else {
        actions.push(`<button class="btn primary" data-action="select" data-id="${esc(group.id)}">按组入选</button>`);
        actions.push(`<button class="btn ghost" data-action="edit" data-id="${esc(group.id)}">改图名/图说</button>`);
        actions.push(`<button class="btn ghost" data-action="order" data-id="${esc(group.id)}">调顺序</button>`);
      }
      return `
        <article class="group-card ${group.status === "selected" ? "selected" : ""} ${group.status === "published" ? "published" : ""}" data-group="${esc(group.id)}">
          <div class="photo-mosaic ${countClass}">${mosaic}</div>
          <div class="group-body">
            <div class="group-topline">
              <h3>${esc(group.title)}</h3>
              <span class="pill ${statusClass}">${statusLabel[group.status] || group.status}</span>
            </div>
            <div class="group-meta">
              <span>${esc(group.photographer)}${group.account ? ` · ${esc(group.account)}` : " · 未映射账号"}</span>
              <span>投稿 ${esc(group.submitted_at)}</span>
              <span>${count} 张</span>
              ${group.selected_date ? `<span>入选 ${esc(group.selected_date)}</span>` : ""}
            </div>
            ${quote}
            ${warnings}
          </div>
          <div class="group-actions">${actions.join("")}</div>
        </article>`;
    }).join("");
    const selected = groups.filter((group) => group.status === "selected");
    const bar = $("selection-bar");
    bar.classList.toggle("hidden", selected.length === 0);
    $("selection-count").textContent = `已选 ${selected.length} 组`;
    if (selected.length > 1) {
      const dates = [...new Set(selected.map((group) => group.selected_date))];
      $("selection-hint").textContent = dates.length === 1 ? "同主题可合并为一篇；标题由主编填写" : "所选组入选日期不同，不能合并";
    } else if (selected.length === 1) {
      $("selection-hint").textContent = "可直接组稿，或再勾选同主题组进行合并";
    }
    $("selection-summary").classList.toggle("hidden", selected.length === 0);
    $("selection-summary").textContent = selected.length > 1 ? `已选 ${selected.length} 组，可合并组稿` : (selected[0] ? `已入选：${selected[0].title}` : "");
  }

  async function loadNotes() {
    const data = await api("/api/notes");
    renderNotes(data.notes || []);
  }

  function renderNotes(notes) {
    state.notes = notes || [];
    $("notes-empty").classList.toggle("hidden", state.notes.length > 0);
    $("notes-list").innerHTML = state.notes.map((note) => {
      const image = note.images && note.images[0] ? `<img class="note-thumb" src="${esc(note.images[0].url)}" alt="">` : `<div class="note-thumb"></div>`;
      const groups = note.groups.map((group) => group.title).join(" / ");
      return `
        <article class="note-card" data-note="${esc(note.id)}">
          ${image}
          <div>
            <div class="group-topline"><h3>${esc(note.title)}</h3><span class="pill ${note.status === "published" ? "published" : "selected"}">${note.status === "published" ? "已发布" : "待发布"}</span></div>
            <p class="muted">${esc(note.day_number)} · ${esc(note.publish_date)} · ${note.images.length} 图 · ${esc(groups)}</p>
            <div class="note-body-preview">${esc(note.content)}</div>
          </div>
          <div class="inline-actions">
            <button class="btn secondary small" data-action="open-note" data-id="${esc(note.id)}">打开</button>
          </div>
        </article>`;
    }).join("");
  }

  function openModal({ title, eyebrow = "操作", body = "", footer = "" }) {
    $("modal-title").textContent = title;
    $("modal-eyebrow").textContent = eyebrow;
    $("modal-body").innerHTML = body;
    $("modal-footer").innerHTML = footer;
    $("modal").classList.remove("hidden");
  }

  function closeModal() {
    $("modal").classList.add("hidden");
    state.currentNote = null;
    state.reorderGroup = null;
  }

  async function openGroupEdit(groupId) {
    const group = findGroup(groupId);
    if (!group) return;
    openModal({
      title: "编辑图名与图说",
      eyebrow: "投稿组",
      body: `
        <label class="field"><span>作品名（组内共用）</span><input id="edit-title" value="${esc(group.title)}"></label>
        <label class="field"><span>图说（可含 emoji 与换行，系统不做内容审核）</span><textarea id="edit-quote" placeholder="摄影师未填写可留空">${esc(group.quote)}</textarea></label>
        <p class="muted small">无论是否入选，整组文件会按同一套命名规则重排；如与其他组冲突，系统会拒绝覆盖并提示。</p>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn primary" id="save-group">保存并重命名</button>`,
    });
    $("save-group").onclick = async () => {
      try {
        await api(`/api/groups/${encodeURIComponent(groupId)}`, {
          method: "PATCH",
          body: { title: $("edit-title").value, quote: $("edit-quote").value },
        });
        toast("已保存");
        closeModal();
        await loadDashboard();
      } catch (error) {
        toast(error.message, true);
      }
    };
  }

  function openReorder(groupId) {
    const group = findGroup(groupId);
    if (!group) return;
    state.reorderGroup = group;
    state.reorderIds = group.photos.map((photo) => photo.id);
    renderReorderModal();
  }

  function renderReorderModal() {
    const group = state.reorderGroup;
    if (!group) return;
    const items = state.reorderIds.map((id, index) => {
      const photo = group.photos.find((item) => item.id === id);
      return `<div class="topic-row"><span><b>${index + 1}</b> · ${esc(photo ? photo.name : id)}</span>
        <span class="inline-actions"><button class="btn ghost small" data-move="-1" data-id="${esc(id)}">上移</button><button class="btn ghost small" data-move="1" data-id="${esc(id)}">下移</button></span></div>`;
    }).join("");
    openModal({
      title: "调整组内顺序",
      eyebrow: "组图",
      body: `<p class="muted small">多图文件名序号按这里的顺序生成；保存后会整组重命名。</p><div class="stack">${items}</div>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn primary" id="save-order">保存顺序</button>`,
    });
    document.querySelectorAll("[data-move]").forEach((button) => {
      button.onclick = () => {
        const id = button.dataset.id;
        const index = state.reorderIds.indexOf(id);
        const next = index + Number(button.dataset.move);
        if (next < 0 || next >= state.reorderIds.length) return;
        state.reorderIds.splice(index, 1);
        state.reorderIds.splice(next, 0, id);
        renderReorderModal();
      };
    });
    $("save-order").onclick = async () => {
      try {
        await api(`/api/groups/${encodeURIComponent(group.id)}/order`, {
          method: "PUT",
          body: { photo_ids: state.reorderIds },
        });
        toast("顺序已保存");
        closeModal();
        await loadDashboard();
      } catch (error) {
        toast(error.message, true);
      }
    };
  }

  function findGroup(groupId) {
    return (state.dashboard?.groups || []).find((group) => group.id === groupId);
  }

  async function selectGroup(groupId) {
    const date = $("publish-date").value || todayISO();
    try {
      const result = await api(`/api/groups/${encodeURIComponent(groupId)}/select`, {
        method: "POST",
        body: { publish_date: date },
      });
      toast(result.warning || "已入选；整组文件已追加入选后缀");
      await loadDashboard();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function unselectGroup(groupId) {
    openModal({
      title: "取消入选",
      eyebrow: "确认",
      body: `<p>取消后整组会移除入选后缀；如果本周已收束或已上传，会改名为“未入选”，并在下次上传时同步云端。</p>`,
      footer: `<button class="btn ghost" data-close>保留</button><button class="btn danger" id="confirm-unselect">取消入选</button>`,
    });
    $("confirm-unselect").onclick = async () => {
      try {
        await api(`/api/groups/${encodeURIComponent(groupId)}/unselect`, { method: "POST" });
        toast("已取消入选");
        closeModal();
        await loadDashboard();
      } catch (error) {
        toast(error.message, true);
      }
    };
  }

  function openCompose() {
    const selected = (state.dashboard?.groups || []).filter((group) => group.status === "selected");
    if (!selected.length) {
      toast("请先在投稿组上点“按组入选”", true);
      return;
    }
    const dates = [...new Set(selected.map((group) => group.selected_date))];
    if (dates.length !== 1) {
      toast("所选组的入选日期不同，不能合并", true);
      return;
    }
    const publishDate = dates[0];
    const merged = selected.length > 1;
    openModal({
      title: merged ? "合并组稿" : "生成笔记",
      eyebrow: merged ? `${selected.length} 组 · 同一篇` : "单组",
      body: `
        <label class="field"><span>${merged ? "笔记标题（必填，主编拟定）" : "标题（默认作品名，可修改）"}</span><input id="compose-title" value="${merged ? "" : esc(selected[0].title)}" placeholder="${merged ? "例如：雨后的成电，两种逆光" : ""}"></label>
        <div class="form-grid">
          <label class="field"><span>发布日期</span><input id="compose-date" value="${esc(publishDate)}" readonly></label>
          <label class="field"><span>dayNNNN</span><input id="compose-day" placeholder="留空自动计算"></label>
          <label class="field full"><span>动态话题（用逗号分隔，可留空稍后实时抓取）</span><input id="compose-topics" placeholder="例如：校园摄影, 成都, 逆光"></label>
        </div>
        <div class="stack">${selected.map((group) => `<div class="topic-row"><span><b>${esc(group.title)}</b><br><small>${esc(group.photographer)} · ${group.photos.length} 图 · ${esc(group.quote ? group.quote.slice(0, 38) : "无图说")}</small></span></div>`).join("")}</div>
        <p class="muted small">创建后仍可在笔记详情里刷新实时话题、编辑正文，再生成草稿。</p>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn primary" id="create-note">创建并预览</button>`,
    });
    $("create-note").onclick = async () => {
      try {
        const topics = $("compose-topics").value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
        const note = await api("/api/notes", {
          method: "POST",
          body: {
            group_ids: selected.map((group) => group.id),
            title: $("compose-title").value,
            publish_date: publishDate,
            day: $("compose-day").value || null,
            dynamic_topics: topics,
          },
        });
        closeModal();
        await openNote(note.id);
        await loadNotes();
      } catch (error) {
        if (error.message.includes("已被其他笔记占用")) {
          closeModal();
          try { await loadNotes(); } catch (_) {}
          toast("该组已经生成过笔记，已刷新到笔记列表；请直接打开已有笔记");
          return;
        }
        toast(error.message, true);
      }
    };
  }

  async function openNote(noteId) {
    try {
      const note = await api(`/api/notes/${encodeURIComponent(noteId)}`);
      state.currentNote = note;
      renderNoteModal(note);
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderNoteModal(note, notice = "") {
    const published = note.status === "published";
    const topicRows = (note.topics || []).map((topic) => `
      <label class="topic-row">
        <span><b>${esc(topic.name)}</b><br><small>${topic.views_raw ? `浏览 ${esc(topic.views_raw)}` : "浏览量未取得"} · ${esc(topic.source || "候选")}</small></span>
        <input type="checkbox" data-topic="${esc(topic.name)}" ${topic.adopted ? "checked" : ""}>
      </label>`).join("");
    const body = `
      ${notice ? `<div class="alert ok">${esc(notice)}</div>` : ""}
      <div class="preview-photos">${(note.images || []).map((image) => `<img src="${esc(image.url)}" data-photo="${esc(image.url)}" alt="${esc(image.name)}">`).join("")}</div>
      <label class="field"><span>标题</span><input id="note-title" value="${esc(note.title)}" ${published ? "readonly" : ""}></label>
      <div class="form-grid">
        <label class="field"><span>dayNNNN</span><input id="note-day" value="${esc(note.day_number)}" ${published ? "readonly" : ""}></label>
        <label class="field"><span>发布日期 / 账号</span><input value="${esc(note.publish_date)} · ${esc(note.account || "账号未配置")}" readonly></label>
      </div>
      <label class="field"><span>正文（最后一行必须是标签）</span><textarea id="note-content" ${published ? "readonly" : ""}>${esc(note.content)}</textarea></label>
      <div class="section-head tight"><div><h3>话题</h3><p class="muted small">固定标签自动保留；动态话题每次实时读取浏览量，失败不静默用旧缓存。</p></div><button class="btn ghost small" id="refresh-topics" ${published ? "disabled" : ""}>刷新实时话题</button></div>
      <div class="stack">${topicRows || '<p class="muted small">尚无候选话题；先刷新实时话题，或手动勾选下方输入。</p>'}</div>
      <label class="field"><span>手动补充动态话题（逗号分隔）</span><input id="manual-topics" placeholder="只写动态话题，不要写固定标签" ${published ? "disabled" : ""}></label>
      ${(note.warnings || []).map((text) => `<div class="alert">${esc(text)}</div>`).join("")}
      <p class="muted small">草稿与发布均在项目 Chrome 的小红书后台执行；手机端可在小红书 App 草稿箱查看。发布结果不确定时只提示人工核对，不会自动重试。</p>`;
    const footer = published
      ? `<button class="btn ghost" data-close>关闭</button><button class="btn secondary" id="open-native">打开后台</button>`
      : `<button class="btn ghost" data-close>关闭</button>
         <button class="btn ghost" id="save-note">保存编辑</button>
         <button class="btn ghost" id="save-topics">保存话题</button>
         <button class="btn secondary" id="make-draft">生成草稿</button>
         <button class="btn secondary" id="open-native">打开后台</button>
         <button class="btn primary" id="publish-note">发布</button>`;
    openModal({ title: "笔记预览", eyebrow: note.status === "published" ? "已发布" : "发布前复核", body, footer });
    state.currentNote = note;
    if ($("save-note")) {
      $("save-note").onclick = async () => {
        try {
          const updated = await api(`/api/notes/${encodeURIComponent(note.id)}`, {
            method: "PATCH",
            body: { title: $("note-title").value, content: $("note-content").value, day: $("note-day").value },
          });
          state.currentNote = updated;
          toast("正文已保存");
          renderNoteModal(updated);
        } catch (error) {
          toast(error.message, true);
        }
      };
    }
    if ($("save-topics")) {
      $("save-topics").onclick = async () => {
        try {
          const names = [...document.querySelectorAll("[data-topic]:checked")].map((input) => input.dataset.topic);
          const manual = $("manual-topics").value.split(/[,，]/).map((item) => item.trim().replace(/^#/, "")).filter(Boolean);
          const result = await api(`/api/notes/${encodeURIComponent(note.id)}/topics`, {
            method: "PUT",
            body: { names: [...names, ...manual] },
          });
          state.currentNote = result.note;
          toast(result.warning || "话题已保存");
          renderNoteModal(result.note);
        } catch (error) {
          toast(error.message, true);
        }
      };
    }
    if ($("refresh-topics")) {
      $("refresh-topics").onclick = async () => {
        const button = $("refresh-topics");
        button.disabled = true;
        button.textContent = "读取中…";
        try {
          const result = await api(`/api/notes/${encodeURIComponent(note.id)}/topics/refresh`, { method: "POST" });
          if (!result.ok) {
            toast(result.error || "实时话题读取失败，请手动填写", true);
            button.disabled = false;
            button.textContent = "刷新实时话题";
            return;
          }
          state.currentNote = result.note;
          toast(result.error || "已按实时浏览量自动选择高浏览话题");
          renderNoteModal(result.note, "已刷新实时话题并更新正文最后一行。");
        } catch (error) {
          toast(error.message, true);
          button.disabled = false;
          button.textContent = "刷新实时话题";
        }
      };
    }
    if ($("make-draft")) {
      $("make-draft").onclick = async () => {
        try {
          const result = await api(`/api/notes/${encodeURIComponent(note.id)}/draft`, { method: "POST" });
          toast("草稿已保存到小红书草稿箱");
          state.currentNote = result.note;
          renderNoteModal(result.note, "草稿已生成。请在后台预览无误后再发布；重复生成会新增独立草稿。");
        } catch (error) {
          toast(error.message, true);
        }
      };
    }
    if ($("open-native")) {
      $("open-native").onclick = async () => {
        try {
          const result = await api(`/api/notes/${encodeURIComponent(note.id)}/open-native`, { method: "POST" });
          if (result.opened) {
            toast("已在项目 Chrome 打开小红书后台；手机端可在 App 草稿箱查看");
            window.open(result.native_url, "_blank", "noopener");
          } else {
            toast("已在项目 Chrome 打开草稿箱；请人工确认定位到的草稿", true);
            window.open(result.native_url, "_blank", "noopener");
          }
        } catch (error) {
          toast(error.message, true);
        }
      };
    }
    if ($("publish-note")) {
      $("publish-note").onclick = () => confirmPublish(note);
    }
  }

  function confirmPublish(note) {
    openModal({
      title: "确认发布",
      eyebrow: "最后一步",
      body: `
        <div class="alert danger"><strong>请在后台预览后再确认</strong>发布由服务端复核标题、正文、图片数与话题，然后点击小红书原生发布按钮。结果不确定时只提示人工核对，不自动重试。</div>
        <div class="preview-title">${esc(note.title)}</div>
        <div class="preview-text">${esc(note.content)}</div>
        <label class="check"><input id="publish-confirm" type="checkbox"><span>我已在后台预览，确认标题、正文、图片和话题无误</span></label>`,
      footer: `<button class="btn ghost" data-close>返回复核</button><button class="btn primary" id="do-publish" disabled>确认发布</button>`,
    });
    $("publish-confirm").onchange = (event) => {
      $("do-publish").disabled = !event.target.checked;
    };
    $("do-publish").onclick = async () => {
      $("do-publish").disabled = true;
      $("do-publish").textContent = "发布中…";
      try {
        const result = await api(`/api/notes/${encodeURIComponent(note.id)}/publish`, {
          method: "POST",
          body: { confirmed: true },
        });
        if (result.ok) {
          toast("已发布");
          closeModal();
          await loadNotes();
          await loadDashboard();
        } else {
          toast(result.message || "结果不确定，请人工核对", true);
          $("do-publish").textContent = "结果不确定";
        }
      } catch (error) {
        toast(error.message, true);
        $("do-publish").disabled = false;
        $("do-publish").textContent = "重试发布";
      }
    };
  }

  function openImage(url, name) {
    openModal({
      title: name || "图片预览",
      eyebrow: "原图",
      body: `<div class="photo-viewer"><img src="${esc(url)}" alt="${esc(name || "")}"></div>`,
      footer: `<button class="btn ghost" data-close>关闭</button>`,
    });
  }

  async function loadUpload() {
    const weekId = state.week;
    const tasks = await api(`/api/upload-tasks${weekId ? `?week=${encodeURIComponent(weekId)}` : ""}`);
    const settings = state.settings || await api("/api/settings");
    state.settings = settings;
    $("upload-root").innerHTML = `当前 123 年度根目录：<b>${esc(settings.pan123_root)}</b>`;
    const alerts = [];
    if (state.dashboard?.week.needs_sync) alerts.push(`<div class="alert"><strong>本周有新变化待上传</strong>新入选/改名后会先上传新文件并核验，再只清理明确对应的旧“未入选”文件。</div>`);
    if (state.dashboard?.week.selected_unpublished) alerts.push(`<div class="alert danger"><strong>仍有 ${state.dashboard.week.selected_unpublished} 组已入选未发布</strong>收束会被阻断，刷新也不会清除该提醒。</div>`);
    $("upload-alerts").innerHTML = alerts.join("");
    const list = tasks.tasks || [];
    $("upload-tasks").innerHTML = list.length ? list.map((task) => `
      <div class="task-card">
        <div class="task-head">
          <div><h3>${esc(task.id)}</h3><span class="pill ${taskTone[task.status] || "neutral"}">${statusLabel[task.status] || task.status}</span></div>
          <span class="muted small">第 ${task.attempt} 次 · ${esc(task.created_at)}</span>
        </div>
        <div class="task-meta"><span>根目录 ${esc(task.root_path)}</span><span>文件 ${task.items.length} 个</span>${task.finished_at ? `<span>完成 ${esc(task.finished_at)}</span>` : ""}</div>
        ${task.summary?.remote_url ? `<p class="muted small">云盘：${esc(task.summary.remote_url)}</p>` : ""}
        ${task.error ? `<p class="error">${esc(task.error)}</p>` : ""}
        <div class="task-items">${task.items.map((item) => `<span>${esc(item.filename)}${item.status === "verified" ? " · 已核验" : ""}</span>`).join("")}</div>
        ${task.status === "failed" || task.status === "uploaded_with_cleanup_pending" ? `<div class="inline-actions" style="margin-top:.7rem"><button class="btn secondary small" data-retry="${esc(task.id)}">重试</button></div>` : ""}
      </div>`).join("") : `<div class="empty-state"><h3>本周还没有上传任务</h3><p>周收束无阻断后，点“上传本周文件夹”。</p></div>`;
    document.querySelectorAll("[data-retry]").forEach((button) => {
      button.onclick = async () => {
        try {
          await api(`/api/upload-tasks/${encodeURIComponent(button.dataset.retry)}/retry`, { method: "POST" });
          toast("已重新排队");
          await loadUpload();
        } catch (error) {
          toast(error.message, true);
        }
      };
    });
  }

  async function closeWeek() {
    if (!state.week) return;
    openModal({
      title: "收束本周",
      eyebrow: "状态检查",
      body: `<p>系统会先检查是否有“已入选但未发布”的组；若有则显式阻断，绝不静默上传。通过后，仍未入选/未发布的组会改名为“未入选”。</p>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn primary" id="do-close">开始收束</button>`,
    });
    $("do-close").onclick = async () => {
      try {
        const result = await api(`/api/weeks/${encodeURIComponent(state.week)}/close`, { method: "POST" });
        if (!result.ok) {
          toast(result.message || "收束被阻断", true);
          closeModal();
          await loadDashboard();
          await loadUpload();
          return;
        }
        toast("本周已收束");
        closeModal();
        await loadDashboard();
        await loadUpload();
      } catch (error) {
        toast(error.message, true);
      }
    };
  }

  async function uploadWeek() {
    if (!state.week) return;
    openModal({
      title: "上传本周文件夹",
      eyebrow: "123 云盘",
      body: `<p>系统会先收束并检查阻断，然后把整个周目录上传到年度根目录，回读核验文件数。上传失败会持久记录，刷新不会消失。</p><p class="muted small">年度根目录：${esc((state.settings || {}).pan123_root || "")}</p>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn primary" id="do-upload">开始上传</button>`,
    });
    $("do-upload").onclick = async () => {
      $("do-upload").disabled = true;
      $("do-upload").textContent = "排队中…";
      try {
        const task = await api(`/api/weeks/${encodeURIComponent(state.week)}/upload`, {
          method: "POST",
          body: { allow_current: false },
        });
        toast("上传任务已排队");
        closeModal();
        await loadUpload();
        pollTasks();
      } catch (error) {
        toast(error.message, true);
        $("do-upload").disabled = false;
        $("do-upload").textContent = "开始上传";
      }
    };
  }

  let pollTimer = null;
  function pollTasks() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const data = await api(`/api/upload-tasks${state.week ? `?week=${encodeURIComponent(state.week)}` : ""}`);
        const active = data.tasks.some((task) => ["queued", "running"].includes(task.status));
        if (active) {
          await loadUpload();
          pollTasks();
        } else {
          await loadUpload();
        }
      } catch (_) { /* 登录失效已由 api 处理 */ }
    }, 3500);
  }

  async function loadSettings() {
    const settings = await api("/api/settings");
    state.settings = settings;
    const form = $("settings-form");
    form.pan123_root.value = settings.pan123_root;
    form.fixed_tags.value = (settings.fixed_tags || []).join(", ");
    $("path-list").innerHTML = [
      ["数据根", settings.data_root],
      ["周目录父路径", settings.archive_parent],
      ["状态目录", settings.state_root],
      ["账号 CSV", settings.roster_path || "未配置"],
      ["数据库", settings.db_path],
    ].map(([label, value]) => `<div><span>${esc(label)}</span><code>${esc(value)}</code></div>`).join("");
    form.upload_weekday.value = String(settings.upload_weekday);
    form.upload_hour.value = settings.upload_hour;
    form.upload_minute.value = settings.upload_minute;
    form.dynamic_topic_count.value = settings.dynamic_topic_count;
    form.max_topics.value = settings.max_topics;
    form.auto_start.checked = settings.auto_start;
    form.xhs_account_label.value = settings.xhs_account_label || "";
    form.querySelectorAll("input[name=publish_mode]").forEach((radio) => {
      radio.checked = radio.value === settings.publish_mode;
    });
    form.official_publish_authorized.checked = settings.official_publish_authorized;
    $("auto-status").textContent = settings.auto_start ? "清单已启用" : "未启用";
    $("auto-status").className = "pill " + (settings.auto_start ? "published" : "neutral");
    $("autostart-list").innerHTML = settings.autostart_checklist.map((item) => `
      <div class="checklist-item"><b>${esc(item.item)}</b><p>${esc(item.action)}</p><p>${esc(item.verification)}</p></div>`).join("");
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = $("settings-form");
    try {
      const payload = {
        pan123_root: form.pan123_root.value,
        fixed_tags: form.fixed_tags.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
        upload_weekday: Number(form.upload_weekday.value),
        upload_hour: Number(form.upload_hour.value),
        upload_minute: Number(form.upload_minute.value),
        dynamic_topic_count: Number(form.dynamic_topic_count.value),
        max_topics: Number(form.max_topics.value),
        auto_start: form.auto_start.checked,
        publish_mode: form.querySelector("input[name=publish_mode]:checked").value,
        official_publish_authorized: form.official_publish_authorized.checked,
        xhs_account_label: form.xhs_account_label.value,
      };
      const settings = await api("/api/settings", { method: "PUT", body: payload });
      state.settings = settings;
      $("settings-msg").textContent = "已保存。";
      toast("设置已保存");
      await loadSettings();
    } catch (error) {
      $("settings-msg").textContent = error.message;
      toast(error.message, true);
    }
  }

  async function changePassword(event) {
    event.preventDefault();
    const form = $("password-form");
    try {
      await api("/api/settings/password", {
        method: "POST",
        body: { current: form.current.value, new: form.new.value },
      });
      form.reset();
      $("password-msg").textContent = "密码已更新；当前设备已接管会话。";
      toast("密码已更新");
    } catch (error) {
      $("password-msg").textContent = error.message;
    }
  }

  async function loadAccounts() {
    const data = await api("/api/accounts");
    $("accounts-box").innerHTML = data.accounts.length
      ? data.accounts.map((account) => `<span>${esc(account.photographer)} → ${esc(account.handle)}</span>`).join("")
      : `<p class="muted small">暂无映射；导入 CSV 后按姓名精确匹配。</p>`;
  }

  async function loadAudit() {
    const data = await api("/api/audit");
    $("audit-list").innerHTML = (data.items || []).map((item) => `
      <div class="audit-row"><b>${esc(item.action)}</b><span>${esc(item.actor)}</span><span>${esc(item.target)}</span><span>${esc(item.created_at)}</span></div>`).join("") || `<p class="muted small">暂无审计记录</p>`;
  }

  async function checkHealth() {
    const box = $("health-box");
    box.innerHTML = `<div class="alert">检查中…</div>`;
    try {
      const data = await api("/api/health/detail");
      const items = [`数据库：${data.ok ? "正常" : "异常"}`, `路径：${esc(data.db)}`];
      if (data.xhs) items.push(`小红书：${data.xhs.reachable ? (data.xhs.logged_in ? "已登录" : "CDP 可达但未登录") : "不可达"}`);
      if (data.adapter_error) items.push(`适配器：${esc(data.adapter_error)}`);
      box.innerHTML = `<div class="alert ${data.ok ? "ok" : "danger"}"><strong>连接检查</strong>${items.join("<br>")}</div>`;
    } catch (error) {
      box.innerHTML = `<div class="alert danger">${esc(error.message)}</div>`;
    }
  }

  function setTab(name) {
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
    document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${name}`));
    if (name === "upload") loadUpload().catch((error) => toast(error.message, true));
    if (name === "settings") {
      loadSettings().catch((error) => toast(error.message, true));
      loadAudit().catch(() => {});
      loadAccounts().catch(() => {});
    }
  }

  // ---- 事件绑定 -------------------------------------------------
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-error").textContent = "";
    try {
      await api("/api/login", { method: "POST", body: { password: $("password").value } });
      $("password").value = "";
      showApp();
      state.week = null;
      await boot();
    } catch (error) {
      $("login-error").textContent = error.message;
    }
  });

  $("logout-btn").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST" }); } catch (_) { /* ignore */ }
    showLogin();
  });

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => setTab(tab.dataset.tab)));

  $("week-select").addEventListener("change", async (event) => {
    try { await loadDashboard(event.target.value); } catch (error) { toast(error.message, true); }
  });

  $("sync-btn").addEventListener("click", () => {
    openModal({
      title: "同步飞书投稿",
      eyebrow: "只读拉取",
      body: `<p>将按当前所选周拉取飞书记录，按“一条记录=一个组”下载全部附件，并更新本地目录与命名。不会修改飞书表格。</p><p class="muted small">如果飞书附件仍在变化，重复同步会补齐/修正缺失文件。</p>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn primary" id="do-sync">开始同步</button>`,
    });
    $("do-sync").onclick = async () => {
      $("do-sync").disabled = true;
      $("do-sync").textContent = "同步中…";
      try {
        const result = await api("/api/sync", { method: "POST", body: { week: state.week } });
        toast(`同步完成：${result.groups} 组，新增 ${result.photos_added} 张`);
        closeModal();
        await loadDashboard();
      } catch (error) {
        toast(error.message, true);
        closeModal();
      }
    };
  });

  $("groups").addEventListener("click", (event) => {
    const photo = event.target.closest("[data-photo]");
    if (photo) { openImage(photo.dataset.photo, photo.dataset.name); return; }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    if (button.dataset.action === "select") selectGroup(id);
    if (button.dataset.action === "unselect") unselectGroup(id);
    if (button.dataset.action === "edit") openGroupEdit(id);
    if (button.dataset.action === "order") openReorder(id);
    if (button.dataset.action === "open-note" && id) openNote(id);
  });

  $("notes-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-action=open-note]");
    if (button) openNote(button.dataset.id);
  });

  $("compose-btn").addEventListener("click", openCompose);
  $("new-note-btn").addEventListener("click", openCompose);
  $("clear-selection").addEventListener("click", async () => {
    const selected = (state.dashboard?.groups || []).filter((group) => group.status === "selected");
    if (!selected.length) return;
    openModal({
      title: "清空已选组",
      eyebrow: "批量操作",
      body: `<p>将取消 ${selected.length} 组的入选。已发布组不受影响；已收束/已上传周会改名为“未入选”。</p>`,
      footer: `<button class="btn ghost" data-close>取消</button><button class="btn danger" id="do-clear">全部取消入选</button>`,
    });
    $("do-clear").onclick = async () => {
      try {
        for (const group of selected) {
          await api(`/api/groups/${encodeURIComponent(group.id)}/unselect`, { method: "POST" });
        }
        toast("已清空");
        closeModal();
        await loadDashboard();
      } catch (error) {
        toast(error.message, true);
      }
    };
  });

  $("close-week-btn").addEventListener("click", closeWeek);
  $("upload-week-btn").addEventListener("click", uploadWeek);

  $("settings-form").addEventListener("submit", saveSettings);
  $("password-form").addEventListener("submit", changePassword);
  $("accounts-btn").addEventListener("click", () => loadAccounts().catch((error) => toast(error.message, true)));
  $("import-accounts-btn").addEventListener("click", async () => {
    try {
      const result = await api("/api/accounts/import", { method: "POST" });
      toast(`已导入 ${result.imported} 条账号映射`);
      await loadAccounts();
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("audit-btn").addEventListener("click", () => loadAudit().catch((error) => toast(error.message, true)));
  $("health-btn").addEventListener("click", checkHealth);

  $("modal").addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) closeModal();
    const photo = event.target.closest("[data-photo]");
    if (photo) openImage(photo.dataset.photo, "");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("modal").classList.contains("hidden")) closeModal();
  });

  boot().catch((error) => {
    console.error(error);
    showLogin();
    $("login-error").textContent = error.message;
  });
})();
