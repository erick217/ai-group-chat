(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    conn: $("conn"), members: $("members"), memberCount: $("member-count"), scheduler: $("scheduler"),
    maxTurns: $("max-turns"), messages: $("messages"), empty: $("empty"), input: $("input"),
    send: $("btn-send"), stop: $("btn-stop"), cont: $("btn-continue"), reload: $("btn-reload"),
    clear: $("btn-clear"), status: $("status-text"), sidebar: $("sidebar"), menu: $("btn-menu"),
    pop: $("mention-pop"), toast: $("toast"), template: $("template"), templateDesc: $("template-desc"),
    phases: $("phases"), search: $("search"), searchLabel: $("search-label"), chatTitle: $("chat-title"),
    emptyIcon: $("empty-icon"), emptyText: $("empty-text"), emptyTitle: $("empty-title"), examples: $("examples"),
    confirm: $("confirm"), confirmTitle: $("confirm-title"), confirmText: $("confirm-text"), confirmOk: $("confirm-ok"),
  };
  const SKELETON = `<div class="skeleton" aria-label="正在输入"><span></span><span></span><span></span></div>`;

  let ws = null;
  let agents = [];
  let templates = [];
  let currentTemplate = "free";
  let running = false;
  const nodes = new Map();   // message id -> { el, bubble, raw, sources, role, error }
  const renderQueued = new Set();

  // ---------- WebSocket ----------
  function connect() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { els.conn.classList.add("on"); els.conn.title = "已连接"; };
    ws.onclose = () => { els.conn.classList.remove("on"); els.conn.title = "连接已断开，正在重连"; setTimeout(connect, 1500); };
    ws.onmessage = (e) => handle(JSON.parse(e.data));
  }
  const send = (obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

  function handle(ev) {
    switch (ev.type) {
      case "state":
        agents = ev.agents;
        templates = ev.templates;
        currentTemplate = ev.template;
        renderTemplates();
        renderMembers();
        renderSchedulers(ev.schedulers, ev.settings);
        els.search.disabled = !ev.search_available;
        els.searchLabel.textContent = ev.search_available ? ev.search_label : "未配置";
        els.messages.querySelectorAll(".msg").forEach((n) => n.remove());
        nodes.clear();
        els.empty.hidden = false;
        ev.history.forEach(addMessage);
        setStatus(ev);
        scrollBottom(true);
        break;
      case "message": addMessage(ev.message); scrollBottom(); break;
      case "delta": appendDelta(ev.id, ev.text); break;
      case "message_meta": setSources(ev.id, ev.queries, ev.sources); break;
      case "message_done": finishMessage(ev.message); break;
      case "status": setStatus(ev); break;
      case "settings": renderSchedulers(null, ev.settings); break;
      case "notice": toast(ev.text, ev.level); break;
    }
  }

  // ---------- 模板 / 成员 / 设置 ----------
  const agentOf = (name) => agents.find((a) => a.name === name);
  const tplOf = (key) => templates.find((t) => t.key === key) || templates[0];

  function renderTemplates() {
    els.template.innerHTML = "";
    for (const t of templates) {
      const o = document.createElement("option");
      o.value = t.key;
      o.textContent = `${t.icon} ${t.name}`;
      els.template.appendChild(o);
    }
    const t = tplOf(currentTemplate);
    els.template.value = t.key;
    els.templateDesc.textContent = t.description;
    els.chatTitle.textContent = `${t.icon} ${t.name}`;
    els.emptyIcon.textContent = t.icon;
    els.emptyTitle.textContent = t.phases.length ? t.name : "开始一场讨论";
    els.emptyText.textContent = t.phases.length ? t.description : "提一个问题，看看群里的 AI 们怎么讨论。";
    els.input.placeholder = t.placeholder || "说点什么…";
    els.examples.innerHTML = "";
    for (const ex of t.examples || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "example";
      b.innerHTML = `<i class="ph ph-arrow-bend-down-right" aria-hidden="true"></i><span></span>`;
      b.querySelector("span").textContent = ex;
      b.title = ex;
      b.onclick = () => { els.input.value = ex; autosize(); els.input.focus(); };
      els.examples.appendChild(b);
    }
    els.phases.innerHTML = "";
    t.phases.forEach((name, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="dot"></span><span class="label"></span>`;
      li.querySelector(".dot").textContent = i + 1;
      li.querySelector(".label").textContent = name;
      els.phases.appendChild(li);
    });
  }
  els.template.onchange = async () => {
    const key = els.template.value;
    if (nodes.size > 0 && !(await ask("切换模板", "切换模板会清空当前的聊天记录。", "切换并清空"))) {
      els.template.value = currentTemplate;
      return;
    }
    send({ type: "template", key });
  };

  // 确认框（替代 window.confirm）
  function ask(title, text, okLabel) {
    els.confirmTitle.textContent = title;
    els.confirmText.textContent = text;
    els.confirmOk.textContent = okLabel || "确定";
    els.confirm.returnValue = "";
    els.confirm.showModal();
    return new Promise((resolve) => {
      els.confirm.addEventListener("close", () => resolve(els.confirm.returnValue === "ok"), { once: true });
    });
  }

  function renderPhase(phase) {
    const items = [...els.phases.children];
    items.forEach((li, i) => {
      const done = !!phase && i < phase.index;
      li.classList.toggle("done", done);
      li.classList.toggle("current", !!phase && i === phase.index);
      li.querySelector(".dot").innerHTML = done ? `<i class="ph ph-check" aria-hidden="true"></i>` : String(i + 1);
    });
  }

  function renderMembers() {
    els.memberCount.textContent = `(${agents.length})`;
    els.members.innerHTML = "";
    for (const a of agents) {
      const li = document.createElement("li");
      li.className = "member";
      li.dataset.name = a.name;
      li.style.setProperty("--c", a.color);
      li.innerHTML = `<div class="avatar"></div><div class="info"><div class="name"><span></span><i class="live" aria-hidden="true"></i></div><div class="bio"></div><div class="model"></div></div>`;
      li.querySelector(".avatar").textContent = a.avatar;
      li.querySelector(".name span").textContent = a.name;
      li.querySelector(".bio").textContent = a.bio;
      li.querySelector(".model").textContent = a.model;
      li.title = `${a.provider} / ${a.model}`;
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      li.setAttribute("aria-label", `@${a.name}`);
      li.onclick = () => { insertMention(a.name); setMenu(false); };
      li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); li.onclick(); } };
      els.members.appendChild(li);
    }
  }

  function renderSchedulers(labels, settings) {
    if (labels) {
      els.scheduler.innerHTML = "";
      for (const [k, v] of Object.entries(labels)) {
        const o = document.createElement("option");
        o.value = k; o.textContent = v;
        els.scheduler.appendChild(o);
      }
    }
    els.scheduler.value = settings.scheduler;
    els.maxTurns.value = settings.max_turns;
    els.search.checked = !!settings.search;
  }
  els.scheduler.onchange = () => send({ type: "settings", scheduler: els.scheduler.value });
  els.maxTurns.onchange = () => send({ type: "settings", max_turns: els.maxTurns.value });
  els.search.onchange = () => send({ type: "settings", search: els.search.checked });

  function setStatus(s) {
    running = s.running;
    els.stop.hidden = !running;
    els.cont.hidden = running;
    const parts = [];
    if (s.phase && s.phase.name) parts.push(`阶段 ${s.phase.index + 1}/${s.phase.count} · ${s.phase.name}`);
    if (running) {
      if (s.speaking) parts.push(s.activity === "searching" ? `${s.speaking} 正在搜索…` : `${s.speaking} 正在输入…`);
      else parts.push("讨论中…");
    } else if (!parts.length) parts.push("空闲");
    els.status.textContent = parts.join(" · ");
    renderPhase(s.phase);
    els.members.querySelectorAll(".member").forEach((li) => li.classList.toggle("speaking", li.dataset.name === s.speaking));
    if (s.speaking && s.activity === "searching") {
      for (const n of nodes.values()) {
        if (n.pending && n.speaker === s.speaking && !n.raw) n.bubble.innerHTML = `<span class="searching"><i class="ph ph-magnifying-glass" aria-hidden="true"></i>正在联网搜索…</span>`;
      }
    }
  }

  // ---------- 消息渲染 ----------
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMarkdown(text, sources) {
    let html = window.marked ? marked.parse(text, { breaks: true }) : escapeHtml(text).replace(/\n/g, "<br>");
    html = window.DOMPurify ? DOMPurify.sanitize(html) : html;
    const names = agents.map((a) => a.name).sort((a, b) => b.length - a.length);
    if (names.length) {
      const re = new RegExp(`@(${names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
      html = html.replace(re, '<span class="mention">@$1</span>');
    }
    if (sources && sources.length) {  // [1] → 指向资料的上标链接
      html = html.replace(/\[(\d{1,2})\]/g, (m, n) => {
        const s = sources[Number(n) - 1];
        return s ? `<sup class="cite"><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener" title="${escapeHtml(s.title)}">[${n}]</a></sup>` : m;
      });
    }
    return html;
  }

  function addMessage(m) {
    els.empty.hidden = true;
    const el = document.createElement("div");
    el.className = `msg ${m.role}${m.kind ? " " + m.kind : ""}${m.error ? " error" : ""}`;
    const a = agentOf(m.speaker);
    if (a) el.style.setProperty("--c", a.color);

    if (m.role === "system") {
      el.innerHTML = `<div class="bubble"></div>`;
    } else {
      const avatar = m.role === "user" ? "🙂" : (a ? a.avatar : "🤖");
      const time = new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      el.innerHTML = `<div class="avatar"></div><div class="body"><div class="meta"><span class="who"></span><span class="time"></span></div><div class="bubble"></div><div class="extra"></div></div>`;
      el.querySelector(".avatar").textContent = avatar;
      el.querySelector(".who").textContent = m.role === "user" ? "我" : m.speaker;
      el.querySelector(".time").textContent = time;
      if (a) {
        const model = document.createElement("span");
        model.className = "model";
        model.textContent = a.model;
        el.querySelector(".meta").appendChild(model);
      }
    }
    const n = { el, bubble: el.querySelector(".bubble"), raw: m.content || "", sources: m.sources || [], role: m.role, kind: m.kind, speaker: m.speaker, pending: !m.done };
    nodes.set(m.id, n);
    if (!m.done && !m.content) n.bubble.innerHTML = SKELETON;
    else paint(m.id);
    if (m.queries && m.queries.length) setSources(m.id, m.queries, m.sources);
    els.messages.appendChild(el);
  }

  function paint(id) {
    const n = nodes.get(id);
    if (!n) return;
    if (n.role === "system" && n.kind === "phase") {
      const [title, ...goal] = n.raw.split("\n");
      const m = title.match(/第\s*(\d+)\/(\d+)\s*阶段\s*·\s*([^（]+)(?:（(.+)）)?/);
      n.bubble.innerHTML = `<div class="phase-no"></div><div><div class="phase-title"><span class="name"></span><span class="phase-tags"></span></div><div class="phase-goal"></div></div>`;
      n.bubble.querySelector(".phase-no").textContent = m ? m[1] : "·";
      n.bubble.querySelector(".name").textContent = m ? `${m[3].trim()}` : title;
      if (m && m[4]) {
        for (const tag of m[4].split("、")) {
          const t = document.createElement("span");
          t.className = "tag";
          t.textContent = tag;
          n.bubble.querySelector(".phase-tags").appendChild(t);
        }
      }
      n.bubble.querySelector(".phase-goal").textContent = goal.join("\n");
      n.el.setAttribute("aria-label", title);
      return;
    }
    n.bubble.innerHTML = n.el.classList.contains("error") ? escapeHtml(n.raw) : renderMarkdown(n.raw, n.sources);
  }

  function setSources(id, queries, sources) {
    const n = nodes.get(id);
    if (!n) return;
    n.sources = sources || [];
    const box = n.el.querySelector(".extra");
    if (!box) return;
    box.innerHTML = "";
    const d = document.createElement("details");
    d.className = "search-info";
    const sum = document.createElement("summary");
    sum.innerHTML = `<i class="ph ph-magnifying-glass" aria-hidden="true"></i><span></span><i class="ph ph-caret-down caret" aria-hidden="true"></i>`;
    sum.querySelector("span").textContent = `${queries.join("；")} · ${n.sources.length} 条资料`;
    d.appendChild(sum);
    const ol = document.createElement("ol");
    for (const s of n.sources) {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = s.url; link.target = "_blank"; link.rel = "noopener"; link.textContent = s.title || s.url;
      const snip = document.createElement("span");
      snip.className = "snip"; snip.textContent = s.snippet;
      li.append(link, snip);
      ol.appendChild(li);
    }
    d.appendChild(ol);
    box.appendChild(d);
    if (n.pending && !n.raw) n.bubble.innerHTML = SKELETON;
  }

  function appendDelta(id, text) {
    const n = nodes.get(id);
    if (!n) return;
    n.raw += text;
    if (!renderQueued.size) requestAnimationFrame(flush);
    renderQueued.add(id);
  }
  function flush() {
    const stick = nearBottom();
    renderQueued.forEach(paint);
    renderQueued.clear();
    if (stick) scrollBottom(true);
  }

  function finishMessage(m) {
    const n = nodes.get(m.id);
    if (!n) return addMessage(m);
    renderQueued.delete(m.id);
    n.raw = m.content;
    n.pending = false;
    n.sources = m.sources || n.sources;
    n.el.classList.toggle("error", m.error);
    const stick = nearBottom();
    paint(m.id);
    if (stick) scrollBottom(true);
  }

  const nearBottom = () => els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 120;
  function scrollBottom(force) {
    if (force || nearBottom()) els.messages.scrollTop = els.messages.scrollHeight;
  }

  // ---------- 输入 ----------
  function submit() {
    const text = els.input.value.trim();
    if (!text) return;
    send({ type: "send", text });
    els.input.value = "";
    autosize();
    hidePop();
    scrollBottom(true);
  }
  els.send.onclick = submit;
  els.stop.onclick = () => send({ type: "stop" });
  els.cont.onclick = () => send({ type: "continue" });
  els.reload.onclick = () => send({ type: "reload" });
  els.clear.onclick = async () => { if (await ask("清空聊天", "所有聊天记录都会被删除，无法恢复。", "清空")) send({ type: "clear" }); };
  function setMenu(open) {
    els.sidebar.classList.toggle("open", open);
    els.menu.setAttribute("aria-expanded", String(open));
    els.menu.setAttribute("aria-label", open ? "关闭设置" : "打开设置");
  }
  els.menu.onclick = () => setMenu(!els.sidebar.classList.contains("open"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });

  function autosize() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 180) + "px";
  }

  // @ 补全
  let popItems = [], popIndex = 0;
  function mentionQuery() {
    const before = els.input.value.slice(0, els.input.selectionStart);
    const m = before.match(/@([^\s@]*)$/);
    return m ? m[1] : null;
  }
  function showPop() {
    const q = mentionQuery();
    popItems = q === null ? [] : agents.filter((a) => a.name.includes(q));
    if (!popItems.length) return hidePop();
    popIndex = Math.min(popIndex, popItems.length - 1);
    els.pop.innerHTML = "";
    popItems.forEach((a, i) => {
      const d = document.createElement("div");
      d.className = i === popIndex ? "active" : "";
      d.textContent = `${a.avatar} ${a.name}`;
      d.onmousedown = (e) => { e.preventDefault(); pickMention(a.name); };
      els.pop.appendChild(d);
    });
    els.pop.hidden = false;
  }
  function hidePop() { els.pop.hidden = true; popItems = []; popIndex = 0; }
  function pickMention(name) {
    const pos = els.input.selectionStart;
    const before = els.input.value.slice(0, pos).replace(/@([^\s@]*)$/, `@${name} `);
    els.input.value = before + els.input.value.slice(pos);
    els.input.selectionStart = els.input.selectionEnd = before.length;
    hidePop();
    els.input.focus();
  }
  function insertMention(name) {
    const v = els.input.value;
    els.input.value = v + (v && !v.endsWith(" ") ? " " : "") + `@${name} `;
    els.input.focus();
    autosize();
  }

  els.input.addEventListener("input", () => { autosize(); showPop(); });
  els.input.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;  // 中文输入法选词中
    if (!els.pop.hidden && popItems.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        popIndex = (popIndex + (e.key === "ArrowDown" ? 1 : popItems.length - 1)) % popItems.length;
        return showPop();
      }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); return pickMention(popItems[popIndex].name); }
      if (e.key === "Escape") return hidePop();
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  els.input.addEventListener("blur", () => setTimeout(hidePop, 100));

  // ---------- 提示 ----------
  let toastTimer;
  function toast(text, level) {
    els.toast.textContent = text;
    els.toast.className = `toast ${level === "error" ? "error" : ""}`;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (els.toast.hidden = true), level === "error" ? 6000 : 2500);
  }

  connect();
})();
