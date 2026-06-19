/* Finanzas Familia — dashboard en vivo + chat con el asistente IA. */
(function () {
  "use strict";
  if (!window.DASH) return;

  // ───────────────────────── utilidades ─────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const fmt = (n) => "$" + Math.round(n || 0).toLocaleString("es-CL");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  // Markdown mínimo para los mensajes del asistente (ya escapado).
  function md(s) {
    let t = esc(s);
    t = t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    t = t.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<b>$2</b>");
    t = t.replace(/^[-•]\s?/gm, "<span class='b-dot'>•</span> ");
    return t.replace(/\n/g, "<br>");
  }

  const PALETTE = ["#818cf8", "#22d3ee", "#34d399", "#fbbf24", "#fb7185",
    "#a78bfa", "#f472b6", "#60a5fa", "#facc15", "#4ade80", "#94a3b8"];

  if (window.Chart) {
    Chart.defaults.color = "#9aa3c7";
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.borderColor = "rgba(154,163,199,.12)";
  }

  let filtroPersona = "";   // "" = todos
  const charts = {};

  function mkChart(id, cfg) {
    if (charts[id]) charts[id].destroy();
    const el = document.getElementById(id);
    if (!el) return;
    charts[id] = new Chart(el, cfg);
  }

  // ───────────────────────── render ─────────────────────────
  function delta(actual, anterior, invertir) {
    if (!anterior) return "";
    const pct = Math.round(((actual - anterior) / anterior) * 100);
    if (!isFinite(pct) || Math.abs(pct) > 900) return "";
    const sube = pct > 0;
    const malo = invertir ? sube : !sube;
    const flecha = sube ? "▲" : "▼";
    return `<span class="delta ${malo ? "bad" : "good"}">${flecha} ${Math.abs(pct)}% vs mes anterior</span>`;
  }

  function renderKPIs(d) {
    const r = d.resumen, a = d.anterior;
    $("#kpiIngresos").textContent = fmt(r.ingresos);
    $("#kpiIngresosSub").innerHTML =
      r.ingresos_extra ? `sueldos ${fmt(r.sueldos)} + extras ${fmt(r.ingresos_extra)}` : "";
    $("#kpiGastos").textContent = fmt(r.gastos);
    $("#kpiGastosDelta").innerHTML = delta(r.gastos, a.gastos, true);
    const disp = $("#kpiDisponible");
    disp.textContent = fmt(r.disponible);
    disp.classList.toggle("neg", r.disponible < 0);
    $("#kpiDisponibleSub").innerHTML =
      r.bills_pendientes ? `descontando ${fmt(r.bills_pendientes)} en cuentas por pagar` : "";
    $("#kpiHormiga").textContent = fmt(r.hormigas);
    $("#kpiHormigaDelta").innerHTML = delta(r.hormigas, a.hormigas, true);
  }

  function renderChartCat(d) {
    const cats = d.por_categoria;
    const top = cats.slice(0, 8);
    const resto = cats.slice(8).reduce((s, c) => s + c.total, 0);
    const labels = top.map((c) => `${c.emoji} ${c.nombre}`.trim());
    const data = top.map((c) => c.total);
    if (resto) { labels.push("📦 Otras"); data.push(resto); }
    mkChart("chartCat", {
      type: "doughnut",
      data: { labels, datasets: [{ data, backgroundColor: PALETTE, borderWidth: 0, hoverOffset: 8 }] },
      options: {
        cutout: "62%", maintainAspectRatio: false,
        plugins: {
          legend: { position: "right", labels: { boxWidth: 10, boxHeight: 10, padding: 10, font: { size: 12 } } },
          tooltip: { callbacks: { label: (c) => ` ${fmt(c.parsed)}` } },
        },
      },
    });
  }

  function renderChartDiario(d) {
    const act = d.es_mes_actual ? d.diario.actual.slice(0, d.dia_hoy) : d.diario.actual;
    const n = Math.max(d.diario.actual.length, d.diario.anterior.length);
    mkChart("chartDiario", {
      type: "line",
      data: {
        labels: Array.from({ length: n }, (_, i) => i + 1),
        datasets: [
          { data: act, borderColor: "#818cf8", backgroundColor: "rgba(129,140,248,.15)",
            fill: true, tension: .3, pointRadius: 0, borderWidth: 2.5 },
          { data: d.diario.anterior, borderColor: "#475281", borderDash: [6, 5],
            fill: false, tension: .3, pointRadius: 0, borderWidth: 1.5 },
        ],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (c) => ` día ${c.label}: ${fmt(c.parsed.y)}` } } },
        scales: { y: { ticks: { callback: (v) => fmt(v) } }, x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } } },
      },
    });
  }

  function renderChartMeses(d) {
    mkChart("chartMeses", {
      type: "bar",
      data: {
        labels: d.serie_meses.map((m) => m.label),
        datasets: [
          { data: d.serie_meses.map((m) => m.ingresos), backgroundColor: "rgba(52,211,153,.75)", borderRadius: 6 },
          { data: d.serie_meses.map((m) => m.gastos), backgroundColor: "rgba(251,113,133,.8)", borderRadius: 6 },
        ],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (c) => ` ${c.datasetIndex ? "gastos" : "ingresos"}: ${fmt(c.parsed.y)}` } } },
        scales: { y: { ticks: { callback: (v) => fmt(v) } }, x: { grid: { display: false } } },
      },
    });
  }

  function renderBudgets(d) {
    const box = $("#budgetList");
    if (!d.presupuestos.length) {
      box.innerHTML = '<p class="muted">Sin presupuestos. Fija uno y te aviso cuando te acerques al límite.</p>';
      return;
    }
    box.innerHTML = d.presupuestos.map((b) => {
      const pct = Math.min(b.pct, 100);
      const nivel = b.pct >= 100 ? "over" : b.pct >= 80 ? "warn" : "ok";
      return `<div class="budget">
        <div class="budget-row"><span>${esc(b.emoji)} ${esc(b.categoria)}</span>
          <b class="${nivel}">${b.pct}%</b></div>
        <div class="bar"><i class="${nivel}" style="width:${pct}%"></i></div>
        <small>${fmt(b.gastado)} de ${fmt(b.limite)}</small>
      </div>`;
    }).join("");
  }

  function renderBills(d) {
    const ul = $("#billList");
    if (!d.bills.length) { ul.innerHTML = '<li class="muted">Nada pendiente 🎉</li>'; return; }
    ul.innerHTML = d.bills.map((b) => {
      const urg = b.dias < 0 ? "over" : b.dias <= 3 ? "warn" : "";
      const cuando = b.dias < 0 ? `venció hace ${-b.dias}d` : b.dias === 0 ? "vence hoy" : `en ${b.dias}d`;
      return `<li>
        <span><b>${esc(b.label)}</b> <span class="due ${urg}">${b.vence.slice(8, 10)}/${b.vence.slice(5, 7)} · ${cuando}</span>
        ${b.monto ? `<span class="bill-monto">${fmt(b.monto)}</span>` : ""}</span>
        <button class="btn sm ghost" data-pagar="${b.id}">Pagada</button>
      </li>`;
    }).join("");
    ul.querySelectorAll("[data-pagar]").forEach((btn) => btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch(`/bill/${btn.dataset.pagar}/pagar`, { method: "POST" });
      refresh();
    }));
  }

  function renderPersonas(d) {
    const box = $("#personaList");
    if (!d.por_persona.length) { box.innerHTML = '<p class="muted">Sin gastos este mes.</p>'; return; }
    const max = Math.max(...d.por_persona.map((p) => p.total), 1);
    box.innerHTML = d.por_persona.map((p, i) => `<div class="persona">
      <div class="budget-row"><span>${esc(p.nombre)}</span><b>${fmt(p.total)}</b></div>
      <div class="bar"><i style="width:${Math.round(p.total * 100 / max)}%;background:${PALETTE[i % PALETTE.length]}"></i></div>
    </div>`).join("");
  }

  const FUENTE_ICO = { ia: "🤖", whatsapp: "💬", manual: "✍️", statement: "📄" };

  function renderTable(d) {
    const q = ($("#txSearch").value || "").toLowerCase();
    const rows = d.txs.filter((t) =>
      !q || `${t.categoria} ${t.descripcion} ${t.persona}`.toLowerCase().includes(q));
    $("#txCount").textContent = `${rows.length}`;
    const body = $("#txBody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">Sin movimientos.</td></tr>';
      return;
    }
    body.innerHTML = rows.map((t) => `<tr class="${t.kind === "income" ? "inc" : ""}">
      <td>${t.fecha.slice(8, 10)}/${t.fecha.slice(5, 7)}</td>
      <td>${esc(t.emoji)} ${esc(t.categoria)}</td>
      <td>${esc(t.descripcion)} <span class="src" title="origen">${FUENTE_ICO[t.fuente] || ""}</span></td>
      <td>${esc(t.persona)}</td>
      <td class="r">${t.kind === "income" ? "+" : "−"}${fmt(t.monto)}</td>
      <td class="r tx-actions">
        <button class="tx-ed" data-ed="${t.id}" title="Editar">✎</button>
        <button class="tx-del" data-del="${t.id}" title="Eliminar">✕</button>
      </td>
    </tr>`).join("");
    body.querySelectorAll("[data-del]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!confirm("¿Eliminar este movimiento?")) return;
      await fetch(`/api/tx/${btn.dataset.del}/delete`, { method: "POST" });
      refresh();
    }));
    body.querySelectorAll("[data-ed]").forEach((btn) =>
      btn.addEventListener("click", () => abrirEditor(parseInt(btn.dataset.ed, 10))));
  }

  // ───────────────────────── editar movimiento ─────────────────────────
  function llenarCategorias(kind) {
    const sel = $("#txEditCat");
    const cats = (DASH.cats || []).filter((c) => c.kind === kind);
    sel.innerHTML = '<option value="">(automática)</option>' +
      cats.map((c) => `<option value="${esc(c.name)}">${esc(c.emoji)} ${esc(c.name)}</option>`).join("");
  }

  function abrirEditor(id) {
    const t = (DASH.data.txs || []).find((x) => x.id === id);
    if (!t) return;
    $("#txEditId").value = id;
    $("#txEditKind").value = t.kind;
    llenarCategorias(t.kind);
    $("#txEditCat").value = t.categoria || "";
    $("#txEditMonto").value = t.monto;
    $("#txEditDesc").value = t.descripcion || "";
    $("#txEditFecha").value = t.fecha.slice(0, 10);
    $("#txEditModal").classList.remove("hidden");
    $("#txEditMonto").focus();
  }

  function cerrarEditor() { $("#txEditModal").classList.add("hidden"); }

  function render(d) {
    renderKPIs(d); renderChartCat(d); renderChartDiario(d); renderChartMeses(d);
    renderBudgets(d); renderBills(d); renderPersonas(d); renderTable(d);
  }

  async function refresh() {
    try {
      const u = filtroPersona ? `&u=${filtroPersona}` : "";
      const r = await fetch(`/api/dashboard-data?year=${DASH.year}&month=${DASH.month}${u}`);
      const j = await r.json();
      if (j.ok) { DASH.data = j; render(j); }
    } catch (e) { /* sin red: se mantiene lo pintado */ }
  }

  // ───────────────────────── insights IA ─────────────────────────
  async function loadInsights(force) {
    const box = $("#insightsBox");
    if (!box) return;
    if (!DASH.deepseek) {
      box.innerHTML = '<p class="muted">Agrega tu <code>DEEPSEEK_API_KEY</code> en <code>.env</code> para ver el análisis inteligente del mes aquí. 🧠</p>';
      return;
    }
    if (force || !box.dataset.loaded) {
      box.innerHTML = '<div class="shimmer"></div><div class="shimmer w70"></div><div class="shimmer w85"></div><div class="shimmer w60"></div>';
    }
    try {
      const r = await fetch(`/api/insights?year=${DASH.year}&month=${DASH.month}${force ? "&force=1" : ""}`);
      const j = await r.json();
      if (j.ok) {
        box.innerHTML = `<div class="insights-text fade-in">${md(j.content)}</div>`;
        box.dataset.loaded = "1";
      } else {
        box.innerHTML = `<p class="muted">No pude generar el análisis ahora (${esc(j.error || "error")}). Intenta con ↻.</p>`;
      }
    } catch (e) {
      box.innerHTML = '<p class="muted">Error de red al pedir el análisis. Intenta con ↻.</p>';
    }
  }

  // ───────────────────────── chat con Fin ─────────────────────────
  const chatKey = `finchat-${DASH.householdId}`;
  let hist = [];
  try { hist = JSON.parse(sessionStorage.getItem(chatKey) || "[]"); } catch (e) { hist = []; }
  let chatBusy = false;

  function saveHist() { sessionStorage.setItem(chatKey, JSON.stringify(hist.slice(-30))); }

  function chatBubble(role, html, extraCls) {
    const div = document.createElement("div");
    div.className = `msg ${role}${extraCls ? " " + extraCls : ""}`;
    div.innerHTML = html;
    $("#chatMsgs").appendChild(div);
    $("#chatMsgs").scrollTop = $("#chatMsgs").scrollHeight;
    return div;
  }

  function paintChat() {
    $("#chatMsgs").innerHTML = "";
    hist.forEach((m) => chatBubble(m.role === "user" ? "user" : "bot",
      m.role === "user" ? esc(m.content) : md(m.content)));
    $("#chatSuggest").style.display = hist.length ? "none" : "flex";
  }

  async function sendChat(texto) {
    if (chatBusy || !texto.trim()) return;
    chatBusy = true;
    $("#chatText").value = "";
    $("#chatSuggest").style.display = "none";
    chatBubble("user", esc(texto));
    const typing = chatBubble("bot", '<span class="typing"><i></i><i></i><i></i></span>');
    try {
      const r = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: texto,
          user_id: parseInt($("#chatUser").value, 10),
          history: hist.slice(-12),
        }),
      });
      const j = await r.json();
      typing.remove();
      if (!j.ok) {
        chatBubble("bot", esc(j.error || "Algo falló 😅, intenta de nuevo."), "err");
      } else {
        let html = md(j.reply);
        if (j.actions && j.actions.length) {
          html += '<div class="chat-actions">' +
            j.actions.map((a) => `<span class="action-chip">${esc(a.texto)}</span>`).join("") + "</div>";
        }
        chatBubble("bot", html);
        hist.push({ role: "user", content: texto });
        hist.push({ role: "assistant", content: j.reply });
        saveHist();
        if (j.refresh) { refresh(); loadInsights(false); }
      }
    } catch (e) {
      typing.remove();
      chatBubble("bot", "Error de conexión 😕. ¿Está corriendo la app?", "err");
    }
    chatBusy = false;
    $("#chatText").focus();
  }

  // ───────────────────────── eventos ─────────────────────────
  function bind() {
    document.querySelectorAll("#personFilter .chip").forEach((chip) =>
      chip.addEventListener("click", () => {
        document.querySelectorAll("#personFilter .chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        filtroPersona = chip.dataset.u;
        refresh();
      }));

    $("#txSearch").addEventListener("input", () => renderTable(DASH.data));
    $("#btnInsights") && $("#btnInsights").addEventListener("click", () => loadInsights(true));

    // Editor de movimientos
    $("#txEditClose").addEventListener("click", cerrarEditor);
    $("#txEditModal").addEventListener("click", (e) => { if (e.target.id === "txEditModal") cerrarEditor(); });
    $("#txEditKind").addEventListener("change", () => llenarCategorias($("#txEditKind").value));
    $("#txEditForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const id = $("#txEditId").value;
      const btn = e.submitter; if (btn) btn.disabled = true;
      try {
        const r = await fetch(`/api/tx/${id}/edit`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: $("#txEditKind").value, monto: $("#txEditMonto").value,
            categoria: $("#txEditCat").value, descripcion: $("#txEditDesc").value,
            fecha: $("#txEditFecha").value,
          }),
        });
        const j = await r.json();
        if (!j.ok) { alert("No se pudo guardar el cambio."); }
        else { cerrarEditor(); refresh(); loadInsights(false); }
      } finally { if (btn) btn.disabled = false; }
    });

    const fab = $("#chatFab"), panel = $("#chatPanel");
    fab.addEventListener("click", () => {
      panel.classList.toggle("hidden");
      fab.classList.toggle("open");
      if (!panel.classList.contains("hidden")) { paintChat(); $("#chatText").focus(); }
    });
    $("#chatClose").addEventListener("click", () => { panel.classList.add("hidden"); fab.classList.remove("open"); });
    $("#chatClear").addEventListener("click", () => { hist = []; saveHist(); paintChat(); });
    $("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("#chatText").value); });
    document.querySelectorAll("#chatSuggest .chip").forEach((c) =>
      c.addEventListener("click", () => sendChat(c.textContent)));

    const savedUser = localStorage.getItem("fin-user");
    if (savedUser && $("#chatUser").querySelector(`option[value="${savedUser}"]`)) $("#chatUser").value = savedUser;
    $("#chatUser").addEventListener("change", () => localStorage.setItem("fin-user", $("#chatUser").value));
  }

  // init
  render(DASH.data);
  loadInsights(false);
  bind();
})();
