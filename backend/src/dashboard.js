// The operational dashboard's script (dashboard.html). Served from
// /api/v2/dashboard/static/ so the page needs no inline script.
let ADMIN_KEY = "";
let CLINICS = [];

const $ = (id) => document.getElementById(id);
// Local, not UTC. toISOString() converts first, so in Dhaka - six hours
// ahead - everything recorded before 6am belonged to "yesterday" and the
// page opened on an empty day while the clinic was working.
const ymd = (d) => {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const today = () => ymd(new Date());
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };
const esc = (v) => String(v ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (n) => Number(n || 0).toLocaleString();

async function api(path, params) {
  if (!ADMIN_KEY) ADMIN_KEY = window.prompt("Administrator key") || "";
  const query = new URLSearchParams(
    Object.entries(params || {}).filter(([, v]) => v !== "" && v != null));
  const response = await fetch(`/api/v2/dashboard${path}?${query}`,
    { headers: { "X-Admin-Key": ADMIN_KEY } });
  if (response.status === 401 || response.status === 403) {
    ADMIN_KEY = "";
    throw new Error("That key was not accepted.");
  }
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

/* ---- tooltip: one element, moved ---- */
const tip = $("tip");
function showTip(event, html) {
  tip.innerHTML = html;
  tip.style.opacity = "1";
  const box = tip.getBoundingClientRect();
  let x = event.clientX + 14, y = event.clientY - box.height - 10;
  if (x + box.width > innerWidth - 8) x = event.clientX - box.width - 14;
  if (y < 8) y = event.clientY + 16;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.style.opacity = "0"; };

/* ============================================================
   One chart, one series, no legend - the heading names it. The
   hit target is the whole column, so a two-pixel bar is still
   easy to hover.
   ============================================================ */
function barChart(host, rows, { label, value, unit }) {
  if (!rows.length) { host.innerHTML = `<div class="empty">Nothing in this range.</div>`; return; }
  const W = Math.max(320, host.clientWidth || 640), H = 232;
  const padL = 36, padR = 10, padT = 12, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const max = Math.max(1, ...rows.map((r) => Number(value(r)) || 0));
  const step = plotW / rows.length;
  const bw = Math.max(3, Math.min(40, step - 6));

  let g = "";
  for (let i = 0; i <= 4; i++) {                 // four gridlines reads a magnitude
    const v = (max / 4) * i, y = padT + plotH - (v / max) * plotH;
    g += `<line class="gridline" x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}"/>`
       + `<text x="${padL - 7}" y="${y + 3.5}" text-anchor="end">${Math.round(v)}</text>`;
  }

  const every = Math.ceil(rows.length / 9);
  let bars = "";
  rows.forEach((r, i) => {
    const v = Number(value(r)) || 0;
    const h = v > 0 ? Math.max(2, (v / max) * plotH) : 0;
    const x = padL + i * step + (step - bw) / 2;
    bars += `<g class="col">`
      + `<rect x="${padL + i * step}" y="${padT}" width="${step}" height="${plotH}"`
      + ` fill="transparent" data-i="${i}"/>`
      + `<rect class="bar" x="${x}" y="${padT + plotH - h}" width="${bw}" height="${h}"`
      + ` rx="4" ry="4" fill="var(--s1)" pointer-events="none"/></g>`;
    if (i % every === 0 || i === rows.length - 1) {
      bars += `<text x="${padL + i * step + step / 2}" y="${H - 10}"`
        + ` text-anchor="middle">${esc(label(r))}</text>`;
    }
  });

  host.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" height="${H}" role="img"`
    + ` aria-label="Recordings each day">${g}`
    + `<line class="axis" x1="${padL}" x2="${W - padR}" y1="${padT + plotH}" y2="${padT + plotH}"/>`
    + bars + `</svg>`;

  host.querySelectorAll("g.col rect[data-i]").forEach((hit) => {
    const r = rows[Number(hit.dataset.i)];
    hit.addEventListener("mousemove", (e) => showTip(e,
      `<b>${esc(label(r))}</b><br>${num(value(r))} ${esc(unit)} · ${num(r.hours)} h`));
    hit.addEventListener("mouseleave", hideTip);
  });
}

/* ---- KPI strip ---- */
const toneFor = (n, warnAt = 1) => !n ? "good" : (n >= warnAt * 3 ? "bad" : "warn");

function kpi({ label, value, note, tone, state }) {
  return `<div class="kpi t-${tone || "info"}${state ? " actionable" : ""}"`
    + (state ? ` data-state="${esc(state)}"` : "")
    + `><div class="k-label">${esc(label)}</div>`
    + `<div class="k-value${tone ? " t-" + tone : ""}">${esc(value)}</div>`
    + `<div class="k-note">${esc(note || "")}</div></div>`;
}

function drawKpis(d) {
  const v = d.volume, c = d.confirmation.totals || {}, i = d.integrity,
        p = d.copies, r = d.recorders;
  const total = v.total_recordings || 0;
  const confirmed = c.confirmed || 0;
  const rate = total ? Math.round((confirmed / total) * 100) : 0;
  const rooms = r.rooms || [];
  const silent = rooms.filter((q) => q.silent && !q.revoked).length;
  const uncopied = p.recordings_without_a_cloud_copy || 0;

  $("kpis").innerHTML = [
    kpi({ label: "Recordings", value: num(total),
          note: `${num(v.total_hours)} hours of audio`, tone: "info" }),
    kpi({ label: "Confirmed", value: total ? rate + "%" : "—",
          note: `${num(confirmed)} matched to CMED`,
          tone: !total ? null : rate >= 98 ? "good" : rate >= 90 ? "warn" : "bad" }),
    kpi({ label: "Unconfirmed", value: num(c.unconfirmed || 0),
          note: "erased after 24 hours", tone: toneFor(c.unconfirmed),
          state: "unconfirmed" }),
    kpi({ label: "Quarantined", value: num(i.quarantined_total || 0),
          note: "held, never deleted", tone: toneFor(i.quarantined_total),
          state: "quarantined" }),
    kpi({ label: "No cloud copy", value: num(uncopied),
          note: p.oldest_uncopied_hours != null
            ? `oldest ${Math.round(p.oldest_uncopied_hours)} h`
            : "safe in two places",
          tone: toneFor(uncopied, 2), state: "uncopied" }),
    kpi({ label: "Silent recorders", value: num(silent),
          note: `${num(rooms.length)} enrolled`, tone: toneFor(silent) }),
  ].join("");

  $("notice").style.display = total ? "none" : "block";
  $("notice").textContent =
    `No recordings between ${$("from").value} and ${$("to").value}`
    + ($("clinic").value ? ` for ${$("clinic").selectedOptions[0].textContent}` : "")
    + `. Widen the dates or choose another clinic — the page itself is fine.`;
}

/* ---- clinic board: the filter, as data ---- */
function drawClinics(d) {
  const byClinic = new Map((d.volume.by_clinic || []).map((c) => [c.hospital_id, c]));
  const max = Math.max(1, ...[...byClinic.values()].map((c) => c.recordings || 0));
  const current = $("clinic").value;

  $("clinicBoard").innerHTML = CLINICS.length ? CLINICS.map((c) => {
    const row = byClinic.get(c.hospital_id) || { recordings: 0, hours: 0 };
    const pct = Math.round(((row.recordings || 0) / max) * 100);
    return `<div class="clinic" data-id="${esc(c.hospital_id)}" tabindex="0" role="button"`
      + ` aria-current="${current === c.hospital_id}">`
      + `<div><div class="name">${esc(c.name || c.hospital_id)}</div>`
      + `<div class="code">${esc(c.hospital_id)}</div></div>`
      + `<div><div class="n">${num(row.recordings)}</div>`
      + `<div class="sub">${num(row.hours)} h</div></div>`
      + `<div class="meter"><i data-pct="${pct}"></i></div></div>`;
  }).join("") : `<div class="empty">No clinics registered.</div>`;

  // Widths are set through the DOM, not a style attribute in the markup:
  // the page's security policy allows no inline style.
  $("clinicBoard").querySelectorAll(".meter i").forEach((bar) => {
    bar.style.width = `${bar.dataset.pct}%`;
  });

  $("clinicBoard").querySelectorAll(".clinic").forEach((el) => {
    const pick = () => {
      const id = el.dataset.id;
      $("clinic").value = ($("clinic").value === id) ? "" : id;   // click again to clear
      refresh();
    };
    el.addEventListener("click", pick);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
  });
}

/* ---- needs attention ---- */
function drawAttention(d) {
  const c = d.confirmation.totals || {}, i = d.integrity,
        x = d.reconciliation, p = d.copies;
  const items = [
    { what: "Unconfirmed recordings", why: "CMED never described them — erased after 24 h",
      n: c.unconfirmed || 0, state: "unconfirmed" },
    { what: "Records with no recording", why: "CMED sent a visit we never recorded",
      n: x.records_without_a_recording || 0, state: null },
    { what: "Recordings with no record", why: "recorded, nothing from CMED yet",
      n: x.recordings_without_a_record || 0, state: "pending" },
    { what: "Quarantined", why: "a hash did not match — kept, never deleted",
      n: i.quarantined_total || 0, state: "quarantined" },
    { what: "Stopped early", why: "closed for a reason other than the prescription",
      n: i.stopped_early_total || 0, state: "stopped_early" },
    { what: "No cloud copy", why: "on the array, not yet safe in two places",
      n: p.recordings_without_a_cloud_copy || 0, state: "uncopied" },
    { what: "Pieces still in transit", why: "in the segment store over 48 hours",
      n: p.pieces_older_than_48h || 0, state: null },
    { what: "Open integrity alerts", why: "the operator queue",
      n: i.alerts_total || 0, state: null },
  ];
  const row = (it) => {
    const tone = !it.n ? "quiet" : it.n >= 3 ? "bad" : "warn";
    return `<div class="att-row${it.n && it.state ? " clickable" : ""}"`
      + (it.n && it.state ? ` data-state="${esc(it.state)}"` : "")
      + `><span class="ic ${tone}" aria-hidden="true">${it.n ? "!" : "✓"}</span>`
      + `<span class="what">${esc(it.what)}<small>${esc(it.why)}</small></span>`
      + `<span class="n">${num(it.n)}</span></div>`;
  };
  const clean = items.every((it) => !it.n);
  $("attention").innerHTML = clean
    ? `<div class="att-row"><span class="ic good" aria-hidden="true">✓</span>`
      + `<span class="what">Nothing needs attention`
      + `<small>every check in this range is clear</small></span>`
      + `<span class="n">0</span></div>`
    : items.map(row).join("");
}

/* ---- recorders ---- */
function drawRooms(d) {
  const rooms = d.recorders.rooms || [];
  if (!rooms.length) {
    $("rooms").innerHTML = `<div class="empty">No recorders enrolled yet.</div>`;
    return;
  }
  $("rooms").innerHTML = `<table><thead><tr><th>Machine</th><th>Clinic</th>
      <th>Version</th><th class="num">Holding</th><th>Last seen</th><th>State</th>
      <th>Offline this week</th></tr></thead><tbody>`
    + rooms.map((q) => {
        const st = q.revoked ? ["bad", "revoked"]
                 : q.silent ? ["warn", "silent"] : ["good", "reporting"];
        const seen = q.minutes_since_seen == null ? "never"
          : q.minutes_since_seen < 60 ? `${Math.round(q.minutes_since_seen)} min ago`
          : `${Math.round(q.minutes_since_seen / 60)} h ago`;
        return `<tr><td>${esc(q.machine_name || String(q.device_id).slice(0, 8))}</td>`
          + `<td>${esc(q.hospital_id)}</td><td>${esc(q.app_version || "—")}</td>`
          + `<td class="num">${num(q.pending_segments)} · ${num(q.spool_mb)} MB</td>`
          + `<td>${esc(seen)}</td>`
          + `<td><span class="tag ${st[0]}">${st[1]}</span></td>`
          + `<td>${offlineText(q.offline_periods)}</td></tr>`;
      }).join("") + `</tbody></table>`;
}

/* What a recorder did while it could not reach the server, from its own log. */
function offlineText(periods) {
  if (!periods || !periods.length) return `<span class="hint">—</span>`;
  const at = (iso) => new Date(iso).toLocaleTimeString([],
    { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Dhaka" });
  const day = (iso) => new Date(iso).toLocaleDateString([],
    { day: "numeric", month: "short", timeZone: "Asia/Dhaka" });
  return periods.map((p) => {
    const mic = p.microphone === "fine" ? "microphone fine"
      : `<span class="tag warn">microphone ${esc(p.microphone)}</span>`;
    return `${esc(day(p.from))} ${esc(at(p.from))}–${esc(at(p.to))} · `
      + `${num(p.consultations)} consultation${p.consultations === 1 ? "" : "s"} · ${mic}`;
  }).join("<br>");
}

/* ---- recordings table, with the folder to delete by hand ---- */
function confirmTag(v) {
  const map = { confirmed: "good", pending: "warn", unconfirmed: "bad",
                refused: "quiet", expired: "quiet", legacy: "quiet" };
  return `<span class="tag ${map[v] || "quiet"}">${esc(v || "—")}</span>`;
}

function recordingsTable(list) {
  if (!list.length) return `<div class="empty">No recordings match.</div>`;
  return `<table><thead><tr><th>Date</th><th>Clinic</th><th>Doctor</th><th>Patient</th>
      <th class="num">Length</th><th>Confirmation</th><th>Archived</th>
      <th>Folder on the server</th></tr></thead><tbody>`
    + list.map((r) => {
        const mins = Math.round((r.total_duration_seconds || 0) / 60);
        const rel = r.archive_relpath || "";
        const folder = rel ? rel.replace(/\/[^/]*$/, "") : "";
        return `<tr><td>${esc(r.session_date || "")}</td>`
          + `<td>${esc(r.hospital_id)}</td><td>${esc(r.doctor_id)}</td>`
          + `<td>${esc(r.patient_id)}</td><td class="num">${mins} min</td>`
          + `<td>${confirmTag(r.confirmation)}</td>`
          + `<td>${r.archived_at ? `<span class="tag good">yes</span>`
                                 : `<span class="tag warn">no</span>`}</td>`
          + `<td class="path" title="${esc(folder)}">${esc(folder || "—")}`
          + (folder ? ` <button class="copy" data-copy="${esc(folder)}">copy</button>` : "")
          + `</td></tr>`;
      }).join("") + `</tbody></table>`;
}

function wireCopy(root) {
  root.querySelectorAll("button[data-copy]").forEach((b) => {
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(b.dataset.copy);
        const was = b.textContent; b.textContent = "copied";
        setTimeout(() => { b.textContent = was; }, 1200);
      } catch { /* clipboard blocked: the title attribute still shows the path */ }
    });
  });
}

async function loadRecordings() {
  const clinic = $("clinic").value;
  $("recTitle").textContent = clinic
    ? `Recordings — ${$("clinic").selectedOptions[0].textContent}`
    : "Recordings — all clinics";
  $("recTable").innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const d = await api("/recordings", { from: $("from").value, to: $("to").value,
                                         hospital_id: clinic, limit: 200 });
    const list = d.recordings || [];
    $("recTable").innerHTML = recordingsTable(list);
    $("recHint").textContent = `${num(list.length)} shown · newest first`;
    wireCopy($("recTable"));
  } catch (err) {
    $("recTable").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

async function openRows(state) {
  $("rowsTitle").textContent = "Recordings — " + state.replace(/_/g, " ");
  $("rowsBody").innerHTML = `<div class="empty">Loading…</div>`;
  $("rows").showModal();
  try {
    const d = await api("/recordings", { from: $("from").value, to: $("to").value,
      hospital_id: $("clinic").value, state, limit: 200 });
    $("rowsBody").innerHTML = recordingsTable(d.recordings || []);
    wireCopy($("rowsBody"));
  } catch (err) {
    $("rowsBody").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-state]");
  if (el) openRows(el.dataset.state);
});

/* ---- refresh ---- */
async function refresh() {
  try {
    const d = await api("/summary", { from: $("from").value, to: $("to").value,
                                      hospital_id: $("clinic").value });
    drawKpis(d);
    drawClinics(d);
    drawAttention(d);
    drawRooms(d);
    barChart($("byDay"), d.volume.by_day || [],
      { label: (r) => String(r.date || "").slice(5), value: (r) => r.recordings,
        unit: "recordings" });
    await loadRecordings();
    $("when").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (err) {
    $("notice").style.display = "block";
    $("notice").textContent = err.message;
  }
}

$("refresh").addEventListener("click", refresh);
$("closeRows").addEventListener("click", () => $("rows").close());
["from", "to", "clinic"].forEach((id) => $(id).addEventListener("change", refresh));
addEventListener("resize", () => {
  clearTimeout(window._rz);
  window._rz = setTimeout(refresh, 300);
});

/* Dark mode is chosen, not flipped: its own steps from the same ramps. */
$("theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("aims.theme", next); } catch {}
  refresh();
});
try {
  document.documentElement.dataset.theme = localStorage.getItem("aims.theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
} catch { document.documentElement.dataset.theme = "light"; }

async function start() {
  $("from").value = daysAgo(6);
  $("to").value = today();
  try {
    const { clinics } = await api("/clinics", {});
    CLINICS = clinics || [];
    $("clinic").innerHTML = `<option value="">All clinics</option>`
      + CLINICS.map((c) => `<option value="${esc(c.hospital_id)}">`
        + `${esc(c.name || c.hospital_id)}</option>`).join("");
  } catch { /* refresh reports it */ }
  await refresh();
}

start();
