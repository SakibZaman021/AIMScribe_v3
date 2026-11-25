// AIMS LAB server dashboard script. Served from /api/v2/dashboard/static/.
// Vanilla JS, no packages. The admin key lives in memory for this tab only.
"use strict";

let ADMIN_KEY = "";
// Signed in with Google (portal.py): the session cookie is the credential,
// and every request carries X-AIMS-Portal so no other site can use it.
let PORTAL = null;          // {email, role} when signed in
const portalHeaders = () => (PORTAL ? { "X-AIMS-Portal": "1" } : { "X-Admin-Key": ADMIN_KEY });
let CLINICS = [];
let DOCTORS = [];     // 24 Nov 2026: {hospital_id, doctor_id}, for the Recordings doctor filter
let LAST = null;            // last /summary answer, for redraws without a fetch
let STATE = "";             // recordings table filter (the API's `state`)
let PC_FILTER = "all";      // PCs table filter
let UPDATED_AT = 0;
let FAILED = false;
let PAUSED = false;
let BUSY = false;

const AUTO_SECONDS = 30;    // auto-refresh interval
const STALE_SECONDS = 90;   // the "updated" pill turns amber after this

const $ = (id) => document.getElementById(id);
// Local date, not UTC: in Dhaka toISOString() puts everything before 6 am on
// the previous day.
const ymd = (d) => {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const today = () => ymd(new Date());
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString();
const plural = (n, one, many) => `${num(n)} ${Number(n) === 1 ? one : (many || one + "s")}`;
const human = (code) => String(code || "").replace(/[_-]+/g, " ").trim();
const shortId = (s) => String(s || "").slice(-8);
const clinicName = (id) => {
  const c = CLINICS.find((x) => x.hospital_id === id);
  return (c && c.name) || id || "";
};
// 18 Nov: this collector's Telegram, for the warning at Start shift.
let myTelegram = {};
const tag = (tone, text, tip) =>
  `<span class="tag ${tone}"${tip ? ` title="${esc(tip)}"` : ""}>${esc(text)}</span>`;
const empty = (title, line, ok) =>
  `<div class="empty${ok ? " ok" : ""}"><b>${esc(title)}</b>${esc(line || "")}</div>`;
const ago = (minutes) => {
  if (minutes == null) return "never";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} d ago`;
};
const when = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString([], { day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit" });
};
const minutesSince = (iso) => {
  const t = Date.parse(iso || "");
  return isNaN(t) ? null : (Date.now() - t) / 60000;
};
const td = (label, html, cls) =>
  `<td data-label="${esc(label)}"${cls ? ` class="${cls}"` : ""}>${html}</td>`;

/* ============================================================
   Admin key: asked once in an in-page form, kept in memory only.
   ============================================================ */
let keyWaiters = [];
function askKey(message) {
  const dlg = $("keyDialog");
  $("keyMsg").textContent = message || "Kept in this tab only. Not saved.";
  $("keyMsg").className = "keymsg" + (message ? " bad" : "");
  if (!dlg.open) {
    $("keyInput").value = "";
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }
  $("keyInput").focus();
  return new Promise((resolve) => keyWaiters.push(resolve));
}
$("keyForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const key = $("keyInput").value.trim();
  if (!key) { $("keyInput").focus(); return; }
  ADMIN_KEY = key;
  $("keyDialog").close ? $("keyDialog").close() : $("keyDialog").removeAttribute("open");
  const waiting = keyWaiters; keyWaiters = [];
  waiting.forEach((fn) => fn(key));
});
$("keyDialog").addEventListener("cancel", (e) => e.preventDefault());  // the page needs it

let MINE = false;   // 038: "Only my hospitals" - a researcher's own hospitals
async function api(path, params, retried) {
  if (!PORTAL && !ADMIN_KEY) await askKey();
  const query = new URLSearchParams(
    Object.entries({ ...(params || {}), ...(MINE ? { mine: "1" } : {}) })
      .filter(([, v]) => v !== "" && v != null));
  const response = await fetch(`/api/v2/dashboard${path}?${query}`,
    { headers: portalHeaders() });
  if (PORTAL && response.status === 401) {
    window.location.href = "/auth/login?next=/dashboard";   // the session ended
    throw new Error("Signed out.");
  }
  if (!PORTAL && (response.status === 401 || response.status === 403)) {
    ADMIN_KEY = "";
    if (!retried) {
      await askKey("Key not accepted. Try again.");
      return api(path, params, true);
    }
    throw new Error("Admin key not accepted.");
  }
  if (!response.ok) throw new Error(`Server error ${response.status} on ${path}`);
  return response.json();
}

/* ============================================================
   Tooltip: one element. Anything with data-tip shows it on hover
   or keyboard focus; "?" buttons also toggle it on tap.
   ============================================================ */
const tip = $("tip");
function placeTip(x, y) {
  const box = tip.getBoundingClientRect();
  let left = x + 14, top = y - box.height - 10;
  if (left + box.width > innerWidth - 8) left = Math.max(8, x - box.width - 14);
  if (top < 8) top = y + 18;
  tip.style.left = left + "px"; tip.style.top = top + "px";
}
function showTip(html, x, y) { tip.innerHTML = html; tip.classList.add("on"); placeTip(x, y); }
function showTipFor(el) {
  const r = el.getBoundingClientRect();
  showTip(esc(el.dataset.tip), r.left + r.width / 2, r.top);
}
const hideTip = () => tip.classList.remove("on");
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest && e.target.closest("[data-tip]");
  if (el) showTip(esc(el.dataset.tip), e.clientX, e.clientY);
});
document.addEventListener("mousemove", (e) => {
  const el = e.target.closest && e.target.closest("[data-tip]");
  if (el) placeTip(e.clientX, e.clientY);
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest && e.target.closest("[data-tip]")) hideTip();
});
document.addEventListener("focusin", (e) => {
  if (e.target.dataset && e.target.dataset.tip) showTipFor(e.target);
});
document.addEventListener("focusout", hideTip);
document.addEventListener("click", (e) => {
  const q = e.target.closest && e.target.closest("button.q[data-tip]");
  if (q) showTipFor(q);
});
addEventListener("scroll", hideTip, { passive: true });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });

/* ============================================================
   Recordings per day. One series, no legend. Days with no
   recordings are drawn as zero so gaps are visible.
   ============================================================ */
function fillDays(rows, from, to) {
  const have = new Map(rows.map((r) => [r.date, r]));
  const start = new Date(from + "T00:00:00"), end = new Date(to + "T00:00:00");
  if (isNaN(start) || isNaN(end) || (end - start) / 864e5 > 120) return rows;
  const out = [];
  for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const k = ymd(d);
    out.push(have.get(k) || { date: k, recordings: 0, hours: 0 });
  }
  rows.filter((r) => r.date === "undated").forEach((r) => out.push(r));
  return out;
}

function barChart(host, rows) {
  if (!rows.length || rows.every((r) => !r.recordings)) {
    host.innerHTML = empty("No recordings in this range", "Pick a wider range or another clinic.");
    return;
  }
  const W = Math.max(300, host.clientWidth || 600), H = 300;
  const padL = 34, padR = 8, padT = 18, padB = 28;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const max = Math.max(1, ...rows.map((r) => Number(r.recordings) || 0));
  const step = plotW / rows.length;
  const bw = Math.max(3, Math.min(38, step - 6));
  const now = today();

  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, y = padT + plotH - (v / max) * plotH;
    g += `<line class="gridline" x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}"/>`
       + `<text x="${padL - 6}" y="${y + 3.5}" text-anchor="end">${Math.round(v)}</text>`;
  }
  const every = Math.ceil(rows.length / Math.max(3, Math.floor(plotW / 60)));
  const showVals = step >= 26;
  let bars = "";
  rows.forEach((r, i) => {
    const v = Number(r.recordings) || 0;
    const h = v > 0 ? Math.max(2, (v / max) * plotH) : 0;
    const x = padL + i * step + (step - bw) / 2;
    const label = String(r.date || "").slice(5);
    bars += `<g class="col"><rect class="hit" x="${padL + i * step}" y="${padT}" width="${step}"`
      + ` height="${plotH}" data-i="${i}"/>`
      + `<rect class="bar${r.date === now ? " today" : ""}" x="${x}" y="${padT + plotH - h}"`
      + ` width="${bw}" height="${h}" rx="3" ry="3" pointer-events="none"/>`
      + (showVals && v ? `<text class="val" x="${x + bw / 2}" y="${padT + plotH - h - 4}"`
        + ` text-anchor="middle" pointer-events="none">${num(v)}</text>` : "")
      + `</g>`;
    if (i % every === 0 || i === rows.length - 1) {
      bars += `<text x="${padL + i * step + step / 2}" y="${H - 9}" text-anchor="middle">`
        + `${esc(r.date === now ? "today" : label)}</text>`;
    }
  });
  const total = rows.reduce((s, r) => s + (Number(r.recordings) || 0), 0);
  host.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" height="${H}" role="img"`
    + ` aria-label="${esc(`Recordings per day, ${total} in total`)}">${g}`
    + `<line class="axis" x1="${padL}" x2="${W - padR}" y1="${padT + plotH}" y2="${padT + plotH}"/>`
    + bars + `</svg>`;
  host.querySelectorAll("rect.hit").forEach((hit) => {
    const r = rows[Number(hit.dataset.i)];
    hit.addEventListener("mousemove", (e) => showTip(
      `<b>${esc(r.date)}</b><br>${plural(r.recordings, "recording")} · ${num(r.hours)} h`,
      e.clientX, e.clientY));
    hit.addEventListener("mouseleave", hideTip);
  });
}

/* ============================================================
   Status tiles. Click one to filter the table below it.
   ============================================================ */
const STATE_LABELS = {
  "": "All",
  pending: "Waiting for CMED",
  unconfirmed: "Not confirmed",
  confirmed: "Confirmed",
  quarantined: "Held",
  stopped_early: "Stopped early",
  unarchived: "Not archived",
  uncopied: "No cloud copy",
  on_jabra: "On the Jabra",
  not_jabra: "Not on the Jabra",
  no_sound: "No sound",
  refused: "Refused",
  expired: "Expired",
  legacy: "Legacy",
};

function tile({ id, value, label, note, tone, tip: help, pressed }) {
  return `<button type="button" class="tile ${tone || "quiet"}" data-tile="${esc(id)}"`
    + ` aria-pressed="${pressed ? "true" : "false"}"${help ? ` data-tip="${esc(help)}"` : ""}>`
    + `<span class="t-value">${esc(value)}</span>`
    + `<span class="t-label">${esc(label)}</span>`
    + `<span class="t-note">${esc(note || "")}</span></button>`;
}

const tone0 = (n, bad) => (!n ? "good" : bad ? "bad" : "warn");

function activePcs(d) { return (d.recorders.rooms || []).filter((q) => !q.revoked); }

function drawTiles(d) {
  const v = d.volume, c = d.confirmation.totals || {}, i = d.integrity,
        p = d.copies, r = d.recorders;
  const total = v.total_recordings || 0;
  const confirmed = c.confirmed || 0;
  const rate = total ? Math.round((confirmed / total) * 100) : null;
  const pcs = activePcs(d);
  const offline = pcs.filter((q) => q.silent).length;
  const uncopied = p.recordings_without_a_cloud_copy || 0;
  const oldest = p.oldest_uncopied_hours;

  $("kpis").innerHTML = [
    tile({ id: "all", value: num(total), label: "Recordings", tone: "info",
           note: `${num(v.total_hours)} h of audio`,
           tip: "All recordings in the date range. Click to list them." }),
    tile({ id: "confirmed", value: rate == null ? "–" : rate + "%", label: "Confirmed by CMED",
           note: `${num(confirmed)} of ${num(total)}`, pressed: STATE === "confirmed",
           tone: rate == null ? "quiet" : rate >= 98 ? "good" : rate >= 90 ? "warn" : "bad",
           tip: "Matched to a CMED visit (API 2)." }),
    tile({ id: "pending", value: num(c.pending || 0), label: "Waiting for CMED",
           note: "stored, not confirmed", tone: c.pending ? "warn" : "good",
           pressed: STATE === "pending",
           tip: "Saved on the server. CMED has not sent the visit details (API 2) yet." }),
    tile({ id: "unconfirmed", value: num(c.unconfirmed || 0), label: "Not confirmed",
           note: "stored, marked unconfirmed", tone: tone0(c.unconfirmed, true),
           pressed: STATE === "unconfirmed",
           tip: "Stored, not in the dataset yet. Confirmed when CMED's API 2 arrives, or by an administrator." }),
    tile({ id: "quarantined", value: num(i.quarantined_total || 0), label: "Held",
           note: "kept, never deleted", tone: tone0(i.quarantined_total, true),
           pressed: STATE === "quarantined",
           tip: "Quarantined on the server, usually a hash mismatch. Never deleted." }),
    tile({ id: "offline", value: num(offline), label: offline === 1 ? "PC offline" : "PCs offline",
           note: `${num(pcs.length - offline)} of ${num(pcs.length)} online`,
           tone: !pcs.length ? "quiet" : tone0(offline, true), pressed: PC_FILTER === "offline",
           tip: "No heartbeat for 3 min or more." }),
    tile({ id: "uncopied", value: num(uncopied), label: "No cloud copy",
           note: oldest != null ? `oldest ${Math.round(oldest)} h` : "all copied",
           tone: !uncopied ? "good" : oldest != null && oldest > 48 ? "bad" : "warn",
           pressed: STATE === "uncopied",
           tip: "Archived on the server but not yet copied to the cloud. All dates." }),
    tile({ id: "alerts", value: num(i.alerts_total || 0), label: "Alerts",
           note: r.held && r.held.length ? `${num(r.held.length)} held on PCs` : "integrity alerts",
           tone: tone0(i.alerts_total, true),
           tip: "Integrity alerts raised in the date range. Click to see types." }),
  ].join("");
}

$("kpis").addEventListener("click", (e) => {
  const t = e.target.closest("[data-tile]");
  if (!t) return;
  const id = t.dataset.tile;
  if (id === "offline") { setPcFilter(PC_FILTER === "offline" ? "all" : "offline"); go("pcsPanel"); return; }
  if (id === "alerts") { go("problemsPanel"); return; }
  setState(id === "all" ? "" : (STATE === id ? "" : id));
  go("recPanel");
});

function go(id) {
  const el = $(id);
  if (!el) return;
  // Open the section it is in first, as a step the Back button undoes.
  const view = el.closest(".view");
  if (view && view.hidden) {
    history.pushState(null, "", "#" + view.dataset.view);
    showView(view.dataset.view);
  }
  const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
}

/* ============================================================
   Sections: one shown at a time, named in the address (#recordings),
   so Back, a bookmark and a shared link all land in the right place.
   ============================================================ */
const VIEWS = {
  overview: ["Overview", "How the clinics, the recordings and the server are doing", true],
  recordings: ["Recordings", "Every consultation, its confirmation, and where its audio is", true],
  events: ["Event log", "Every start, end, pause, refusal, microphone change, CMED message and alert", true],
  pcs: ["PCs", "The recorders in the consulting rooms", true],
  patient: ["Patients", "One patient's visits, newest first: demographics, prescriptions and recordings", false],
  shift: ["My shift", "Your shift, and the alerts mailed to you", false],
  watch: ["My alerts", "The hospitals you look after: alerts, and the evening report", false],
  downloads: ["Downloads", "Recordings in numbered batches, one ZIP each", false],
  cmed: ["CMED", "CMED's EHR addresses and the key each hospital signs with", false],
  people: ["People", "Who may sign in, and their database logins", false],
};

function showView(name) {
  const link = document.querySelector(`#nav a[data-view="${name}"]`);
  if (!VIEWS[name] || !link || link.hidden) name = "overview";
  document.querySelectorAll("main .view").forEach((v) => { v.hidden = v.dataset.view !== name; });
  document.querySelectorAll("#nav a").forEach((a) => {
    if (a.dataset.view === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  const [title, sub, filtered] = VIEWS[name];
  $("viewTitle").textContent = title;
  $("viewSub").textContent = sub;
  $("filters").hidden = !filtered;
  document.title = `${title} - AIMScribe`;
  // The chart is sized to its panel, which has no width while hidden.
  if (name === "overview" && LAST) drawChart(LAST);
}

addEventListener("hashchange", () => {
  const name = location.hash.slice(1);
  if (VIEWS[name]) showView(name);          // "#main" is the skip link, not a section
});

function setBadge(id, n, bad) {
  const b = $(id);
  if (!b) return;
  b.hidden = !n;
  b.textContent = n > 99 ? "99+" : String(n || "");
  b.className = "badge" + (bad ? " bad" : "");
}

function drawBadges(d) {
  const c = d.confirmation.totals || {}, i = d.integrity;
  setBadge("badge-overview", i.alerts_total || 0, true);
  setBadge("badge-recordings", (c.unconfirmed || 0) + (i.quarantined_total || 0), true);
  setBadge("badge-pcs", activePcs(d).filter((q) => q.silent).length, false);
}

/* ============================================================
   Clinics: one card each. Click to filter the whole page.
   ============================================================ */
function drawClinics(d) {
  const byClinic = new Map((d.volume.by_clinic || []).map((c) => [c.hospital_id, c]));
  const max = Math.max(1, ...[...byClinic.values()].map((c) => c.recordings || 0));
  const current = $("clinic").value;
  const rooms = d.recorders.rooms || [];
  const held = d.recorders.held || [];
  const waiting = new Map();
  (d.confirmation.by_day || []).forEach((row) => {
    waiting.set(row.hospital_id, (waiting.get(row.hospital_id) || 0) + (row.pending || 0));
  });

  if (!CLINICS.length) {
    $("clinicBoard").innerHTML = empty("No clinics registered", "Add a hospital on the server first.");
    return;
  }
  $("clinicBoard").innerHTML = CLINICS.map((c) => {
    const id = c.hospital_id;
    const row = byClinic.get(id) || { recordings: 0, hours: 0 };
    const pcs = rooms.filter((q) => q.hospital_id === id && !q.revoked);
    const off = pcs.filter((q) => q.silent).length;
    const h = held.filter((x) => x.hospital_id === id).length;
    const w = waiting.get(id) || 0;
    const pct = Math.round(((row.recordings || 0) / max) * 100);
    const pcTag = !pcs.length ? tag("quiet", "no PCs")
      : off ? tag("bad", `${off} of ${pcs.length} PCs offline`)
      : tag("good", `${plural(pcs.length, "PC")} online`);
    return `<button type="button" class="clinic${current && current !== id ? " dim" : ""}"`
      + ` data-id="${esc(id)}" aria-pressed="${current === id}">`
      + `<span class="top-row"><span class="name">${esc(c.name || id)}</span>`
      + `<span class="n">${num(row.recordings)}</span></span>`
      + `<span class="sub"><span class="code">${esc(id)}</span>·<span>${num(row.hours)} h</span></span>`
      + `<span class="meter" aria-hidden="true"><i data-pct="${pct}"></i></span>`
      + `<span class="tags">${pcTag}`
      + (w ? tag("warn", `${num(w)} waiting`) : "")
      + (h ? tag("bad", `${num(h)} held on PC`) : "")
      + `</span></button>`;
  }).join("");
  // Widths through the DOM: the page policy allows no inline style attribute.
  $("clinicBoard").querySelectorAll(".meter i").forEach((bar) => {
    bar.style.width = `${bar.dataset.pct}%`;
  });
}

$("clinicBoard").addEventListener("click", (e) => {
  const el = e.target.closest(".clinic");
  if (!el) return;
  const id = el.dataset.id;
  $("clinic").value = ($("clinic").value === id) ? "" : id;   // click again to clear
  refresh();
});

/* ============================================================
   Needs attention: number first, short label, "?" for detail.
   ============================================================ */
function drawAttention(d) {
  const c = d.confirmation.totals || {}, i = d.integrity,
        x = d.reconciliation, p = d.copies;
  const items = [
    { n: c.unconfirmed || 0, what: "Not confirmed", bad: true, state: "unconfirmed",
      tip: "Stored, marked unconfirmed: no details from CMED yet." },
    { n: x.records_without_a_recording || 0, what: "CMED visit, no recording", bad: true,
      tip: "CMED sent a visit (older than 15 min) that has no recording." },
    { n: x.recordings_without_a_record || 0, what: "Recording, nothing from CMED",
      state: "pending", tip: "Waiting for CMED plus not confirmed. Click lists the waiting ones." },
    { n: i.quarantined_total || 0, what: "Held on server", bad: true, state: "quarantined",
      tip: "A hash did not match. Kept, never deleted." },
    { n: i.stopped_early_total || 0, what: "Stopped early", state: "stopped_early",
      tip: "Closed for a reason other than the prescription being built." },
    { n: p.recordings_without_a_cloud_copy || 0, what: "No cloud copy", state: "uncopied",
      tip: "On the server archive only. All dates." },
    { n: p.pieces_older_than_48h || 0, what: "Pieces in transit > 48 h",
      tip: "Audio pieces still in the segment store after 48 h. All dates." },
    { n: i.alerts_total || 0, what: "Integrity alerts", bad: true, go: "problemsPanel",
      tip: "Raised in this date range. See Problems by type." },
  ];
  if (items.every((it) => !it.n)) {
    $("attention").innerHTML = empty("All clear", "Nothing needs attention in this range.", true);
    return;
  }
  // Problems first, then the zeros.
  items.sort((a, b) => (b.n ? 1 : 0) - (a.n ? 1 : 0));
  $("attention").innerHTML = items.map((it) => {
    const tone = !it.n ? "good" : it.bad ? "bad" : "warn";
    const open = it.n && (it.state || it.go);
    const inner = `<span class="n">${num(it.n)}</span><span class="what">${esc(it.what)}</span>`
      + (open ? `<span class="go" aria-hidden="true">List &rsaquo;</span>` : `<span></span>`);
    return open
      ? `<button type="button" class="att-row ${tone}" data-tip="${esc(it.tip)}"`
        + (it.state ? ` data-state="${esc(it.state)}"` : ` data-go="${esc(it.go)}"`)
        + `>${inner}</button>`
      : `<div class="att-row ${tone}" data-tip="${esc(it.tip)}">${inner}</div>`;
  }).join("");
}

$("attention").addEventListener("click", (e) => {
  const el = e.target.closest("[data-state],[data-go]");
  if (!el) return;
  if (el.dataset.go) { go(el.dataset.go); return; }
  setState(el.dataset.state);
  go("recPanel");
});

/* ============================================================
   Problems by type: why recordings were held or stopped, and
   which alerts were raised.
   ============================================================ */
function drawProblems(d) {
  const i = d.integrity;
  const q = i.quarantined || [], s = i.stopped_early || [], a = i.alerts || [];
  const total = (i.quarantined_total || 0) + (i.stopped_early_total || 0) + (i.alerts_total || 0);
  $("probHint").textContent = total ? `${num(total)} in range` : "";
  if (!q.length && !s.length && !a.length) {
    $("problems").innerHTML = empty("No problems", "No held recordings, early stops or alerts in this range.", true);
    return;
  }
  const who = (r) => `<small>${esc(clinicName(r.hospital_id))}${r.doctor_id ? " · " + esc(r.doctor_id) : ""}</small>`;
  let html = "";
  if (q.length) {
    html += `<div class="group-title">Held on server</div><ul class="list">`
      + q.map((r) => `<li><span class="n">${num(r.n)}</span>`
        + `<span>${tag("bad", "Held: " + human(r.quarantine_reason))} ${who(r)}</span></li>`).join("")
      + `</ul>`;
  }
  if (s.length) {
    html += `<div class="group-title">Stopped early</div><ul class="list">`
      + s.map((r) => `<li><span class="n">${num(r.n)}</span>`
        + `<span>${tag("warn", (ENDED[r.close_reason] || [0, human(r.close_reason)])[1])} ${who(r)}</span></li>`).join("")
      + `</ul>`;
  }
  if (a.length) {
    html += `<div class="group-title">Alerts</div><ul class="list">`
      + a.map((r) => {
          const sev = String(r.severity || "").toLowerCase();
          const tone = /crit|error|high/.test(sev) ? "bad" : /warn/.test(sev) ? "warn" : "info";
          return `<li><span class="n">${num(r.n)}</span>`
            + `<span>${tag(tone, human(r.alert_type))} <small>${esc(sev)}</small></span></li>`;
        }).join("") + `</ul>`;
  }
  $("problems").innerHTML = html;
}

/* ============================================================
   Archive and cloud copy, and the last restore test.
   ============================================================ */
function drawCopies(d) {
  const p = d.copies;
  const n = p.recordings_without_a_cloud_copy || 0;
  const oldest = p.oldest_uncopied_hours;
  const pieces = p.pieces_older_than_48h || 0;
  const rt = p.last_restore_test || {};
  const detail = rt.detail || {};
  const rtAge = minutesSince(rt.at);
  const rtTone = !rt.at ? "warn" : rtAge > 90 * 1440 ? "warn" : "good";
  const rtText = rt.at
    ? `${when(rt.at)}${detail.result ? " · " + detail.result : ""}${detail.by ? " · " + detail.by : ""}`
    : "No restore test recorded";
  const rtSub = [detail.what, detail.notes].filter(Boolean).join(" · ");
  $("copies").innerHTML = `<div class="kv">`
    + `<div class="kv-row ${n ? (oldest > 48 ? "bad" : "warn") : "good"}"><span class="n">${num(n)}</span>`
    + `<span class="what">No cloud copy<small>${oldest != null ? `oldest ${Math.round(oldest)} h` : "every archived recording is copied"}</small></span></div>`
    + `<div class="kv-row ${pieces ? "warn" : "good"}"><span class="n">${num(pieces)}</span>`
    + `<span class="what">Pieces in transit &gt; 48 h<small>segment store</small></span></div>`
    + `<div class="kv-row ${rtTone}"><span class="n">${rt.at ? esc(ago(rtAge).replace(" ago", "")) : "–"}</span>`
    + `<span class="what">Last restore test<small>${esc(rtText)}${rtSub ? "<br>" + esc(rtSub) : ""}</small></span></div>`
    + `</div>`;
}

/* ============================================================
   PCs (recorders), grouped by clinic.
   ============================================================ */
const PC_FILTERS = [
  // 24 Nov 2026: revoked PCs are hidden unless "Revoked" is chosen - since
  // the beta split every test PC is revoked here and lives on beta.
  ["all", "All", (q) => !q.revoked],
  ["approval", "Waiting for approval", (q) => q.approval === "pending" && !q.revoked],
  ["offline", "Offline", (q) => q.silent && !q.revoked],
  ["outdated", "Needs update", (q) => q.outdated && !q.revoked],
  ["unencrypted", "Disk not encrypted", (q) => q.disk_encrypted === false && !q.revoked],
  ["microphone", "Not on the Jabra", (q) => q.microphone_is_clinic === false && !q.revoked],
  ["holding", "Audio waiting", (q) => (q.pending_segments || 0) > 0],
  ["held", "Holding a recording", (q) => (q.held_count || 0) > 0],
  ["blocked", "Blocked", (q) => q.blocked],
  ["revoked", "Revoked", (q) => q.revoked],
];

function setPcFilter(f) {
  PC_FILTER = f;
  if (LAST) { drawPcFilters(LAST); drawRooms(LAST); drawTiles(LAST); }
}

function drawPcFilters(d) {
  const rooms = d.recorders.rooms || [];
  $("pcFilters").innerHTML = PC_FILTERS.map(([id, label, test]) => {
    const n = rooms.filter(test).length;
    if (id !== "all" && !n && PC_FILTER !== id) return "";
    return `<button type="button" class="chip" data-pc="${id}" aria-pressed="${PC_FILTER === id}">`
      + `<b>${num(n)}</b> ${esc(label)}</button>`;
  }).join("");
}
$("pcFilters").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pc]");
  if (b) setPcFilter(b.dataset.pc === PC_FILTER ? "all" : b.dataset.pc);
});

function offlineText(periods) {
  if (!periods || !periods.length) return `<span class="hint">none</span>`;
  const at = (iso) => new Date(iso).toLocaleTimeString([],
    { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Dhaka" });
  const day = (iso) => new Date(iso).toLocaleDateString([],
    { day: "numeric", month: "short", timeZone: "Asia/Dhaka" });
  return periods.map((p) => {
    const mic = p.microphone === "fine" ? tag("good", "mic OK")
      : tag("warn", "mic " + p.microphone);
    return `<span>${esc(day(p.from))} ${esc(at(p.from))}–${esc(at(p.to))}`
      + ` · ${plural(p.consultations, "consultation")} ${mic}</span>`;
  }).join("<br>");
}

function drawRooms(d) {
  const all = d.recorders.rooms || [];
  if (!all.length) {
    $("rooms").innerHTML = empty("No PCs enrolled", "A PC appears here after it enrols with this server.");
    return;
  }
  const test = (PC_FILTERS.find((f) => f[0] === PC_FILTER) || PC_FILTERS[0])[2];
  const rooms = all.filter(test);
  if (!rooms.length) {
    $("rooms").innerHTML = empty("No PCs match", "Clear the filter to see all PCs.", true);
    return;
  }
  const minV = d.recorders.minimum_version;
  const groups = new Map();
  rooms.forEach((q) => {
    if (!groups.has(q.hospital_id)) groups.set(q.hospital_id, []);
    groups.get(q.hospital_id).push(q);
  });
  const COLS = 10;
  let body = "";
  groups.forEach((list, hid) => {
    const live = list.filter((q) => !q.revoked);
    const off = live.filter((q) => q.silent).length;
    body += `<tr class="grp"><th colspan="${COLS}">${esc(clinicName(hid))}`
      + `<span class="hint">${plural(live.length, "PC")}${off ? ` · ${off} offline` : " · all online"}</span></th></tr>`;
    // Offline first inside each clinic.
    list.sort((a, b) => (b.silent && !b.revoked) - (a.silent && !a.revoked));
    body += list.map((q) => {
      const waits = q.approval === "pending" && !q.revoked;
      const state = (q.blocked ? tag("bad", "Blocked", q.blocked_reason || "")
          + `<br><button type="button" class="chip" data-decide="unblock" data-device="${esc(q.device_id)}">Unblock</button>`
        : q.revoked ? tag("quiet", "Revoked")
        : q.silent ? tag("bad", "Offline") : tag("good", "Online"))
        + (waits ? " " + tag("warn", "Waiting for CMED",
          "Registered itself. Approved when CMED confirms 2 of its recordings, or here.")
          + `<br><button type="button" class="chip" data-decide="approve" data-device="${esc(q.device_id)}">Approve</button>`
          + ` <button type="button" class="chip" data-decide="reject" data-device="${esc(q.device_id)}">Reject</button>`
          : "");
      const version = q.outdated
        ? tag("warn", `${q.app_version || "?"} · update`, `Below ${minV}`)
        : esc(q.app_version || "–");
      const disk = q.disk_encrypted === true ? tag("good", "Encrypted")
        : q.disk_encrypted === false ? tag("bad", "Not encrypted")
        : `<span class="hint">not reported</span>`;
      const key = q.key === "tpm" ? tag("good", "TPM chip", "Its key is inside the PC's security chip: it cannot be copied off this PC")
        : q.key === "software" ? tag("warn", "Software key", "No usable TPM chip (or joined before 3.6): its key is locked to this PC by Windows, but a person with full control of the PC could copy it. Revoke the PC here if it is ever lost or misused.")
        : `<span class="hint">–</span>`;
      const mic = !q.microphone ? `<span class="hint">not reported</span>`
        : q.microphone_is_clinic ? tag("good", "Jabra", q.microphone)
        : tag("warn", "PC microphone", `${q.microphone} - the Jabra is not plugged in`);
      const off = q.clock_offset_seconds;
      const clock = off == null ? `<span class="hint">–</span>`
        : Math.abs(off) >= 120 ? tag("warn", `${off > 0 ? "+" : ""}${num(off)} s`, "Server time minus PC time")
        : `${off > 0 ? "+" : ""}${num(off)} s`;
      const held = q.held_count
        ? tag("bad", `${num(q.held_count)} held`, (q.held || []).map((h) => human(h.code)).join(", "))
        : `<span class="hint">0</span>`;
      const waiting = q.pending_segments
        ? `<b>${num(q.pending_segments)}</b> pieces · ${num(q.spool_mb)} MB`
        : `<span class="hint">0 · ${num(q.spool_mb)} MB</span>`;
      const rowTone = q.revoked ? "" : q.silent || q.held_count ? "row-bad"
        : waits ? "row-warn"
        : q.outdated || q.disk_encrypted === false || q.microphone_is_clinic === false
          ? "row-warn" : "";
      const move = q.revoked ? "" : `<br><button type="button" class="chip" data-move="${esc(q.device_id)}"`
        + ` data-from="${esc(q.hospital_id)}" title="Move this PC to another clinic">Move</button>`;
      return `<tr class="${rowTone}">`
        + td("PC", `<b>${esc(q.machine_name || String(q.device_id).slice(0, 8))}</b>${move}`)
        + td("State", state)
        + td("Last seen", esc(ago(q.minutes_since_seen)))
        + td("Version", version)
        + td("Audio waiting", waiting, "num")
        + td("Held", held, "num")
        + td("Microphone", mic)
        + td("Disk", disk)
        + td("Key", key)
        + td("Clock", clock, "num")
        + td("Offline (7 days)", offlineText(q.offline_periods))
        + `</tr>`;
    }).join("");
  });
  $("rooms").innerHTML = `<table><thead><tr><th>PC</th><th>State</th><th>Last seen</th>`
    + `<th>Version</th><th class="num">Audio waiting</th><th class="num">Held</th><th>Microphone</th><th>Disk</th><th>Key</th>`
    + `<th class="num">Clock</th><th>Offline (7 days)</th></tr></thead><tbody>${body}</tbody></table>`;
}

// Move a PC to another clinic: a PC never moves itself.
$("rooms").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-move]");
  if (!b) return;
  const ids = CLINICS.map((c) => c.hospital_id).filter((h) => h !== b.dataset.from);
  const to = (window.prompt(`Move this PC from ${clinicName(b.dataset.from)} to which clinic?\n`
    + `Type one of: ${ids.join(", ")}`) || "").trim();
  if (!to) return;
  if (!ids.includes(to)) { window.alert(`No clinic "${to}".`); return; }
  const again = window.confirm("Was this PC reinstalled, or its AIMScribe folder cleared?\n\n"
    + "OK = yes: let it register again at the new clinic.\nCancel = no: it keeps working as it is.");
  const who = (window.prompt("Your name, for the audit log:") || "").trim();
  if (who.length < 2) return;
  if (!PORTAL && !ADMIN_KEY) await askKey();
  b.disabled = true;
  try {
    const response = await fetch(`/api/v2/admin/device/${encodeURIComponent(b.dataset.move)}/move`,
      { method: "POST", headers: { ...portalHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ moved_by: who, hospital_id: to, register_again: again }) });
    if (!response.ok) throw new Error(`Server error ${response.status}`);
    await refresh();
  } catch (err) {
    b.disabled = false;
    window.alert(`Not moved: ${err.message}`);
  }
});

// A PC that registered itself: a person approves or rejects it, by name.
$("rooms").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-decide]");
  if (!b) return;
  const approve = b.dataset.decide === "approve";
  const unblock = b.dataset.decide === "unblock";
  const who = (window.prompt(`${approve ? "Approve" : unblock ? "Unblock" : "Reject"} this PC. Your name, for the audit log:`) || "").trim();
  if (who.length < 2) return;
  if (unblock && !window.confirm("Unblock this PC? It becomes pending again, and is approved once CMED confirms two of its new recordings.")) return;
  if (!approve && !unblock && !window.confirm("Reject and revoke this PC? Anything it recorded that CMED did not confirm is erased on its date.")) return;
  if (!PORTAL && !ADMIN_KEY) await askKey();
  b.disabled = true;
  try {
    const response = await fetch(`/api/v2/admin/device/${encodeURIComponent(b.dataset.device)}/${b.dataset.decide}`,
      { method: "POST", headers: { ...portalHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ decided_by: who }) });
    if (!response.ok) throw new Error(`Server error ${response.status}`);
    await refresh();
  } catch (err) {
    b.disabled = false;
    window.alert(`Not done: ${err.message}`);
  }
});

/* ============================================================
   Held on PCs: kept on a PC because this server said no.
   ============================================================ */
function drawHeld(d) {
  const held = (d.recorders && d.recorders.held) || [];
  $("heldHint").textContent = held.length ? plural(held.length, "recording") : "";
  if (!held.length) {
    $("held").innerHTML = empty("Nothing held", "No PC is keeping a refused recording.", true);
    return;
  }
  $("held").innerHTML = `<table><thead><tr><th>Reason</th><th>PC</th><th>Clinic</th>`
    + `<th>Since</th><th>Recording</th></tr></thead><tbody>`
    + held.map((h) => `<tr class="row-bad">`
        + td("Reason", tag("bad", "Held: " + (human(h.code) || "unknown")))
        + td("PC", esc(h.machine_name || ""))
        + td("Clinic", esc(clinicName(h.hospital_id)))
        + td("Since", h.since ? `${esc(ago(minutesSince(h.since)))}<span class="sub2">${esc(when(h.since))}</span>` : "–")
        + td("Recording", `<span class="mono" title="${esc(h.session_id || "")}">${esc(shortId(h.session_id))}</span>`)
        + `</tr>`).join("")
    + `</tbody></table>`;
}

/* ============================================================
   Needs review: labels the rules could not settle.
   ============================================================ */
function drawReview(d) {
  const items = (d.review && d.review.items) || [];
  $("reviewHint").textContent = items.length ? plural(items.length, "open item") : "";
  if (!items.length) {
    $("review").innerHTML = empty("Nothing to review", "Every label in the archive is settled.", true);
    return;
  }
  $("review").innerHTML = `<table><thead><tr><th>Why</th><th>Raised</th><th>Recording</th>`
    + `<th>Files and rule</th></tr></thead><tbody>`
    + items.map((it) => {
        const why = it.alert_type === "relabelled_after_archive"
          ? tag("warn", "Relabelled after archive") : tag("warn", "Rules unsure");
        const parts = (it.parts || []).map((p) =>
          `${esc(p.file_stem || p.patient_id)} ${tag("quiet", p.rule || "")}`
          + (p.needs_review ? " " + tag("bad", "check") : "")).join("<br>")
          || esc(it.file_stem || "–");
        return `<tr class="row-warn">`
          + td("Why", why)
          + td("Raised", `${esc(ago(minutesSince(it.raised_at)))}<span class="sub2">${esc(when(it.raised_at))}</span>`)
          + td("Recording", `${esc(clinicName(it.hospital_id))}`
            + `<span class="sub2">${it.doctor_id ? esc(it.doctor_id) + " · " : ""}`
            + `<span class="mono">${esc(shortId(it.session_id))}</span></span>`)
          + td("Files", parts, "wrap")
          + `</tr>`;
      }).join("") + `</tbody></table>`;
}

/* ============================================================
   Recordings table, with the folder to delete by hand.
   ============================================================ */
const CONFIRM = {
  confirmed: ["good", "Confirmed"], pending: ["warn", "Waiting for CMED"],
  unconfirmed: ["bad", "Not confirmed"], refused: ["quiet", "Refused"],
  expired: ["quiet", "Expired"], legacy: ["quiet", "Legacy"],
};
const confirmTag = (v) => {
  const m = CONFIRM[v] || ["quiet", v || "–"];
  return tag(m[0], m[1]);
};
// How a recording ended. Green: the two ordinary ends - the next patient, or
// the consultation finished (dashboard.py, _ENDED_NORMALLY). Everything else is
// "stopped early", and alerts the collectors and researchers.
const ENDED = {
  superseded_by_new_patient: ["good", "Next patient"],
  consultation_ended: ["good", "CMED closed the visit (API 4)"],
  counselling_limit: ["good", "2 min after prescription"],
  prescription_built: ["good", "Prescription"],
  doctor_stopped: ["good", "Consultation finished"],
  consultation_finished: ["good", "Consultation finished"],
  patient_did_not_consent: ["bad", "Patient did not consent"],
  "doctor_stopped:patient_left": ["warn", "Patient left the room"],
  "doctor_stopped:urgent_interruption": ["warn", "Emergency or urgent interruption"],
  "doctor_stopped:technical_problem": ["bad", "Recorder or microphone problem"],
  "doctor_stopped:other": ["warn", "Stopped: other reason"],
  patient_left: ["warn", "Patient left the room"],
  urgent_interruption: ["warn", "Emergency or urgent interruption"],
  technical_problem: ["bad", "Recorder or microphone problem"],
  recovered_after_unclean_shutdown: ["bad", "Recorder crashed, recovered"],
  no_activity_limit: ["warn", "Nobody pressed Stop"],
  stopped_from_tray: ["warn", "Stopped by the doctor"],
  pc_shutdown: ["bad", "PC shut down"],
  agent_shutdown: ["bad", "Recorder closed"],
  software_update: ["quiet", "Recorder updated"],
};
const timeOf = (v) => { const m = /(\d{2}:\d{2})/.exec(String(v || "")); return m ? m[1] : ""; };

function filesText(r) {
  const files = r.files || [];
  if (!files.length) return esc(r.patient_id || "–");
  return files.map((f) => {
    const from = files.length > 1
      ? ` <span class="hint">from ${num(Math.round(f.start_seconds || 0))} s</span>` : "";
    const flag = f.needs_review ? " " + tag("warn", "review") : "";
    return `${esc(f.patient_id)} ${tag("quiet", f.rule || "")}${from}${flag}`;
  }).join("<br>");
}

function storedTag(r) {
  if (r.archived && r.copied) return tag("good", "Archive + cloud");
  if (r.archived) return tag("warn", "Archive only");
  return tag("warn", "Not archived");
}

/* The audio, decrypted from the cloud copy by the server as it downloads.
   Only for someone signed in: each download is logged against their email.
   One link per patient file when a recording was cut into several. */
function audioLinks(r) {
  if (PORTAL && PORTAL.role === "collector") return `<span class="hint">view only</span>`;
  if (!r.copied || r.confirmation !== "confirmed") return `<span class="hint">–</span>`;
  if (!PORTAL) return `<span class="hint">sign in</span>`;
  // 049: a researcher downloads only the hospitals assigned to them.
  if (PORTAL.role === "researcher" && !(PORTAL.download_hospitals || []).includes(r.hospital_id)) {
    return `<span class="hint" title="You can download only the hospitals assigned to you`
      + ` (People page)">Not your hospital</span>`;
  }
  const base = `/api/v2/dashboard/recordings/${encodeURIComponent(r.session_id)}/audio`;
  const files = r.files || [];
  const link = (href, label, title) =>
    `<a class="copy dl" href="${esc(href)}" title="${esc(title)}">${esc(label)}</a>`;
  // 17 Nov: the clinical record CMED sent (JSON), decrypted the same way, for
  // the same people: researchers and admins (a data collector stopped above).
  const record = files.length > 1
      ? "<br>" + files.map((f) => link(`${recordUrl(r)}?part=${f.part_no}`, `Record ${f.part_no}`,
          `Download the clinical record of patient file ${f.part_no} (JSON)`)).join(" ")
      : "<br>" + link(recordUrl(r), "Record (JSON)", "Download the clinical record CMED sent (JSON)");
  // 049: CMED staff take the clinical record only, never the audio.
  if (PORTAL.role === "cmed") return record.slice("<br>".length);
  if (files.length > 1) {
    return files.map((f) => link(`${base}?part=${f.part_no}`, `Part ${f.part_no}`,
      `Download patient file ${f.part_no} (WAV)`)).join(" ") + record;
  }
  return link(base, "Audio (WAV)", "Download the WAV") + record;
}

function recordUrl(r) {
  return `/api/v2/dashboard/recordings/${encodeURIComponent(r.session_id)}/record`;
}

/* 036: which microphone a recording used - the Jabra, the PC's own, or both. */
function micTag(m) {
  if (!m || m.kind === "unknown") return `<span class="hint">not reported</span>`;
  const names = (m.names || []).join(", ");
  return tag(m.kind === "jabra" ? "good" : "warn", m.label, names);
}

/* 18 Nov: when CMED's API 2 / API 3 for this recording's visit reached us. */
function cmedArrival(r, iso, what) {
  if (!r.cmed_matched) return `<span class="hint">–</span>`;
  if (!iso) return tag("warn", "not received", `CMED's ${what} for this visit has not reached the server`);
  const at = new Date(iso);
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Dhaka" });
  const day = at.toLocaleDateString("en-CA", { timeZone: "Asia/Dhaka" });
  const other = r.date && day !== String(r.date).slice(0, 10);
  return tag("good", other ? `${day} ${time}` : time, `CMED's ${what} arrived ${at.toLocaleString([], { timeZone: "Asia/Dhaka" })}`);
}

/* 20 Nov: each pause - when it began and how long - without the reason. */
function pauseLine(r) {
  const list = r.pause_list || [];
  if (!list.length) return "";
  const at = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Dhaka" });
  const parts = list.map((p) => `${p.at ? at(p.at) : "?"} · ${num(p.minutes)} min${p.open ? " (still)" : ""}`);
  return `<span class="sub2" title="Paused by the doctor: nothing was recorded then">`
    + `Paused ${list.length}×: ${esc(parts.join(", "))}</span>`;
}

/* 26 Nov: the doctor's Stop and Pause choices on the recorder's buttons, in
   order, each with its Dhaka time and its place in the audio. They never
   stopped the recording; they arrive with the recording when it closes. */
function doctorLog(r) {
  const list = r.doctor_log || [];
  if (!list.length) {
    return r.status === "active"
      ? `<span class="hint" title="The doctor's choices arrive with the recording when it closes">after it closes</span>`
      : `<span class="hint">–</span>`;
  }
  const at = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Dhaka" }) : "?";
  const mmss = (s) => s == null ? "" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  return `<span class="tags">` + list.map((e) => {
    const tone = e.button === "Stop" ? "bad" : "warn";
    const words = e.button === "Pause ended" ? "Pause ended" : `${e.button}: ${e.reason}`;
    const where = e.audio_seconds == null ? "" : ` · ${mmss(e.audio_seconds)}`;
    return tag(tone, `${words}${where}`, `${at(e.at)} (Dhaka)${e.reason ? " - " + e.reason : ""}. The recording continued.`);
  }).join("") + `</span>`;
}

function recordingsTable(list) {
  if (!list.length) {
    return STATE
      ? empty(`No recordings: ${STATE_LABELS[STATE] || human(STATE)}`, "Nothing in this state for this range.", true)
      : empty("No recordings in this range", "Pick a wider range or another clinic.");
  }
  const showClinic = !$("clinic").value;
  return `<table><thead><tr><th>Date</th>${showClinic ? "<th>Clinic</th>" : ""}<th>PC</th><th>Doctor</th>`
    + `<th>Patient files and rule</th><th>Visit</th><th class="num">Length</th><th>Microphone</th>`
    + `<th>CMED</th><th>Demographics</th><th>Prescription</th><th>Status</th><th>Doctor's log</th>`
    + `<th>Stored</th><th>Download</th><th>Pieces</th><th>Folder on server</th></tr></thead><tbody>`
    + list.map((r) => {
        const rel = r.archive_relpath || "";
        const folder = rel ? rel.replace(/\/[^/]*$/, "") : "";
        const t = timeOf(r.local_start);
        const status = [];
        if (r.quarantine_reason) status.push(tag("bad", "Held: " + human(r.quarantine_reason)));
        if (r.close_reason) {
          const [tone, text] = ENDED[r.close_reason] || ["warn", "Stopped: " + human(r.close_reason)];
          status.push(tag(tone, text));
        }
        // 18 Nov: open, but its PC has gone quiet - the audio waits on that PC.
        if (r.on_pc_offline_since) {
          status.push(tag("warn", "Waiting on the PC (offline since "
            + esc(r.on_pc_offline_since.replace("T", " ").slice(0, 16)) + " UTC)",
            "The PC went offline with this recording open. Its audio is on the PC and is sent, and the recording closed, when the PC is back online."));
        }
        // 18 Nov: the doctor has it paused now - nothing is being recorded.
        if (r.paused_since) {
          const at = new Date(r.paused_since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Dhaka" });
          status.push(tag("warn", `Paused since ${at}` + (r.pause_reason ? ` (${human(r.pause_reason)})` : ""),
            "The doctor paused this recording; nothing is recorded until they press Resume."));
        }
        // 20 Nov (recorder 3.9.6+): pieces its PC still holds, by number.
        if (r.pc_waiting && r.pc_waiting.length) {
          status.push(tag("warn", `On the PC: ${r.pc_waiting.length} piece${r.pc_waiting.length === 1 ? "" : "s"} not sent (${esc(pieceRanges(r.pc_waiting))})`,
            "The PC has recorded these pieces but not sent them yet. It keeps trying every 10 seconds; they arrive when it is back online."));
        }
        if (!status.length) status.push(`<span class="hint">${esc(human(r.status) || "–")}</span>`);
        const tone = r.quarantine_reason || r.confirmation === "unconfirmed" ? "row-bad"
          : r.confirmation === "pending" ? "row-warn" : "";
        return `<tr class="${tone}">`
          + td("Date", `${esc(r.date || "")}${t ? `<span class="sub2">${esc(t)}</span>` : ""}`)
          + (showClinic ? td("Clinic", esc(clinicName(r.hospital_id))) : "")
          + td("PC", r.pc ? `<span title="${esc(r.pc_id || "")}">${esc(r.pc)}</span>` : `<span class="hint">–</span>`)
          + td("Doctor", esc(r.doctor_id))
          + td("Patient", filesText(r), "wrap")
          + td("Visit", r.visit_id ? esc(r.visit_id) : `<span class="hint">–</span>`)
          + td("Length", `${num(r.minutes || 0)} min` + pauseLine(r), "num")
          + td("Microphone", micTag(r.microphone))
          + td("CMED", confirmTag(r.confirmation))
          + td("Demographics", cmedArrival(r, r.api2_at, "demographics (API 2)"))
          + td("Prescription", cmedArrival(r, r.api3_at, "prescription (API 3)"))
          + td("Status", `<span class="tags">${status.join("")}</span>`)
          + td("Doctor's log", doctorLog(r), "wrap")
          + td("Stored", storedTag(r))
          + td("Download", audioLinks(r))
          + td("Pieces", piecesButton(r))
          + td("Folder", folder
            ? `<span title="${esc(folder)}">${esc(folder)}</span>`
              + ` <button type="button" class="copy" data-copy="${esc(folder)}">Copy</button>`
            : `<span class="hint">–</span>`, "path")
          + `</tr>`;
      }).join("") + `</tbody></table>`;
}

// 1 2 3 5 7 8 -> "1-3, 5, 7-8"
function pieceRanges(numbers) {
  const out = [];
  let start = null, prev = null;
  for (const n of [...numbers].sort((a, b) => a - b)) {
    if (start !== null && n === prev + 1) { prev = n; continue; }
    if (start !== null) out.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = prev = n;
  }
  if (start !== null) out.push(start === prev ? `${start}` : `${start}-${prev}`);
  return out.join(", ");
}

/* 20 Nov 2026: the pieces log - every piece of a recording, when it was
   recorded, when it arrived, and where it is now. */
function piecesButton(r) {
  if (PORTAL && (PORTAL.role === "collector" || PORTAL.role === "cmed")) {
    return `<span class="hint">view only</span>`;
  }
  return `<button type="button" class="copy" data-pieces="${esc(r.session_id)}"`
    + ` title="${esc(r.pieces_folder || "")}">Pieces (CSV)</button>`;
}
$("recTable").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-pieces]");
  if (b) downloadCsv({ table: "pieces", session_id: b.dataset.pieces }, b, $("piecesMsg"),
    "/api/v2/dashboard/pieces.csv");
});
document.querySelectorAll("[data-pieces-log]").forEach((b) => b.addEventListener("click", () => {
  const from = $("piecesFrom").value, to = $("piecesTo").value;
  if (from && to && from > to) {
    $("piecesMsg").textContent = "From is after To.";
    return;
  }
  downloadCsv({ table: b.dataset.piecesLog, from, to, hospital_id: $("piecesClinic").value },
    b, $("piecesMsg"), "/api/v2/dashboard/pieces.csv");
}));

$("recTable").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-copy]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.copy);
    b.textContent = "Copied";
    setTimeout(() => { b.textContent = "Copy"; }, 1200);
  } catch { b.textContent = "Select it"; }   // clipboard blocked: the path is on screen
});

/* 26 Nov 2026: the chips count exactly what is chosen - clinic, dates, doctor,
   patient or visit - from /recordings/facets; the Overview's totals until it answers. */
let FACETS = null;
function drawStateFilters() {
  const c = (LAST && LAST.confirmation.totals) || {};
  const i = (LAST && LAST.integrity) || {};
  const counts = FACETS ? { "": FACETS.total, ...FACETS } : {
    pending: c.pending, unconfirmed: c.unconfirmed, confirmed: c.confirmed,
    quarantined: i.quarantined_total, stopped_early: i.stopped_early_total,
    on_jabra: i.on_jabra_total, not_jabra: i.not_jabra_total, unarchived: i.unarchived_total,
    uncopied: LAST && LAST.copies.recordings_without_a_cloud_copy,
    refused: c.refused, expired: c.expired, legacy: c.legacy,
  };
  const order = ["", "pending", "unconfirmed", "confirmed", "quarantined", "stopped_early",
                 "on_jabra", "not_jabra", "no_sound", "unarchived", "uncopied", "refused",
                 "expired", "legacy"];
  $("stateFilters").innerHTML = order.map((s) => {
    const n = counts[s];
    if (["refused", "expired", "legacy"].includes(s) && !n && STATE !== s) return "";
    return `<button type="button" class="chip" data-st="${s}" aria-pressed="${STATE === s}">`
      + (n != null ? `<b>${num(n)}</b> ` : "") + `${esc(STATE_LABELS[s])}</button>`;
  }).join("");
}
$("stateFilters").addEventListener("click", (e) => {
  const b = e.target.closest("[data-st]");
  if (b) setState(b.dataset.st);
});

function setState(s) {
  STATE = s || "";
  drawStateFilters();
  if (LAST) drawTiles(LAST);
  loadRecordings();
}

let recSeq = 0;
/* 26 Nov 2026: only the doctors with a recording in the chosen clinic and dates
   (every clinic the viewer may see, with none chosen), with how many. */
function fillRecDoctors(doctors) {
  const sel = $("recDoctor");
  const chosen = sel.value;
  const ids = doctors.map((d) => d.doctor_id);
  sel.innerHTML = `<option value="">All doctors (${num(doctors.length)})</option>`
    + doctors.map((d) => `<option value="${esc(d.doctor_id)}">`
      + `${esc(d.doctor_id)} · ${num(d.recordings)}</option>`).join("");
  sel.value = ids.includes(chosen) ? chosen : "";
  return sel.value !== chosen;           // the chosen doctor has nothing in this range
}
$("recDoctor").addEventListener("change", () => loadRecordings());

/* 26 Nov 2026: a patient ID or a visit ID, exactly - on Enter, or as typing pauses. */
let findTimer = null;
$("recFind").addEventListener("input", () => {
  clearTimeout(findTimer);
  findTimer = setTimeout(() => loadRecordings(), 500);
});
$("recFind").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(findTimer); loadRecordings(); }
});

async function loadRecordings() {
  const clinic = $("clinic").value;
  const label = clinic ? $("clinic").selectedOptions[0].textContent : "All clinics";
  const find = $("recFind").value.trim();
  $("recTitle").textContent = STATE ? `Recordings · ${STATE_LABELS[STATE] || human(STATE)}` : "Recordings";
  const seq = ++recSeq;
  if (!$("recTable").firstChild) $("recTable").innerHTML = empty("Loading");
  try {
    const range = { from: $("from").value, to: $("to").value, hospital_id: clinic };
    // The doctor menu first: a doctor with nothing in this range is let go.
    const facets = await api("/recordings/facets",
                             { ...range, doctor_id: $("recDoctor").value, find });
    if (seq !== recSeq) return;
    if (fillRecDoctors(facets.doctors || [])) {
      FACETS = (await api("/recordings/facets", { ...range, find })).counts || null;
      if (seq !== recSeq) return;
    } else {
      FACETS = facets.counts || null;
    }
    drawStateFilters();
    const doctor = $("recDoctor").value;
    const params = { ...range, limit: 200 };
    if (doctor) params.doctor_id = doctor;
    if (find) params.find = find;
    if (STATE) params.state = STATE;
    const d = await api("/recordings", params);
    if (seq !== recSeq) return;
    const list = d.recordings || [];
    $("recTable").innerHTML = recordingsTable(list);
    $("recHint").textContent = `${label}${doctor ? ` · ${doctor}` : ""}`
      + `${find ? ` · “${find}”` : ""} · ${num(list.length)}`
      + `${list.length >= 200 ? " (first 200)" : ""} · newest first`;
  } catch (err) {
    if (seq !== recSeq) return;
    $("recTable").innerHTML = empty("Could not load recordings", err.message);
  }
}

/* 18 Nov: the event log - what happened to each recording, newest first. */
let EVENT_KIND = "";
let eventSeq = 0;
const EVENT_TONES = { "Patient did not consent": "bad", "Recording stopped unusually": "warn",
  "Paused by the doctor": "warn", "Resumed": "good", "Not received from CMED": "bad",
  "Microphone changed": "warn", "Alert sent": "quiet", "Recording ended": "good" };

function eventParams() {
  return { from: $("from").value, to: $("to").value, hospital_id: $("clinic").value,
           doctor_id: $("eventDoctor").value.trim(), kind: EVENT_KIND };
}

async function loadEvents() {
  const seq = ++eventSeq;
  if (!$("eventsTable").firstChild) $("eventsTable").innerHTML = empty("Loading");
  try {
    const d = await api("/events", eventParams());
    if (seq !== eventSeq) return;
    const list = d.events || [];
    $("eventsHint").textContent = `${num(d.count)} events${d.count > list.length ? ` (newest ${num(list.length)} shown; the CSV has all)` : ""} · newest first`;
    if (!list.length) { $("eventsTable").innerHTML = empty("Nothing in this range", "Pick a wider range or another clinic."); return; }
    const showClinic = !$("clinic").value;
    $("eventsTable").innerHTML = `<table><thead><tr><th>Time</th>${showClinic ? "<th>Clinic</th>" : ""}`
      + `<th>PC</th><th>Doctor</th><th>Patient</th><th>Event</th><th>Detail</th></tr></thead><tbody>`
      + list.map((e) => `<tr>`
        + td("Time", esc(e.time))
        + (showClinic ? td("Clinic", esc(clinicName(e.hospital))) : "")
        + td("PC", esc(e.pc || "–"))
        + td("Doctor", esc(e.doctor || "–"))
        + td("Patient", esc(e.patient_id || "–") + (e.visit_id ? `<span class="sub2">visit ${esc(e.visit_id)}</span>` : ""))
        + td("Event", tag(EVENT_TONES[e.event] || "quiet", e.event))
        + td("Detail", esc(e.detail || ""), "wrap")
        + `</tr>`).join("") + `</tbody></table>`;
  } catch (err) {
    if (seq !== eventSeq) return;
    $("eventsTable").innerHTML = empty("Could not load the event log", err.message);
  }
}

$("eventKinds").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-kind]");
  if (!b) return;
  EVENT_KIND = b.dataset.kind;
  document.querySelectorAll("#eventKinds [data-kind]").forEach((x) =>
    x.setAttribute("aria-pressed", String(x === b)));
  loadEvents();
});
$("eventDoctor").addEventListener("change", loadEvents);

function fileLink(path, params) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  window.location.href = `/api/v2/dashboard${path}?${query}`;
}
$("eventsCsv").addEventListener("click", () => fileLink("/events", { ...eventParams(), format: "csv" }));
$("cmedMissingCsv").addEventListener("click", () => fileLink("/cmed-missing.csv",
  { from: $("from").value, to: $("to").value, hospital_id: $("clinic").value }));
addEventListener("hashchange", () => { if (location.hash === "#events") loadEvents(); });

/* ============================================================
   Refresh, auto-refresh and the "updated" pill.
   ============================================================ */
function notice(text, bad) {
  const n = $("notice");
  n.textContent = text || "";
  n.className = "notice" + (bad ? " bad" : "");
  n.style.display = text ? "block" : "none";
}

function markRanges() {
  const from = $("from").value, to = $("to").value;
  document.querySelectorAll("#ranges [data-range]").forEach((b) => {
    b.setAttribute("aria-pressed", String(to === today() && from === daysAgo(Number(b.dataset.range))));
  });
}

function drawAll(d) {
  drawBadges(d);
  drawTiles(d);
  drawClinics(d);
  drawAttention(d);
  drawProblems(d);
  drawCopies(d);
  drawPcFilters(d);
  drawRooms(d);
  drawHeld(d);
  drawReview(d);
  drawStateFilters();
  drawChart(d);
}

function drawChart(d) {
  const rows = fillDays(d.volume.by_day || [], d.from || $("from").value, d.to || $("to").value);
  $("dayHint").textContent = `${num(d.volume.total_recordings)} · ${num(d.volume.total_hours)} h`;
  barChart($("byDay"), rows);
}

async function refresh() {
  if (BUSY) return;
  BUSY = true;
  markRanges();
  try {
    const d = await api("/summary", { from: $("from").value, to: $("to").value,
                                      hospital_id: $("clinic").value });
    LAST = d;
    drawAll(d);
    await loadRecordings();
    if (!$("eventsTable").closest(".view").hidden) loadEvents();
    if (PORTAL && PORTAL.role === "collector") loadShift();
    FAILED = false;
    UPDATED_AT = Date.now();
    if (!(d.volume.total_recordings)) {
      notice(`No recordings ${$("from").value} to ${$("to").value}`
        + ($("clinic").value ? ` at ${$("clinic").selectedOptions[0].textContent}` : "")
        + ". The server is answering.");
    } else notice("");
  } catch (err) {
    FAILED = true;
    notice(`Update failed: ${err.message}`, true);
  } finally {
    BUSY = false;
    tick();
  }
}

function tick() {
  const el = $("when");
  if (!UPDATED_AT && !FAILED) { el.textContent = "Connecting"; el.className = "live"; return; }
  const s = Math.max(0, Math.round((Date.now() - UPDATED_AT) / 1000));
  const age = !UPDATED_AT ? "never" : s < 60 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
  if (FAILED) { el.textContent = `Update failed · last ${age}`; el.className = "live fail"; return; }
  if (PAUSED) { el.textContent = `Paused · updated ${age}`; el.className = "live paused"; return; }
  el.textContent = `Live · updated ${age}`;
  el.className = "live " + (s > STALE_SECONDS ? "stale" : "ok");
}

setInterval(() => {
  tick();
  if (PAUSED || document.hidden || BUSY || $("keyDialog").open) return;
  if (UPDATED_AT && Date.now() - UPDATED_AT >= AUTO_SECONDS * 1000) refresh();
  else if (FAILED && Date.now() - UPDATED_AT >= AUTO_SECONDS * 1000) refresh();
}, 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !PAUSED && Date.now() - UPDATED_AT >= AUTO_SECONDS * 1000) refresh();
});

$("refresh").addEventListener("click", refresh);
$("pause").addEventListener("click", () => {
  PAUSED = !PAUSED;
  $("pause").setAttribute("aria-pressed", String(PAUSED));
  $("pause").textContent = PAUSED ? "Resume" : "Pause";
  tick();
});
["from", "to", "clinic"].forEach((id) => $(id).addEventListener("change", refresh));
$("ranges").addEventListener("click", (e) => {
  const b = e.target.closest("[data-range]");
  if (!b) return;
  $("from").value = daysAgo(Number(b.dataset.range));
  $("to").value = today();
  refresh();
});
addEventListener("resize", () => {
  clearTimeout(window._rz);
  window._rz = setTimeout(() => { if (LAST) drawChart(LAST); }, 200);
});

/* Light / dark: follows the system until chosen here. */
$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("aims.theme", root.dataset.theme); } catch { /* private mode */ }
});
try {
  const saved = localStorage.getItem("aims.theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch { /* follow the system */ }

/* ============================================================
   Who is signed in, and the People panel (admins only).
   ============================================================ */
async function whoAmI() {
  let reply;
  try { reply = await fetch("/auth/me", { headers: { Accept: "application/json" } }); }
  catch { return; }
  if (reply.status === 404) return;                 // the loopback door: the admin key
  const me = await reply.json().catch(() => ({}));
  if (reply.ok && me.signed_in) {
    PORTAL = { email: me.email, role: me.role, place_id: me.place_id,
               download_hospitals: me.download_hospitals || [] };
    $("who").textContent = `${me.email} \u00b7 ${me.role}`;
    $("who").hidden = false;
    $("signout").hidden = false;
    if (me.role === "admin") {
      $("peoplePanel").hidden = false;
      $("navPeople").hidden = false;
      loadPeople();
      $("originsPanel").hidden = false;
      $("codesPanel").hidden = false;
      $("deviceLogPanel").hidden = false;
      $("clientsPanel").hidden = false;
      $("navCmed").hidden = false;
      loadCmed();
      $("placesPanel").hidden = false;
      $("shiftLogPanel").hidden = false;
      loadPlaces();
    }
    if (me.role === "collector") {
      $("navShift").hidden = false;
      $("piecesPanel").hidden = true;      // a download: researchers and admins
      loadShift();
    }
    if (me.role === "cmed") {
      // 049: CMED staff download the clinical record (JSON) and nothing else.
      $("piecesPanel").hidden = true;
      $("clinicalCsvPanel").hidden = true;
    }
    if (me.role === "researcher" || me.role === "admin") {
      $("navWatch").hidden = false;
      loadWatch(true);
    }
  } else if (me.google) {
    window.location.href = "/auth/login?next=/dashboard";
    throw new Error("Not signed in.");
  }
}

async function adminCall(method, path, body) {
  const response = await fetch(path, {
    method, headers: { ...portalHeaders(), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);
  return data;
}

/* ============================================================
   CMED: the EHR addresses recorders obey, and each hospital's key.
   ============================================================ */
const dhaka = (iso) => (iso ? new Date(iso).toLocaleString([], { timeZone: "Asia/Dhaka" }) : "never");

/* 16 Nov: each clinic's CMED code, and PCs that could not join with the code they sent. */
let CLINIC_CODES = [];
async function loadCodes() {
  let data;
  try { data = await adminCall("GET", "/api/v2/admin/clinic-codes"); }
  catch (err) {
    $("codes").innerHTML = empty("Clinic codes could not be loaded", err.message, true);
    return;
  }
  CLINIC_CODES = data.clinics || [];
  // The connection form chooses among them, or a new one (16 Nov).
  const keep = $("cClinic").value;
  $("cClinic").innerHTML = `<option value="">Choose...</option>`
    + CLINIC_CODES.map((c) => `<option value="${esc(c.hospital_id)}">${esc(c.hospital_id)} ${esc(c.name || "")}`
      + `${c.cmed_hospital_id ? " (now " + esc(c.cmed_hospital_id) + ")" : ""}</option>`).join("")
    + `<option value="__new">A new clinic...</option>`;
  $("cClinic").value = keep;
  $("codes").innerHTML = !CLINIC_CODES.length
    ? empty("No clinic yet", "Clinics appear here once they are set up.", true)
    : `<table><thead><tr><th>Our clinic</th><th>CMED's code for it</th><th>PCs</th>`
      + `<th>Last from CMED</th><th>How recordings end</th><th>API 4 last seen</th></tr></thead><tbody>`
      + CLINIC_CODES.map((c) => `<tr>`
        + td("Our clinic", `<b><code>${esc(c.hospital_id)}</code></b><br><span class="hint">${esc(c.name || "")}</span>`
          + (c.active ? "" : ` ${tag("quiet", "inactive")}`))
        + td("CMED's code", `<input class="small" data-codefor="${esc(c.hospital_id)}" value="${esc(c.cmed_hospital_id || "")}" `
          + `placeholder="${esc(c.hospital_id)} (our ID)" aria-label="CMED code for ${esc(c.hospital_id)}"> `
          + `<button type="button" class="chip" data-savecode="${esc(c.hospital_id)}">Save</button>`)
        + td("PCs", esc(String(c.pcs || 0)))
        + td("Last from CMED", esc(c.cmed_last_message_at ? dhaka(c.cmed_last_message_at) : "never"))
        // 26 Nov: the end rule, and whether CMED's API 4 reaches this clinic's PCs.
        + td("How recordings end", `<select class="small" data-endrule="${esc(c.hospital_id)}" `
          + `aria-label="How ${esc(c.hospital_id)} recordings end">`
          + `<option value="api3"${c.end_rule === "api4" ? "" : " selected"}>2 min after API 3</option>`
          + `<option value="api4"${c.end_rule === "api4" ? " selected" : ""}>CMED's API 4</option></select>`)
        + td("API 4 last seen", c.api4_last_at
          ? `${tag("good", esc(dhaka(c.api4_last_at)))}<br><span class="hint">${esc(String(c.api4_last_7_days || 0))} in the last 7 days</span>`
          : tag("quiet", "never"))
        + `</tr>`).join("") + `</tbody></table>`;
  const refused = data.refused || [];
  const options = CLINIC_CODES.map((c) =>
    `<option value="${esc(c.hospital_id)}">${esc(c.hospital_id)} ${esc(c.name || "")}</option>`).join("");
  $("refusedPcs").innerHTML = !refused.length
    ? empty("None", "No PC was refused in the last 30 days.", true)
    : `<table><thead><tr><th>Last tried</th><th>Code CMED sent</th><th>Why</th><th>PC</th>`
      + `<th>Fix</th></tr></thead><tbody>`
      + refused.map((r) => `<tr>`
        + td("Last tried", esc(dhaka(r.last_at)))
        + td("Code CMED sent", `<code>${esc(r.cmed_hospital_id || "")}</code>`)
        + td("Why", tag(r.code === "UNKNOWN_CLINIC" ? "bad" : "warn", r.code || ""))
        + td("PC", `${esc(r.machine_name || "")}<br><span class="hint">${esc(r.address || "")}`
          + `${r.app_version ? " · recorder " + esc(r.app_version) : ""}</span>`)
        + td("Fix", r.linked_now ? tag("good", "linked now: joins at the next patient")
          : r.code !== "UNKNOWN_CLINIC" ? `<span class="hint">see the reason</span>`
          : `<select class="small" data-linkcode="${esc(r.cmed_hospital_id || "")}" aria-label="Clinic for ${esc(r.cmed_hospital_id || "")}">`
            + `<option value="">Link to clinic...</option>${options}</select>`)
        + `</tr>`).join("") + `</tbody></table>`;
}

async function saveCode(hospitalId, code) {
  const clinic = CLINIC_CODES.find((c) => c.hospital_id === hospitalId);
  if (!clinic) return;
  if (!code) { window.alert("Type CMED's code first."); return; }
  if (clinic.cmed_hospital_id && clinic.cmed_hospital_id !== code
      && !window.confirm(`${hospitalId} answers to '${clinic.cmed_hospital_id}' now. Replace it with '${code}'? PCs sent the old code would no longer join.`)) return;
  try {
    await adminCall("POST", "/api/v2/admin/hospital", {
      hospital_id: hospitalId, name: clinic.name || hospitalId,
      timezone: clinic.timezone || "Asia/Dhaka", cmed_hospital_id: code });
  } catch (err) { window.alert(`Not saved: ${err.message}`); }
  loadCodes();
}

$("codes").addEventListener("change", async (e) => {
  const sel = e.target.closest("[data-endrule]");
  if (!sel) return;
  const clinic = sel.dataset.endrule;
  const rule = sel.value;
  const words = rule === "api4"
    ? `${clinic}: recordings will end when CMED's API 4 arrives (15 minutes after API 3 if it never comes). Switch only once CMED has API 4 live at this clinic. Continue?`
    : `${clinic}: recordings will end two minutes after CMED's API 3, as before. Continue?`;
  if (!window.confirm(words)) { loadCodes(); return; }
  try { await adminCall("POST", "/api/v2/admin/clinic-end-rule", { hospital_id: clinic, end_rule: rule }); }
  catch (err) { window.alert(`Not saved: ${err.message}`); }
  loadCodes();
});

$("codes").addEventListener("click", (e) => {
  const b = e.target.closest("[data-savecode]");
  if (!b) return;
  const input = document.querySelector(`[data-codefor="${CSS.escape(b.dataset.savecode)}"]`);
  saveCode(b.dataset.savecode, (input ? input.value : "").trim());
});
$("refusedPcs").addEventListener("change", (e) => {
  const sel = e.target.closest("[data-linkcode]");
  if (!sel || !sel.value) return;
  if (!window.confirm(`Link CMED's code '${sel.dataset.linkcode}' to clinic ${sel.value}?`)) { sel.value = ""; return; }
  saveCode(sel.value, sel.dataset.linkcode);
});

/* 042: what every PC was told - admins only (the server refuses anyone else). */
function deviceLogQuery() {
  return `days=${encodeURIComponent($("dlDays").value)}`
    + `&failed_only=${$("dlFailed").checked ? "true" : "false"}`
    + `&q=${encodeURIComponent($("dlSearch").value.trim())}`;
}
async function loadDeviceLog() {
  let rows;
  try { rows = (await adminCall("GET", `/api/v2/admin/device-log?${deviceLogQuery()}`)).rows || []; }
  catch (err) { $("deviceLog").innerHTML = empty("The device log could not be loaded", err.message, true); return; }
  $("deviceLog").innerHTML = !rows.length
    ? empty("Nothing in this period", "No PC tried to join, and no PC was refused anything.", true)
    : `<table><thead><tr><th>When</th><th>PC</th><th>Trying to</th><th>Result</th>`
      + `<th>What the server told it</th><th>What the PC sent</th></tr></thead><tbody>`
      + rows.map((r) => {
        const ok = r.status < 400;
        const when = r.repeats > 1
          ? `${esc(dhaka(r.last_at))}<br><span class="hint">${r.repeats} times since ${esc(dhaka(r.first_at))}</span>`
          : esc(dhaka(r.last_at));
        return `<tr class="${ok ? "" : "row-bad"}">`
          + td("When", when)
          + td("PC", `<b>${esc(r.machine_name || "unknown PC")}</b>`
            + `<br><span class="hint">${esc(r.address || "")}`
            + `${r.app_version ? " · recorder " + esc(r.app_version) : ""}`
            + `${r.device_id ? " · " + esc(String(r.device_id).slice(0, 8)) : ""}</span>`)
          + td("Trying to", esc(r.step || "")
            + (r.cmed_hospital_id ? `<br><span class="hint">CMED code <code>${esc(r.cmed_hospital_id)}</code></span>` : "")
            + (r.hospital_id ? `<br><span class="hint">clinic ${esc(r.hospital_id)}</span>` : ""))
          + td("Result", tag(ok ? "good" : (r.status >= 500 ? "bad" : "warn"),
                             `${r.status} ${r.code || ""}`))
          + td("What the server told it", esc(r.message || ""), "wrap")
          + td("What the PC sent", sentDetails(r.sent, r.repeats), "wrap")
          + `</tr>`;
      }).join("") + `</tbody></table>`;
}
/* 043: the headers and fields the PC sent (latest attempt); secrets and patient
   values were never kept, only that they came, their length and a fingerprint. */
function sentDetails(sent, repeats) {
  if (!sent || typeof sent !== "object") return `<span class="hint">not recorded</span>`;
  const lines = [];
  for (const [key, value] of Object.entries(sent)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [k, v] of Object.entries(value)) {
        if (v && typeof v === "object" && !Array.isArray(v)) {
          for (const [k2, v2] of Object.entries(v)) lines.push([`${key}.${k}.${k2}`, v2]);
        } else lines.push([`${key}.${k}`, v]);
      }
    } else lines.push([key, value]);
  }
  const show = (v) => esc(Array.isArray(v) ? v.join(", ") : String(v));
  return `<details class="sent"><summary>${lines.length} item${lines.length === 1 ? "" : "s"}`
    + `${repeats > 1 ? " (latest try)" : ""}</summary><dl>`
    + lines.map(([k, v]) => `<dt>${esc(k.replace(/^fields\./, ""))}</dt><dd>${show(v)}</dd>`).join("")
    + `</dl></details>`;
}
$("dlShow").addEventListener("click", loadDeviceLog);
$("dlFailed").addEventListener("change", loadDeviceLog);
$("dlDays").addEventListener("change", loadDeviceLog);
$("dlSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadDeviceLog(); });
$("dlCsv").addEventListener("click", () => {
  const link = document.createElement("a");
  link.href = `/api/v2/admin/device-log.csv?${deviceLogQuery()}`;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
});

/* 24 Nov 2026: a list's removed or revoked entries, behind one switch each. */
const SHOW_REMOVED = { origins: false, clients: false };
function removedSwitch(list, n) {
  if (!n) return "";
  return `<button type="button" class="chip" data-removed-switch="${list}">`
    + (SHOW_REMOVED[list] ? `Hide removed (${n})` : `Show removed (${n})`) + `</button>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-removed-switch]");
  if (!b) return;
  SHOW_REMOVED[b.dataset.removedSwitch] = !SHOW_REMOVED[b.dataset.removedSwitch];
  loadCmed();
});

async function loadCmed() {
  await loadCodes();
  loadDeviceLog();
  try {
    const { origins: everyOrigin } = await adminCall("GET", "/api/v2/admin/trusted-origins");
    // 24 Nov 2026: removed addresses are kept (history, audit) but hidden
    // unless asked for, so the list shows what recorders obey now.
    const goneOrigins = everyOrigin.filter((o) => o.removed_at).length;
    const origins = SHOW_REMOVED.origins ? everyOrigin : everyOrigin.filter((o) => !o.removed_at);
    $("originsRemoved").innerHTML = removedSwitch("origins", goneOrigins);
    $("origins").innerHTML = !origins.length
      ? empty("No address trusted yet", "Add CMED's EHR address: until then no recorder obeys any page.", true)
      : `<table><thead><tr><th>Address</th><th>Note</th><th>Added</th><th>State</th><th></th></tr></thead><tbody>`
        + origins.map((o) => `<tr class="${o.removed_at ? "" : ""}">`
          + td("Address", `<code>${esc(o.origin)}</code>`)
          + td("Note", esc(o.note || ""))
          + td("Added", `${esc(dhaka(o.added_at))}<br><span class="hint">by ${esc(o.added_by)}</span>`)
          + td("State", o.removed_at ? tag("quiet", "Removed", `${o.removed_by} · ${dhaka(o.removed_at)}`)
                                     : tag("good", "Trusted"))
          + td("", o.removed_at ? "" : `<button type="button" class="chip" data-unorigin="${esc(o.origin)}">Remove</button>`)
          + `</tr>`).join("") + `</tbody></table>`;
  } catch (err) {
    $("origins").innerHTML = empty("Addresses could not be loaded", err.message, true);
  }
  try {
    const { clients: everyClient } = await adminCall("GET", "/api/v2/admin/cmed-clients");
    const goneClients = everyClient.filter((c) => c.revoked_at).length;
    const clients = SHOW_REMOVED.clients ? everyClient : everyClient.filter((c) => !c.revoked_at);
    $("clientsRemoved").innerHTML = removedSwitch("clients", goneClients);
    $("clients").innerHTML = !clients.length
      ? empty("No hospital connected", "Register CMED's public key for a hospital.", true)
      : `<table><thead><tr><th>Client id</th><th>Hospital</th><th>Key (kid)</th><th>Last used</th>`
        + `<th>Valid until</th><th>State</th><th></th></tr></thead><tbody>`
        + clients.map((c) => {
          const live = !c.revoked_at;
          return `<tr class="${live ? "" : ""}">`
            + td("Client id", `<b><code>${esc(c.client_id)}</code></b><br><span class="hint">${esc(c.label || "")}</span>`)
            + td("Hospital", (c.cmed_hospital_ids || []).map((code) => {
              const clinic = CLINIC_CODES.find((k) => k.cmed_hospital_id === code || k.hospital_id === code);
              return `<code>${esc(code)}</code> `
                + (clinic ? tag("good", "clinic " + clinic.hospital_id)
                          : tag("bad", "no clinic: PCs cannot join"));
            }).join("<br>"))
            + td("Key", (c.kids || []).map((k) => `<code title="Confirm with CMED by phone">${esc(k)}</code>`).join("<br>") || `<span class="hint">none</span>`)
            + td("Last used", esc(dhaka(c.last_used_at)))
            + td("Valid until", esc(dhaka(c.expires_at)))
            + td("State", live ? tag("good", "Active") : tag("quiet", "Revoked"))
            + td("", live ? `<button type="button" class="chip" data-renew="${esc(c.client_id)}">Renew a year</button> `
              + `<button type="button" class="chip" data-unclient="${esc(c.client_id)}">Revoke</button>` : "")
            + `</tr>`;
        }).join("") + `</tbody></table>`;
  } catch (err) {
    $("clients").innerHTML = empty("Connections could not be loaded", err.message, true);
  }
}

function adminName() {
  return (PORTAL && PORTAL.email) || (window.prompt("Your name, for the audit log:") || "").trim();
}

$("originForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const origin = $("oAddress").value.trim();
  const by = adminName();
  if (!origin || by.length < 2) return;
  try {
    const r = await adminCall("POST", "/api/v2/admin/trusted-origins", { origin, by, note: $("oNote").value.trim() });
    $("originMsg").textContent = `${r.origin} is trusted. Every recorder takes it within a minute.`;
    $("oAddress").value = ""; $("oNote").value = "";
    loadCmed();
  } catch (err) { $("originMsg").textContent = `Not done: ${err.message}`; }
});

$("origins").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-unorigin]");
  if (!b) return;
  if (!window.confirm(`Stop trusting ${b.dataset.unorigin}? Recorders refuse that page within a minute.`)) return;
  const by = adminName();
  if (by.length < 2) return;
  try { await adminCall("POST", "/api/v2/admin/trusted-origins/remove", { origin: b.dataset.unorigin, by }); }
  catch (err) { window.alert(`Not done: ${err.message}`); }
  loadCmed();
});

$("cHospital").addEventListener("input", () => {
  // The rule CMED is told in advance: client id = the hospital code, lower case.
  $("cClient").value = $("cHospital").value.trim().toLowerCase();
});

/* 16 Nov: a CMED connection lets CMED's server send API 2 and 3 for a code;
   only a clinic linked to the code lets a PC join. Both are done here, the
   clinic first - Nabila's PC was refused because only the first was done. */
$("cClinic").addEventListener("change", () => {
  const isNew = $("cClinic").value === "__new";
  $("cNewWrap").hidden = !isNew;
  $("cNewNameWrap").hidden = !isNew;
});

async function linkCodeToClinic(code, clinicId, newName) {
  const clinic = CLINIC_CODES.find((c) => c.hospital_id === clinicId);
  if (clinic && clinic.cmed_hospital_id && clinic.cmed_hospital_id !== code
      && !window.confirm(`${clinicId} answers to '${clinic.cmed_hospital_id}' now. Replace it with '${code}'? `
        + "PCs sent the old code would no longer join.")) {
    throw new Error("not linked: the clinic keeps its code");
  }
  await adminCall("POST", "/api/v2/admin/hospital", {
    hospital_id: clinicId, name: clinic ? (clinic.name || clinicId) : newName,
    timezone: clinic ? (clinic.timezone || "Asia/Dhaka") : "Asia/Dhaka", cmed_hospital_id: code });
}

$("clinicForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("ncId").value.trim();
  const name = $("ncName").value.trim();
  const code = $("ncCode").value.trim();
  if (CLINIC_CODES.some((c) => c.hospital_id === id)) {
    $("clinicMsg").textContent = `${id} already exists: give it a code in the table below.`;
    return;
  }
  try {
    await adminCall("POST", "/api/v2/admin/hospital", {
      hospital_id: id, name, timezone: "Asia/Dhaka", cmed_hospital_id: code });
    $("clinicMsg").textContent = code
      ? `Added ${id}, answering to '${code}': a PC CMED starts with that code joins at its next patient.`
      : `Added ${id}. Give it CMED's code in the table below before its PCs can join.`;
    $("ncId").value = ""; $("ncName").value = ""; $("ncCode").value = "";
    loadCmed();
  } catch (err) { $("clinicMsg").textContent = `Not added: ${err.message}`; }
});

$("clientForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  let jwk;
  try { jwk = JSON.parse($("cJwk").value); }
  catch { $("clientMsg").textContent = "The public key is not valid JSON: paste the whole .public.json."; return; }
  if (jwk.d) { $("clientMsg").textContent = "That is a PRIVATE key. Ask CMED for the public one; tell them to keep this secret."; return; }
  const by = adminName();
  if (by.length < 2) return;
  const hospital = $("cHospital").value.trim();
  const alg = jwk.crv === "P-256" ? "ES256" : jwk.kty === "RSA" ? "RS384" : "ES384";
  let clinicId = $("cClinic").value;
  const isNew = clinicId === "__new";
  if (isNew) clinicId = $("cNewId").value.trim();
  if (!clinicId || (isNew && !$("cNewName").value.trim())) {
    $("clientMsg").textContent = "Choose our clinic for this code (or give the new clinic's ID and name): "
      + "without it, PCs CMED starts with this code cannot join.";
    return;
  }
  if (isNew && CLINIC_CODES.some((c) => c.hospital_id === clinicId)) {
    $("clientMsg").textContent = `${clinicId} already exists: choose it in the list instead.`;
    return;
  }
  try {
    await linkCodeToClinic(hospital, clinicId, $("cNewName").value.trim());
  } catch (err) { $("clientMsg").textContent = `Not registered: ${err.message}`; return; }
  try {
    const r = await adminCall("POST", "/api/v2/admin/cmed-client", {
      client_id: $("cClient").value.trim(), label: `CMED - ${hospital}`,
      cmed_hospital_ids: [hospital], created_by: by, jwk, alg });
    $("clientMsg").innerHTML = `Registered, and '${esc(hospital)}' linked to clinic ${esc(clinicId)}. `
      + `Send CMED: client_id <code>${esc(r.client_id)}</code>, `
      + `kid <code>${esc(r.kid)}</code>. Before they use it, read the kid to them by phone: `
      + `their key tool printed the same value.`;
    $("cJwk").value = "";
    loadCmed();
  } catch (err) { $("clientMsg").textContent = `Not registered: ${err.message}`; }
});

$("clients").addEventListener("click", async (e) => {
  const renew = e.target.closest("[data-renew]");
  const revoke = e.target.closest("[data-unclient]");
  if (!renew && !revoke) return;
  const by = adminName();
  if (by.length < 2) return;
  try {
    if (renew) {
      const r = await adminCall("POST", `/api/v2/admin/cmed-client/${encodeURIComponent(renew.dataset.renew)}/renew`,
        { days: 365, created_by: by });
      window.alert(`Valid until ${dhaka(r.expires_at)}.`);
    } else {
      if (!window.confirm(`Revoke ${revoke.dataset.unclient}? Its key and every live token stop at once: `
        + "CMED's messages for that hospital are refused until a new key is registered.")) return;
      await adminCall("POST", `/api/v2/admin/cmed-client/${encodeURIComponent(revoke.dataset.unclient)}/revoke`);
    }
  } catch (err) { window.alert(`Not done: ${err.message}`); }
  loadCmed();
});

let HOSPITAL_LIST = [];   // 038: for "Looks after"
async function loadPeople() {
  try {
    const { people, mail, mail_status } = await adminCall("GET", "/api/v2/admin/people");
    if (!HOSPITAL_LIST.length) {
      try { HOSPITAL_LIST = (await adminCall("GET", "/api/v2/admin/places")).hospitals || []; }
      catch (err) { HOSPITAL_LIST = []; }
    }
    const watchCell = (p) => {
      const mine = p.watching || [];
      if (p.role === "collector") {
        // 18 Nov: assigned as a researcher, then made a collector - the
        // researcher alerts skip them, so they hear nothing for these hospitals.
        if (!mine.length) return `<span class="hint">from their place</span>`;
        return tag("bad", "Gets no researcher alerts",
            "A data collector is sent only their place's alerts. Make them a researcher, or take these hospitals off.")
          + " " + mine.map((h) => `<button type="button" class="chip" title="Take ${esc(h)} off"`
          + ` data-unwatch="${esc(p.email)}" data-hospital="${esc(h)}">${esc(h)} ×</button>`).join(" ");
      }
      return mine.map((h) => `<button type="button" class="chip" title="Stop looking after ${esc(h)}" `
          + `data-unwatch="${esc(p.email)}" data-hospital="${esc(h)}">${esc(h)} \u00d7</button>`).join(" ")
        + ` <select class="small" data-watchsel="${esc(p.email)}" aria-label="Give ${esc(p.email)} a hospital">`
        + `<option value="">+ hospital</option>`
        + HOSPITAL_LIST.filter((h) => !mine.includes(h.hospital_id)).map((h) =>
          `<option value="${esc(h.hospital_id)}">${esc(h.hospital_id)} ${esc(h.name || "")}</option>`).join("")
        + `</select>`;
    };
    $("peopleHint").textContent = mail_status && mail_status.ready
      ? "An invitation email is sent when you give access"
      : "Invitation emails are not working yet, so send them the link yourself. "
        + ((mail_status && mail_status.problem) || "");
    const when = (iso) => (iso ? new Date(iso).toLocaleString([], { timeZone: "Asia/Dhaka" }) : "never");
    $("people").innerHTML = `<table><thead><tr><th>Google account</th><th>Role</th>`
      + `<th>Looks after</th><th>Given by</th><th>Last signed in</th><th>Database login</th><th>Telegram</th><th></th></tr></thead><tbody>`
      + people.map((p) => `<tr>`
        + td("Google account", `<b>${esc(p.email)}</b>${p.display_name ? `<br><span class="hint">${esc(p.display_name)}</span>` : ""}`
          + (p.role === "collector"
            ? `<br>${p.official_name ? esc(p.official_name) : tag("warn", "no official name")} `
              + `${p.collector_code ? `<code>${esc(p.collector_code)}</code>` : tag("warn", "no ID")} `
              + `<button type="button" class="chip" data-detail="official_name" data-who="${esc(p.email)}" `
              + `data-current="${esc(p.official_name || "")}">Name</button> `
              + `<button type="button" class="chip" data-detail="collector_code" data-who="${esc(p.email)}" `
              + `data-current="${esc(p.collector_code || "")}">ID</button>`
            : ""))
        + td("Role", p.role === "admin" ? tag("warn", "Admin")
          : p.role === "cmed" ? tag("info", "CMED") + `<br><span class="hint">JSON only, no audio</span>`
          : p.role === "collector" ? tag("info", "Data collector")
            + (p.place ? `<br><span class="hint">${esc(p.place)}</span>` : "")
          : tag("good", "Researcher"))
        + td("Looks after", watchCell(p))
        + td("Given by", esc(p.added_by || ""))
        + td("Last signed in", esc(when(p.last_signed_in)))
        + td("Database login", p.role === "collector" || p.role === "cmed"
          ? `<span class="hint">none</span>`
          : (p.db_user ? `<code>${esc(p.db_user)}</code><br>` : "")
          + `<button type="button" class="chip" data-dblogin="${esc(p.email)}">`
          + `${p.db_user ? "New password" : "Create login"}</button>`)
        + td("Telegram", (p.telegram ? tag("good", "connected")
          : tag(p.role === "collector" || (p.watching || []).length ? "warn" : "quiet", "not connected"))
          + (p.role === "collector"
            ? `<br>${p.phone ? esc(p.phone) : tag("warn", "no phone")} `
              + `<button type="button" class="chip" data-phone="${esc(p.email)}" `
              + `data-current="${esc(p.phone || "")}">Phone</button>`
            : ""))
        + td("", p.owner ? `<span class="hint">owner</span>`
          : `<button type="button" class="chip" data-remove="${esc(p.email)}">Remove</button>`)
        + `</tr>`).join("") + `</tbody></table>`;
  } catch (err) {
    $("people").innerHTML = empty("People could not be loaded", err.message, true);
  }
}

$("peopleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("pEmail").value.trim();
  if (!email) return;
  try {
    const role = $("pRole").value;
    const body = { email, role };
    if (role === "collector") {
      if (!$("pPlace").value) { $("peopleMsg").textContent = "Pick the collector's place first (add places below)."; return; }
      body.place_id = Number($("pPlace").value);
    }
    const r = await adminCall("POST", "/api/v2/admin/people", body);
    $("peopleMsg").textContent = r.invited ? `Invitation sent to ${r.email}.`
      : `${r.email} can now sign in at ${r.link} - send them this link yourself; `
        + `no invitation email was sent (${r.invite_error || "mail is not set up"}).`;
    if (r.db_login) showDbLogin(r.email, r.db_login);
    else if (r.db_login_error) $("peopleMsg").textContent += ` (No database login: ${r.db_login_error})`;
    $("pEmail").value = "";
    loadPeople();
  } catch (err) { $("peopleMsg").textContent = `Not done: ${err.message}`; }
});

/* The password is shown here once and never again: the server keeps no copy. */
function showDbLogin(email, login) {
  $("peopleMsg").innerHTML = `Database login for <b>${esc(email)}</b>: user `
    + `<code>${esc(login.db_user)}</code>, password <code>${esc(login.password)}</code>. `
    + `Send it to them privately now; it is not shown again. They open the database `
    + `viewer (/db/) signed in with Google, then use this login.`;
}

$("people").addEventListener("change", async (e) => {
  const sel = e.target.closest("[data-watchsel]");
  if (!sel || !sel.value) return;
  try {
    await adminCall("POST", "/api/v2/admin/people/watch",
                    { email: sel.dataset.watchsel, hospital_id: sel.value });
  } catch (err) { window.alert(`Not done: ${err.message}`); }
  loadPeople();
});
$("people").addEventListener("click", async (e) => {
  const w = e.target.closest("[data-unwatch]");
  if (w) {
    if (!window.confirm(`${w.dataset.unwatch} stops looking after ${w.dataset.hospital}?`)) return;
    try {
      await adminCall("POST", "/api/v2/admin/people/watch",
                      { email: w.dataset.unwatch, hospital_id: w.dataset.hospital, remove: true });
    } catch (err) { window.alert(`Not done: ${err.message}`); }
    loadPeople();
    return;
  }
  const dt = e.target.closest("[data-detail]");
  if (dt) {
    const what = dt.dataset.detail === "official_name" ? "Official name" : "ID (like DC-07)";
    const value = window.prompt(`${what} for ${dt.dataset.who} (empty to clear):`,
                                dt.dataset.current || "");
    if (value === null) return;
    try {
      await adminCall("POST", "/api/v2/admin/people/details",
                      { email: dt.dataset.who, [dt.dataset.detail]: value });
    } catch (err) { window.alert(`Not saved: ${err.message}`); }
    loadPeople();
    return;
  }
  const ph = e.target.closest("[data-phone]");
  if (ph) {
    const phone = window.prompt(`Phone number for ${ph.dataset.phone} (empty to clear):`,
                                ph.dataset.current || "");
    if (phone === null) return;
    try { await adminCall("POST", "/api/v2/admin/people/phone", { email: ph.dataset.phone, phone }); }
    catch (err) { window.alert(`Not saved: ${err.message}`); }
    loadPeople();
    return;
  }
  const k = e.target.closest("[data-dblogin]");
  if (k) {
    const who = k.dataset.dblogin;
    if (!window.confirm(`Set a new database password for ${who}? Any old one stops working.`)) return;
    try { showDbLogin(who, await adminCall("POST", "/api/v2/admin/people/db-login", { email: who })); }
    catch (err) { window.alert(`Not done: ${err.message}`); }
    loadPeople();
    return;
  }
  const b = e.target.closest("[data-remove]");
  if (!b) return;
  if (!window.confirm(`Remove ${b.dataset.remove}? Their access ends now, on every device.`)) return;
  try { await adminCall("POST", "/api/v2/admin/people/remove", { email: b.dataset.remove }); }
  catch (err) { window.alert(`Not done: ${err.message}`); }
  loadPeople();
});

/* ============================================================
   Download in batches (bulk.py): a selection, and the next files of
   it this person does not have yet, one ZIP at a time.
   ============================================================ */
let BULK = null;            // last /bulk/preview answer
let BULK_DOCTORS = null;    // null: every doctor; else the chosen ids
let bulkSeq = 0;
let bulkTimer = 0;

const size = (b) => {
  const n = Number(b || 0);
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
};
const shortDay = (iso) => {
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString([], { day: "numeric", month: "short" });
};

async function bulkCall(method, path, params, body) {
  const query = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (Array.isArray(v)) v.forEach((x) => query.append(k, x));
    else if (v !== "" && v != null) query.append(k, v);
  });
  const response = await fetch(`/api/v2/dashboard/bulk${path}${method === "GET" ? `?${query}` : ""}`, {
    method, headers: { ...portalHeaders(), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined });
  if (response.status === 401) { window.location.href = "/auth/login?next=/dashboard"; throw new Error("Signed out."); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);
  return data;
}

function bulkSelection() {
  return { hospital_id: $("bClinic").value, from: $("bFrom").value, to: $("bTo").value,
           doctor: BULK_DOCTORS || [], size: $("bSize").value || 200 };
}

function bulkSoon(ms) { clearTimeout(bulkTimer); bulkTimer = setTimeout(loadBulk, ms || 250); }

async function loadBulk() {
  if (!PORTAL) return;
  const seq = ++bulkSeq;
  try {
    const d = await bulkCall("GET", "/preview", bulkSelection());
    if (seq !== bulkSeq) return;
    BULK = d;
    drawBulk(d);
  } catch (err) {
    if (seq !== bulkSeq) return;
    $("bSummary").innerHTML = empty("Could not load this selection", err.message);
    $("bGo").disabled = true;
  }
  loadBatches();
}

function drawBulk(d) {
  const lim = d.limits;
  // The batch sizes this person may ask for, keeping their choice.
  const chosen = Number($("bSize").value) || Math.min(200, lim.batch_max);
  $("bSize").innerHTML = lim.sizes.map((s) =>
    `<option value="${s}"${s === chosen ? " selected" : ""}>${num(s)}</option>`).join("");
  if (!lim.sizes.includes(chosen)) $("bSize").value = String(lim.sizes[lim.sizes.length - 1]);

  // Doctors, with what each has and what this person already took.
  const all = BULK_DOCTORS === null;
  $("bAll").setAttribute("aria-pressed", String(all));
  $("bDoctors").innerHTML = d.doctors.length ? d.doctors.map((x) => {
    const on = all || BULK_DOCTORS.includes(x.doctor_id);
    return `<button type="button" class="chip" data-doc="${esc(x.doctor_id)}" aria-pressed="${on}"`
      + ` title="${esc(`${x.downloaded} of ${x.files} already downloaded by you`)}">`
      + `${esc(x.doctor_id)} · <b>${num(x.files)}</b>`
      + (x.downloaded ? ` <span class="hint">(${num(x.downloaded)} yours)</span>` : "") + `</button>`;
  }).join("") : `<span class="hint">No downloadable recordings for these days.</span>`;

  const pct = d.total ? Math.round((d.downloaded / d.total) * 100) : 0;
  $("bSummary").innerHTML = d.total ? `<div class="bnums">`
      + `<div><b>${num(d.total)}</b><small>files in this selection · ${size(d.bytes_total)}</small></div>`
      + `<div class="good"><b>${num(d.downloaded)}</b><small>already downloaded by you</small></div>`
      + `<div class="info"><b>${num(d.remaining)}</b><small>left · about ${size(d.bytes_remaining)}</small></div>`
      + `</div><div class="meter" title="${pct}% downloaded"><i data-pct="${pct}"></i></div>`
      + `<div class="bdays">${d.days.map((y) => `<span>${esc(shortDay(y.date))}: <b>${num(y.files)}</b>`
        + (y.downloaded ? ` <span class="hint">(${num(y.downloaded)} yours)</span>` : "") + `</span>`).join("")}</div>`
    : empty("Nothing to download here", "Only recordings CMED confirmed and copied to the cloud can be downloaded.");
  // Set from script: the page policy allows no inline style.
  $("bSummary").querySelectorAll(".meter i").forEach((bar) => { bar.style.width = `${bar.dataset.pct}%`; });

  // The button says exactly which files come next, or why none can.
  const go = $("bGo");
  const w = lim.window || { open: true };
  let reason = "";
  if (d.running) reason = `Batch ${d.running.batch_no} is downloading: ${num(d.running.files_done)} of ${num(d.running.files)} files so far.`;
  else if (!w.open) reason = `Batch downloads for researchers open at ${w.hours.split("-")[0]} (in ${Math.floor(w.opens_in_minutes / 60)} h ${w.opens_in_minutes % 60} min) and close at ${w.hours.split("-")[1]}, clinic time.`;
  else if (!d.total) reason = "";
  else if (!d.remaining) reason = `You have all ${num(d.total)} files of this selection.`;
  else if (lim.left_today === 0) reason = `You have taken today's ${num(lim.daily_files)} files.`
    + (lim.frees_at ? ` More from ${when(lim.frees_at)}.` : "");
  go.disabled = !!reason || !d.next;
  go.textContent = d.next
    ? `Download batch ${d.next.batch_no} · files ${num(d.next.first_no)}–${num(d.next.last_no)} of ${num(d.total)} · about ${size(d.next.bytes)}`
    : "Download next batch";
  $("bMsg").textContent = reason;
  $("bulkHint").textContent = lim.daily_files != null
    ? `Today: ${num(lim.used_today)} of ${num(lim.daily_files)} files` + (lim.window && lim.window.hours ? ` · open ${lim.window.hours}` : "")
    : "Admin: no daily limit, any time";
}

$("bDoctors").addEventListener("click", (e) => {
  const b = e.target.closest("[data-doc]");
  if (!b || !BULK) return;
  const ids = BULK.doctors.map((x) => x.doctor_id);
  let picked = BULK_DOCTORS === null ? ids.slice() : BULK_DOCTORS.slice();
  picked = picked.includes(b.dataset.doc) ? picked.filter((x) => x !== b.dataset.doc) : picked.concat(b.dataset.doc);
  BULK_DOCTORS = picked.length === ids.length || !picked.length ? null : picked;
  bulkSoon(0);
});
$("bAll").addEventListener("click", () => { BULK_DOCTORS = null; bulkSoon(0); });
["bClinic", "bFrom", "bTo"].forEach((id) => $(id).addEventListener("change", () => {
  BULK_DOCTORS = null;           // another selection: its own doctors
  bulkSoon(0);
}));
$("bSize").addEventListener("change", () => bulkSoon(0));

$("bGo").addEventListener("click", async () => {
  const go = $("bGo");
  go.disabled = true;
  $("bMsg").textContent = "Preparing the batch…";
  try {
    const s = bulkSelection();
    const batch = await bulkCall("POST", "/batches", null, {
      hospital_id: s.hospital_id || null, from: s.from, to: s.to,
      doctors: BULK_DOCTORS, size: Number(s.size) });
    startZip(batch);
  } catch (err) {
    $("bMsg").textContent = `Not started: ${err.message}`;
    bulkSoon(1500);
  }
});

/* The browser saves the ZIP itself; this page only watches the batch. Asked
   first whether it may start, so a refusal is said here, not on a bare page. */
async function startZip(batch) {
  try {
    const ok = await bulkCall("GET", `/batches/${encodeURIComponent(batch.id)}/check`, {});
    if (!ok.ok) { $("bMsg").textContent = `Not started: ${ok.detail}`; bulkSoon(1500); return; }
  } catch (err) { $("bMsg").textContent = `Not started: ${err.message}`; return; }
  const a = document.createElement("a");
  a.href = `/api/v2/dashboard/bulk/batches/${encodeURIComponent(batch.id)}/zip`;
  a.download = "";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  $("bMsg").textContent = (batch.batch_no
    ? `Batch ${batch.batch_no} (files ${num(batch.first_no)}–${num(batch.last_no)}) is downloading. `
    : "The batch is downloading again. ")
    + "Your browser shows its progress; the next batch can start when it finishes.";
  setTimeout(loadBulk, 2000);
}

async function loadBatches() {
  try {
    const { batches } = await bulkCall("GET", "/batches", { everyone: $("bEveryone").checked ? 1 : "" });
    drawBatches(batches || []);
    clearTimeout(window._bpoll);
    if (batches.some((b) => b.outcome === "started")) window._bpoll = setTimeout(loadBulk, 5000);
  } catch (err) {
    $("bBatches").innerHTML = empty("Could not load batches", err.message);
  }
}

function batchStatus(b) {
  if (b.outcome === "started") return tag("info", `Downloading ${num(b.files_done)}/${num(b.files)}`);
  if (b.outcome === "completed") {
    return tag("good", "Done", `${b.files_done} files arrived whole`)
      + (b.files_failed ? " " + tag("warn", `${b.files_failed} failed`, "These come again in your next batch.") : "");
  }
  if (b.outcome === "failed") return tag("bad", "Stopped", b.reason || "");
  return tag("quiet", "Not started");
}

function drawBatches(list) {
  if (!list.length) {
    $("bBatches").innerHTML = empty("No batches yet", "Choose a selection above and download its first batch.", true);
    return;
  }
  const everyone = $("bEveryone").checked;
  $("bBatches").innerHTML = `<table><thead><tr><th>Batch</th>${everyone ? "<th>Who</th>" : ""}`
    + `<th>Selection</th><th class="num">Files</th><th class="num">Size</th><th>Started</th>`
    + `<th>Status</th><th></th></tr></thead><tbody>`
    + list.map((b) => {
      const docs = b.doctors ? (b.doctors.length > 3 ? `${b.doctors.length} doctors` : b.doctors.join(", ")) : "all doctors";
      const days = b.from === b.to ? shortDay(b.from) : `${shortDay(b.from)} – ${shortDay(b.to)}`;
      const mine = !PORTAL || b.email === PORTAL.email;
      const again = !mine || b.outcome === "started" ? ""
        : `<button type="button" class="chip" data-again="${b.id}">${b.outcome === "completed" ? "Download again" : "Try again"}</button>`;
      return `<tr class="${b.outcome === "failed" ? "row-bad" : ""}">`
        + td("Batch", `<b>${num(b.batch_no)}</b><span class="sub2">files ${num(b.first_no)}–${num(b.last_no)} of ${num(b.selection_total)}</span>`)
        + (everyone ? td("Who", esc(b.email)) : "")
        + td("Selection", `${esc(clinicName(b.hospital_id) || "All clinics")}<span class="sub2">${esc(days)} · ${esc(docs)}</span>`, "wrap")
        + td("Files", num(b.files), "num")
        + td("Size", size(b.bytes_sent || b.bytes_estimate), "num")
        + td("Started", esc(when(b.started_at || b.created_at)))
        + td("Status", `<span class="tags">${batchStatus(b)}</span>`)
        + td("", again)
        + `</tr>`;
    }).join("") + `</tbody></table>`;
}

$("bBatches").addEventListener("click", (e) => {
  const b = e.target.closest("[data-again]");
  if (!b) return;
  b.disabled = true;
  startZip({ id: b.dataset.again }).finally(() => { b.disabled = false; });
});
$("bEveryone").addEventListener("change", loadBatches);

/* ============================================================
   One patient's history (18 Nov 2026): type the ID, see every visit.
   ============================================================ */
/* 19 Nov: every field is shown, filled or not, so a message CMED sent empty
   reads as empty instead of looking like data the page failed to show. */
const EMPTY = `<span class="hint">–</span>`;
function pText(label, value) {
  return value ? `<div class="prow"><span class="pl">${esc(label)}</span><span class="pv">${esc(value)}</span></div>` : "";
}
function pField(label, value) {
  const shown = value === null || value === undefined || value === "" ? EMPTY : esc(value);
  return `<div class="pf"><span class="pl">${esc(label)}</span><span class="pv">${shown}</span></div>`;
}
function pRow(label, value) {
  // A list joined on the server can start or end with an empty item ("; BP ...").
  const text = value ? String(value).replace(/^(\s*;\s*)+|(\s*;\s*)+$/g, "") : "";
  return `<div class="prow"><span class="pl">${esc(label)}</span><span class="pv">${text ? esc(text) : EMPTY}</span></div>`;
}
function pArrived(iso, ok) {
  if (!ok) return tag("warn", "not received");
  if (!iso) return tag("good", "received");
  return tag("good", `received ${String(iso).replace("T", " ").slice(0, 16)}`);
}
// What the page says when a message has not come, or came carrying nothing.
function pNote(received, filled, what) {
  if (!received) return `<p class="pnote hint">CMED has not sent the ${what} for this visit.</p>`;
  if (!filled) return `<p class="pnote empty">CMED sent the ${what} for this visit, but the message had nothing in it.</p>`;
  return "";
}
function demographicsBlock(v) {
  const d = v.demographics || {};
  const fields = [["Sex", d.sex], ["Date of birth", d.date_of_birth], ["Age", d.age],
    ["Blood group", d.blood_group], ["Blood pressure", d.blood_pressure], ["Pulse", d.pulse],
    ["Temperature", d.temperature], ["Weight", d.weight], ["Height", d.height], ["SpO2", d.spo2]];
  const filled = fields.some(([, x]) => x);
  return `<div class="psec"><div class="psh"><h4>Demographics and vitals (API 2)</h4> ${pArrived(v.api2_received_at, v.api2_received)}</div>
    ${pNote(v.api2_received, filled, "demographics")}
    <div class="pgrid">${fields.map(([n, x]) => pField(n, x)).join("")}</div></div>`;
}
function prescriptionBlock(v) {
  const p = v.prescription || {};
  const meds = v.medicines || [];
  const medTable = meds.length
    ? `<div class="pscroll"><table class="pmeds"><thead><tr><th>Medicine</th><th>Strength</th><th>Dose</th><th>How often</th><th>When</th><th>For</th></tr></thead><tbody>`
      + meds.map((m) => `<tr><td>${esc([m.form, m.medicine].filter(Boolean).join(" "))}</td>`
        + `<td>${esc(m.strength || "")}</td><td>${esc(m.dose || "")}</td><td>${esc(m.frequency || "")}</td>`
        + `<td>${esc(m.timing || "")}</td><td>${esc(m.duration || "")}</td></tr>`).join("")
      + `</tbody></table></div>`
    : (p.medicines ? esc(p.medicines) : EMPTY);
  const before = [["Complaints", p.chief_complaints], ["Examination", p.examination],
    ["Diagnoses", p.diagnoses]];
  const after = [["Tests", p.investigations], ["Advice", p.advice], ["Follow-up", p.follow_up]];
  const filled = meds.length > 0 || !!p.medicines || [...before, ...after].some(([, x]) => x);
  return `<div class="psec"><div class="psh"><h4>Prescription (API 3)</h4> ${pArrived(v.api3_received_at, v.api3_received)}</div>
    ${pNote(v.api3_received, filled, "prescription")}
    <div class="pbody">
      ${before.map(([n, x]) => pRow(n, x)).join("")}
      <div class="prow"><span class="pl">Medicines</span><span class="pv">${medTable}</span></div>
      ${after.map(([n, x]) => pRow(n, x)).join("")}
      ${pText("Issued", p.issued_at)}
    </div></div>`;
}
function visitCard(v) {
  const recs = (v.recordings_detail || []).map((r) => {
    const base = `/api/v2/dashboard/recordings/${encodeURIComponent(r.session_id)}`;
    const record = ` <a class="copy dl" href="${esc(base)}/record">Record (JSON)</a>`;
    // 049: CMED staff the record only; a researcher their own hospitals only.
    const links = !(r.copied && r.confirmation === "confirmed" && PORTAL) ? ""
      : PORTAL.role === "cmed" ? record
      : PORTAL.role === "researcher" && !(PORTAL.download_hospitals || []).includes(r.hospital_id)
        ? ` <span class="hint">Not your hospital</span>`
      : ` <a class="copy dl" href="${esc(base)}/audio">Audio (WAV)</a>` + record;
    return `<div class="prec">${confirmTag(r.confirmation)} <span class="mono">${esc(r.session_id)}</span>`
      + ` <span class="hint">${esc(String(r.local_start || "").slice(0, 5))} · ${num(r.minutes || 0)} min</span>${links}</div>`;
  }).join("");
  const visitDl = v.visit_id && !(PORTAL && PORTAL.role === "cmed")       // 049: no CSV
    ? `<button type="button" class="ghost small" data-pcsv="visits" data-visit="${esc(v.visit_id)}">This visit (CSV)</button>`
    : "";
  return `<section class="panel pvisit">
    <div class="head wrap"><h3>${esc(v.visit_date || "")} <span class="hint">${esc(v.start_time || "")}</span></h3>
      <span class="tags">${tag("quiet", v.visit_id || "no visit ID")} ${tag("quiet", v.doctor_id || "")}
        ${tag("quiet", v.clinic ? clinicName(v.clinic) : v.cmed_clinic_code || "")}</span>
      ${visitDl}</div>
    <div class="body">
      ${demographicsBlock(v)}
      ${prescriptionBlock(v)}
      <div class="psec pbody">
        <div class="prow"><span class="pl">Recordings</span><span class="pv">${recs || `<span class="hint">none</span>`}</span></div>
        ${pText("Archive folder", v.archive_folder)}
      </div>
    </div>
  </section>`;
}

// One CSV download, fetched so the admin key goes with it and a refusal is
// said on the page instead of replacing it.
async function downloadCsv(params, button, msg, path = "/api/v2/dashboard/clinical.csv") {
  const query = new URLSearchParams(Object.entries(params)
    .filter(([, v]) => v !== "" && v != null));
  msg.textContent = "Preparing...";
  button.disabled = true;
  try {
    const response = await fetch(`${path}?${query}`,
      { headers: portalHeaders() });
    if (!response.ok) {
      let detail = `Server error ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const named = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = named ? named[1] : `${params.table}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    msg.textContent = `Downloaded ${link.download}.`;
  } catch (err) {
    msg.textContent = `Not downloaded: ${err.message}`;
  } finally {
    button.disabled = false;
  }
}

// The patient (and visit) on screen, for its download buttons.
let SHOWN_PATIENT = null;
async function loadPatient(ref, visit) {
  $("patientMsg").textContent = "Looking up…";
  $("patientOut").innerHTML = "";
  SHOWN_PATIENT = null;
  try {
    const d = await api(`/patient/${encodeURIComponent(ref)}`, visit ? { visit_id: visit } : {});
    if (!d.found) {
      $("patientMsg").textContent = ref === visit
        ? `No visit "${visit}" is held.`
        : `No patient or visit "${ref}" is held.`;
      return;
    }
    const pt = d.patient || {};
    if (d.visit_found === false) {
      $("patientMsg").textContent = `${pt.patient_id || ref} has no visit "${visit}". Clear the visit ID to see every visit.`;
      return;
    }
    SHOWN_PATIENT = { patient_id: pt.patient_id || ref, visit_id: visit || "" };
    $("patientMsg").textContent = visit
      ? `Visit ${visit} of ${pt.patient_id || ref}.`
      : `${d.visits.length} visit${d.visits.length === 1 ? "" : "s"}, newest first.`;
    const scope = visit ? "this visit" : "every visit of this patient";
    $("patientOut").innerHTML = `<section class="panel"><div class="head wrap"><h3>${esc(pt.patient_id || ref)}</h3>
      <span class="tags">${tag("quiet", pt.cmed_clinic_code || "")}</span></div>
      <div class="body">
        <div class="pgrid">
          ${pField("Sex", pt.sex)}${pField("Date of birth", pt.date_of_birth)}${pField("Age", pt.age)}
          ${pField("Blood group", pt.blood_group)}${pField("Visits", String(pt.visits || d.visits.length))}
          ${pField("First visit", pt.first_visit)}${pField("Last visit", pt.last_visit)}
        </div>
        <div class="pfind pdl">
          <button type="button" class="primary" data-pcsv="visits">Download ${scope} (CSV)</button>
          <button type="button" class="ghost" data-pcsv="medicines">Medicines (CSV)</button>
          <span id="pcsvMsg" class="hint" role="status"></span>
        </div>
        <p class="hint">One row per visit: demographics, vitals and prescription side by side.
          The medicines file has one row per medicine.</p>
      </div></section>` + d.visits.map(visitCard).join("");
  } catch (err) {
    $("patientMsg").textContent = `Not shown: ${err.message}`;
  }
}
$("patientOut").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pcsv]");
  if (!b || !SHOWN_PATIENT) return;
  const params = { table: b.dataset.pcsv, ...SHOWN_PATIENT };
  if (b.dataset.visit) params.visit_id = b.dataset.visit;      // a visit card's own button
  downloadCsv(params, b, $("pcsvMsg"));
});
// Patient ID, visit ID, or both. A visit ID alone finds its patient and shows
// that one consultation.
$("patientForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const ref = $("patientRef").value.trim();
  const visit = $("visitRef").value.trim();
  if (!ref && !visit) {
    $("patientMsg").textContent = "Type a patient ID, a visit ID, or both.";
    return;
  }
  loadPatient(ref || visit, visit);
});

/* 19 Nov 2026: the demographics, prescriptions or medicines of every visit
   in the dates chosen, as a CSV. The server checks the dates and the role. */
function fillCsvClinics() {
  for (const id of ["csvClinic", "piecesClinic"]) {
    const sel = $(id);
    const chosen = sel.value;
    sel.innerHTML = `<option value="">All clinics</option>`
      + CLINICS.map((c) => `<option value="${esc(c.hospital_id)}">${esc(c.name || c.hospital_id)}</option>`).join("");
    sel.value = chosen;
  }
}
document.querySelectorAll("[data-csv]").forEach((b) => b.addEventListener("click", () => {
  const from = $("csvFrom").value, to = $("csvTo").value;
  if (from && to && from > to) {
    $("csvMsg").textContent = "From is after To.";
    return;
  }
  downloadCsv({ table: b.dataset.csv, from, to, hospital_id: $("csvClinic").value }, b, $("csvMsg"));
}));

function startBulk() {
  $("bulkPanel").hidden = false;
  $("batchesPanel").hidden = false;
  $("navDownloads").hidden = false;
  $("bEveryoneLabel").hidden = PORTAL.role !== "admin";
  // 049: a researcher's batch is one of the hospitals assigned to them.
  const mine = PORTAL.role === "researcher" ? (PORTAL.download_hospitals || []) : null;
  const offered = mine ? CLINICS.filter((c) => mine.includes(c.hospital_id)) : CLINICS;
  $("bClinic").innerHTML = (mine ? (offered.length ? ""
      : `<option value="">No hospital assigned to you</option>`)
    : `<option value="">All clinics</option>`)
    + offered.map((c) => `<option value="${esc(c.hospital_id)}">${esc(c.name || c.hospital_id)}</option>`).join("");
  if (!mine || mine.includes($("clinic").value)) $("bClinic").value = $("clinic").value;
  $("bFrom").value = $("from").value;
  $("bTo").value = $("to").value;
  loadBulk();
}

async function start() {
  try { await whoAmI(); } catch { return; }
  $("from").value = daysAgo(6);
  $("to").value = today();
  markRanges();
  try {
    const { clinics, doctors } = await api("/clinics", {});
    CLINICS = clinics || [];
    DOCTORS = doctors || [];
    // The summary's hospital list was filled before the clinics arrived.
    if (PORTAL && PORTAL.role === "admin") loadWatch();
    $("clinic").innerHTML = `<option value="">All clinics</option>`
      + CLINICS.map((c) => `<option value="${esc(c.hospital_id)}">`
        + `${esc(c.name || c.hospital_id)}</option>`).join("");
  } catch { /* refresh reports it */ }
  // A batch is audio: never for a data collector (036) or CMED staff (049).
  if (PORTAL && PORTAL.role !== "collector" && PORTAL.role !== "cmed") startBulk();
  // 18 Nov: a patient's clinical record - researchers and admins only.
  if (!PORTAL || PORTAL.role !== "collector") {
    $("navPatient").hidden = false;
    fillCsvClinics();
  }
  // A data collector views the event log but downloads nothing (036).
  // 049: nor CMED staff, who download the clinical record (JSON) only.
  if (PORTAL && (PORTAL.role === "collector" || PORTAL.role === "cmed")) {
    $("eventFiles").querySelectorAll("button").forEach((b) => { b.hidden = true; });
  }
  showView(location.hash.slice(1));
  await refresh();
}

start();

/* ============================================================
   036: data collectors. Their own shift page, and the admin's
   places and monthly shift log.
   ============================================================ */
async function collectorCall(method, path, body) {
  const response = await fetch(`/api/v2/dashboard/collector${path}`, {
    method, headers: { ...portalHeaders(), Accept: "application/json",
                       ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);
  return data;
}

/* How an alert went out; 'held' = quiet hours, kept for the next summary (16 Nov). */
// 18 Nov: alerts about something that just happened (watchers.REAL_TIME_EVENTS):
// how long from the event to the message reaching them.
const REAL_TIME = ["jabra_unplugged", "jabra_back", "jabra_not_plugged", "recording_problem",
                   "recording_without_jabra", "stopped_unusually", "stopped_technical"];

function tookWords(n) {
  if (!n.delivered_at) return "";
  const from = REAL_TIME.includes(n.event) && n.happened_at ? n.happened_at : n.created_at;
  const s = Math.max(0, Math.round((Date.parse(n.delivered_at) - Date.parse(from)) / 1000));
  return s < 90 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function sentTag(n) {
  if (n.delivered) {
    const took = tookWords(n);
    return tag(took && /min/.test(took) && REAL_TIME.includes(n.event) ? "warn" : "good",
               (n.channel === "telegram" ? "Telegram" : "email") + (took ? ` · ${took}` : ""),
               took ? (REAL_TIME.includes(n.event) ? "From the event to the message reaching them"
                                                   : "From the alert to the message reaching them") : "");
  }
  if (n.channel === "held") return tag("quiet", "held (night)");
  // 17 Nov: no Telegram yet - sent, marked late, the moment they connect it.
  if (n.channel === "waiting") return tag("warn", "waiting for Telegram", n.error || "");
  return tag("warn", "not sent", n.error || "");
}

const EVENT_LABELS = {
  jabra_unplugged: "Jabra unplugged", jabra_back: "Jabra back", jabra_not_plugged: "Jabra not plugged in",
  recording_problem: "Recording problem", pc_silent: "PC not reporting",
  recording_finished: "Recording saved", shift_summary: "Shift summary",
  no_shift_started: "Nobody on shift", escalation: "Not yet seen",
  recording_without_jabra: "Recorded without the Jabra", send_failures: "Alerts not sent",
  recording_too_long: "Recording not stopped", handled: "Seen", daily_report: "Daily report",
  pc_not_on: "PC not on today", patients_not_recorded: "Patients not recorded",
  silent_recording: "Recording is silent",
  still_not_fixed: "Still not fixed", problem_fixed: "Fixed", repeated_stops: "Repeated stops",
  shift_report: "Shift summary", summary_csv: "Summary CSV", summary_xlsx: "Summary workbook",
  stopped_unusually: "Stopped unusually", stopped_technical: "Stopped: technical problem",
};

let tgWaiting = false;   // 037: Connect Telegram was pressed; watch for the link
let tgTimer = null;
let shiftSeq = 0;
async function loadShift() {
  const seq = ++shiftSeq;
  let me;
  try { me = await collectorCall("GET", "/me"); }
  catch (err) {
    $("shiftState").textContent = `Could not load your shift: ${err.message}`;
    return;
  }
  if (seq !== shiftSeq) return;
  const p = me.place || {};
  $("shiftPlace").textContent = `${p.name || ""} · morning ${(p.morning || []).join("–")}`
    + ` · evening ${(p.evening || []).join("–")} · ${(me.hospitals || []).map(clinicName).join(", ") || "no hospitals yet"}`;
  const on = me.shift;
  $("shiftState").textContent = on
    ? `On shift (${on.shift}) since ${dhaka(on.started_at)}.`
    : "You are not on shift.";
  $("shiftStart").hidden = !!on;
  $("shiftEnd").hidden = !on;
  const tg = me.telegram || {};
  myTelegram = tg;
  $("shiftHint").textContent = tg.linked
    ? "While you are on shift, Telegram alerts you at once when the Jabra is unplugged, a patient "
      + "is recorded without the Jabra, a recording has a problem or a PC stops reporting. "
      + "Recordings that finish normally are only shown on the dashboard."
    : (me.mail_ready
      ? "Connect Telegram below so alerts reach your phone at once. Until then they come by email."
      : "Connect Telegram below so alerts reach your phone at once. Until then every alert is "
        + "listed below as soon as it happens, so keep this page open during your shift.");
  $("tgState").innerHTML = !tg.ready
    ? tag("warn", "Telegram not set up yet") + ` <span class="hint">AIMS LAB is finishing it.</span>`
    : tg.linked
      ? tag("good", "Telegram connected")
        + ` <span class="hint">${tg.username ? "@" + esc(tg.username) + " · " : ""}since ${esc(dhaka(tg.linked_at))}</span>`
      : tag("bad", "Telegram not connected")
        + ` <span class="hint">Press Connect Telegram, then Start in Telegram.</span>`;
  $("tgLink").hidden = !tg.ready;
  $("tgLink").textContent = tg.linked ? "Connect again" : "Connect Telegram";
  $("tgTest").hidden = !tg.linked;
  if (tg.linked && tgWaiting) { tgWaiting = false; clearInterval(tgTimer); }
  showDetails(me);
  const mails = me.notifications || [];
  const unseen = mails.filter((n) => n.urgent && !n.seen_at).length;
  const badge = $("badge-shift");
  badge.textContent = unseen ? String(unseen) : "";
  badge.hidden = !unseen;
  $("myMails").innerHTML = !mails.length
    ? empty("No alerts yet", "They appear here, and on Telegram, while you are on shift.", true)
    : `<table><thead><tr><th>When</th><th>What</th><th>Hospital</th><th>Message</th>`
      + `<th>Sent</th><th></th></tr></thead><tbody>`
      + mails.map((n) => `<tr class="${n.urgent && !n.seen_at ? "row-bad" : ""}">`
        + td("When", esc(dhaka(n.happened_at || n.created_at)))
        + td("What", tag(n.urgent ? "bad" : "good", EVENT_LABELS[n.event] || human(n.event)))
        + td("Hospital", esc(n.hospital_id ? clinicName(n.hospital_id) : ""))
        + td("Message", esc(n.subject), "wrap")
        + td("Sent", sentTag(n))
        + td("", n.seen_at ? `<span class="hint">seen</span>`
          : `<button type="button" class="chip" data-seen="${esc(n.id)}">Mark seen</button>`)
        + `</tr>`).join("") + `</tbody></table>`;
}

$("shiftStart").addEventListener("click", async () => {
  if (myTelegram.ready && !myTelegram.linked && !window.confirm(
      "Your Telegram is not connected, so alerts will not reach your phone during this "
      + "shift. Press Cancel, then Connect Telegram (it takes a minute). Or press OK to "
      + "start anyway: alerts then wait and arrive, marked late, once you connect.")) return;
  $("shiftStart").disabled = true;
  try { await collectorCall("POST", "/shift/start"); }
  catch (err) { window.alert(`Not started: ${err.message}`); }
  $("shiftStart").disabled = false;
  loadShift();
});
$("shiftEnd").addEventListener("click", async () => {
  if (!window.confirm("End your shift now? A summary of the shift is emailed to you.")) return;
  $("shiftEnd").disabled = true;
  try { await collectorCall("POST", "/shift/end"); }
  catch (err) { window.alert(`Not ended: ${err.message}`); }
  $("shiftEnd").disabled = false;
  loadShift();
});
$("myMails").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-seen]");
  if (!b) return;
  b.disabled = true;
  try { await collectorCall("POST", `/notifications/${encodeURIComponent(b.dataset.seen)}/seen`); }
  catch (err) { window.alert(`Not marked: ${err.message}`); }
  loadShift();
});

/* Places (admins): which hospitals each place's collectors see. */
let PLACES = [];
async function loadPlaces() {
  try {
    const { places, hospitals, mail, telegram } = await adminCall("GET", "/api/v2/admin/places");
    $("tgAdminState").innerHTML = telegram && telegram.ready
      ? tag("good", "Telegram works") + ` <span class="hint">bot @${esc(telegram.bot)}</span>`
      : tag("bad", "Telegram not working") + ` <span class="hint">${esc((telegram && telegram.problem) || "")}</span>`;
    PLACES = places || [];
    $("placesHint").textContent = "A place is a group of hospitals. Its data collectors see only these hospitals.";
    $("mailState").innerHTML = mail && mail.ready
      ? tag("good", "Email works") + ` <span class="hint">sent as ${esc(mail.sender)}</span>`
      : tag("bad", "Email not working") + ` <span class="hint">${esc((mail && mail.problem) || "")}</span>`;
    const options = (hospitals || []).map((h) =>
      `<option value="${esc(h.hospital_id)}">${esc(h.name || h.hospital_id)}</option>`).join("");
    $("pPlace").innerHTML = `<option value="">Pick a place</option>`
      + PLACES.map((p) => `<option value="${esc(p.place_id)}">${esc(p.name)}</option>`).join("");
    $("places").innerHTML = !PLACES.length
      ? empty("No places yet", "Add one above, then put its hospitals in it.", true)
      : `<table><thead><tr><th>Place</th><th>Shifts</th><th>Hospitals</th><th>Data collectors</th>`
        + `<th>On shift now</th></tr></thead><tbody>`
        + PLACES.map((p) => `<tr>`
          + td("Place", `<b>${esc(p.name)}</b>`)
          + td("Shifts", `morning ${esc(p.morning.join("–"))}<br>evening ${esc(p.evening.join("–"))}`)
          + td("Hospitals", `<span class="placehosp">`
            + p.hospitals.map((h) => `${tag("info", clinicName(h))}`
              + `<button type="button" class="chip" data-unhosp="${esc(h)}" data-place="${esc(p.place_id)}" `
              + `title="Take ${esc(h)} out of ${esc(p.name)}">×</button>`).join("")
            + ` <select data-addfor="${esc(p.place_id)}" aria-label="Add a hospital to ${esc(p.name)}">`
            + `<option value="">Add hospital…</option>${options}</select></span>`)
          + td("Data collectors", p.collectors.length
            ? p.collectors.map((c) => esc(c.name || c.email)).join("<br>") : `<span class="hint">none yet</span>`)
          + td("On shift now", p.on_shift.length
            ? p.on_shift.map((c) => `${esc(c.email)} (${esc(c.shift)})`).join("<br>") : `<span class="hint">nobody</span>`)
          + `</tr>`).join("") + `</tbody></table>`;
  } catch (err) {
    $("places").innerHTML = empty("Places could not be loaded", err.message, true);
  }
}

$("placeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await adminCall("POST", "/api/v2/admin/places", {
      name: $("plName").value.trim(), morning_start: $("plMs").value, morning_end: $("plMe").value,
      evening_start: $("plEs").value, evening_end: $("plEe").value });
    $("placeMsg").textContent = `Added ${$("plName").value.trim()}. Now add its hospitals.`;
    $("plName").value = "";
    loadPlaces();
  } catch (err) { $("placeMsg").textContent = `Not added: ${err.message}`; }
});

$("places").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-addfor]");
  if (!sel || !sel.value) return;
  try {
    await adminCall("POST", `/api/v2/admin/places/${encodeURIComponent(sel.dataset.addfor)}/hospitals`,
                    { hospital_id: sel.value });
  } catch (err) { window.alert(`Not added: ${err.message}`); }
  loadPlaces();
});

$("places").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-unhosp]");
  if (!b) return;
  if (!window.confirm(`Take ${b.dataset.unhosp} out of this place? Its collectors stop seeing it.`)) return;
  try {
    await adminCall("POST", `/api/v2/admin/places/${encodeURIComponent(b.dataset.place)}/hospitals`,
                    { hospital_id: b.dataset.unhosp, remove: true });
  } catch (err) { window.alert(`Not done: ${err.message}`); }
  loadPlaces();
});

$("pRole").addEventListener("change", () => {
  $("pPlaceLabel").hidden = $("pRole").value !== "collector";
});

/* The shift log: one month, as a CSV for salaries. */
function setShiftLogLink() {
  const month = $("slMonth").value;
  $("slDownload").href = "/api/v2/admin/shifts.csv" + (month ? `?month=${encodeURIComponent(month)}` : "");
}
$("slMonth").value = today().slice(0, 7);
setShiftLogLink();
$("slMonth").addEventListener("change", setShiftLogLink);

/* 036: one test mail, with Resend's exact answer, so mail problems are found at once. */
$("mailTest").addEventListener("click", async () => {
  $("mailTest").disabled = true;
  $("mailState").innerHTML = `<span class="hint">Sending a test mail to you...</span>`;
  try {
    const r = await adminCall("POST", "/api/v2/admin/mail-test", {});
    $("mailState").innerHTML = r.sent
      ? tag("good", "Test mail sent") + ` <span class="hint">to ${esc(r.to)}: check the inbox and spam folder</span>`
      : tag("bad", "Test mail not sent") + ` <span class="hint">${esc(r.error || "")}</span>`;
  } catch (err) {
    $("mailState").innerHTML = tag("bad", "Test failed") + ` <span class="hint">${esc(err.message)}</span>`;
  }
  $("mailTest").disabled = false;
});

/* 037: Telegram - a one-time link, then the page watches until it is connected. */
$("tgLink").addEventListener("click", async () => {
  $("tgLink").disabled = true;
  try {
    const r = await collectorCall("POST", "/telegram/link");
    window.open(r.url, "_blank", "noopener");
    $("tgState").innerHTML = `<span class="hint">Telegram opened: press <b>Start</b> there. `
      + `If it did not open, <a href="${esc(r.url)}" target="_blank" rel="noopener">tap here</a>. `
      + `This page shows Connected within half a minute.</span>`;
    tgWaiting = true;
    clearInterval(tgTimer);
    let polls = 0;
    tgTimer = setInterval(() => {
      if (++polls > 40 || !tgWaiting) { clearInterval(tgTimer); return; }
      loadShift();
    }, 5000);
  } catch (err) {
    $("tgState").innerHTML = tag("bad", "Could not connect") + ` <span class="hint">${esc(err.message)}</span>`;
  }
  $("tgLink").disabled = false;
});
/* 040: official name, lab ID and phone - yellow until all three are saved. */
function showDetails(d, saved) {
  const fields = [["myName", "official_name"], ["myCode", "collector_code"], ["myPhone", "phone"]];
  for (const [id, key] of fields) {
    if (document.activeElement !== $(id)) $(id).value = d[key] || "";
  }
  const missing = [["official name", d.official_name], ["ID", d.collector_code],
                   ["phone", d.phone]].filter((x) => !x[1]).map((x) => x[0]);
  $("detailsPanel").classList.toggle("needs", missing.length > 0);
  $("detailsState").innerHTML = missing.length
    ? tag("warn", "Fill in your details") + ` <span class="hint">Missing: ${esc(missing.join(", "))}</span>`
    : tag("good", saved ? "Saved" : "Complete");
}
$("myDetailsSave").addEventListener("click", async () => {
  $("myDetailsSave").disabled = true;
  try {
    const r = await collectorCall("POST", "/details", {
      official_name: $("myName").value, collector_code: $("myCode").value,
      phone: $("myPhone").value });
    showDetails(r, true);
  } catch (err) {
    $("detailsState").innerHTML = tag("bad", "Not saved") + ` <span class="hint">${esc(err.message)}</span>`;
  }
  $("myDetailsSave").disabled = false;
});
$("tgTest").addEventListener("click", async () => {
  $("tgTest").disabled = true;
  try {
    const r = await collectorCall("POST", "/telegram/test");
    $("tgState").innerHTML = r.sent
      ? tag("good", "Test alert sent") + ` <span class="hint">Your phone should have made a sound. If not, turn on Telegram's notifications.</span>`
      : tag("bad", "Test alert not sent") + ` <span class="hint">${esc(r.error || "")}</span>`;
  } catch (err) {
    $("tgState").innerHTML = tag("bad", "Test failed") + ` <span class="hint">${esc(err.message)}</span>`;
  }
  $("tgTest").disabled = false;
});

/* ============================================================
   038: researchers. The hospitals they look after, Telegram, how
   many alerts they want, the evening report, and their alerts.
   ============================================================ */
async function watchCall(method, path, body) {
  const response = await fetch(`/api/v2/dashboard/watch${path}`, {
    method, headers: { ...portalHeaders(), Accept: "application/json",
                       ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);
  return data;
}

let watchWaiting = false;
let watchTimer = null;
async function loadWatch(first) {
  let me;
  try { me = await watchCall("GET", "/me"); }
  catch (err) { $("wState").textContent = `Could not load: ${err.message}`; return; }
  const hs = me.hospitals || [];
  $("wHospitals").textContent = hs.length
    ? hs.map((h) => `${h.hospital_id}${h.name ? " " + h.name : ""}`).join(", ")
    : "None yet: an admin gives you hospitals in People.";
  $("mineField").hidden = !hs.length;
  if (first && hs.length) {          // start on their own hospitals; one click shows all
    $("mine").checked = true;
    MINE = true;
    const again = () => (BUSY ? setTimeout(again, 800) : refresh());
    again();
  }
  $("wMode").value = me.mode || "everything";
  $("wReportAt").textContent = me.report_at || "21:00";
  $("wMiddayAt").textContent = me.midday_at || "14:30";
  if (first) {   // the summary most recently sent
    const hm = new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Dhaka", hour12: false }).slice(0, 5);
    $("wRepShift").value = hm >= (me.report_at || "21:00") ? "evening" : (hm >= (me.midday_at || "14:30") ? "morning" : "evening");
  }
  // 24 Nov 2026: an admin may take any clinic's summary and workbook; a
  // researcher, the hospitals they look after. Before, an admin who looks
  // after none had an empty list, and the workbook asked for "no hospital".
  const choices = PORTAL && PORTAL.role === "admin" && CLINICS.length ? CLINICS : hs;
  $("wRepHosp").innerHTML = choices.map((h) =>
    `<option value="${esc(h.hospital_id)}">${esc(h.hospital_id)} ${esc(h.name || "")}</option>`).join("");
  $("wRepShow").disabled = !choices.length;
  $("wRepCsv").disabled = !choices.length;
  const tg = me.telegram || {};
  $("wTgState").innerHTML = !tg.ready
    ? tag("warn", "Telegram not set up on the server yet")
    : tg.linked
      ? tag("good", "Telegram connected")
        + ` <span class="hint">${tg.username ? "@" + esc(tg.username) + " \u00b7 " : ""}since ${esc(dhaka(tg.linked_at))}</span>`
      : tag("bad", "Telegram not connected")
        + ` <span class="hint">Press Connect Telegram, then Start in Telegram.</span>`;
  $("wTgLink").hidden = !tg.ready;
  $("wTgLink").textContent = tg.linked ? "Connect again" : "Connect Telegram";
  $("wTgTest").hidden = !tg.linked;
  if (tg.linked && watchWaiting) { watchWaiting = false; clearInterval(watchTimer); }
  const groups = me.contacts || [];
  $("wContacts").innerHTML = !groups.length
    ? empty("Nobody to show", "No place holds your hospitals yet, so no collector is alerted for them.", true)
    : `<table><thead><tr><th>Hospital</th><th>Place</th><th>Data collector</th><th>Phone</th>`
      + `<th>Telegram</th><th>On shift</th></tr></thead><tbody>`
      + groups.map((g) => (g.people.length ? g.people : [null]).map((c) => `<tr>`
        + td("Hospital", esc(clinicName(g.hospital_id)))
        + td("Place", esc(g.place))
        + td("Data collector", c ? esc(c.name) + (c.code ? ` <code>${esc(c.code)}</code>` : "")
          + ((c.missing || []).length ? ` ${tag("warn", "missing: " + c.missing.join(", "))}` : "")
          : tag("bad", "No collector"))
        + td("Phone", c && c.phone
          ? `<a href="tel:${esc(c.phone.replace(/[^0-9+]/g, ""))}">${esc(c.phone)}</a>`
          : (c ? tag("warn", "not given") : ""))
        + td("Telegram", c && c.telegram
          ? `<a href="https://t.me/${esc(c.telegram)}" target="_blank" rel="noopener">@${esc(c.telegram)}</a>`
          : (c ? tag(c.telegram_linked ? "quiet" : "warn",
                     c.telegram_linked ? "connected, no username" : "not connected") : ""))
        + td("On shift", g.on_shift && c
          ? tag("good", `${c.shift || ""} since ${dhaka(c.since)}`)
          : tag("bad", "Nobody on shift"))
        + `</tr>`).join("")).join("") + `</tbody></table>`;
  const notes = me.notifications || [];
  $("wAlerts").innerHTML = !notes.length
    ? empty("No alerts yet", "Alerts for your hospitals appear here, and on Telegram.", true)
    : `<table><thead><tr><th>When</th><th>What</th><th>Hospital</th><th>Message</th><th>Sent</th></tr></thead><tbody>`
      + notes.map((n) => `<tr>`
        + td("When", esc(dhaka(n.happened_at || n.created_at)))
        + td("What", tag(n.urgent ? "bad" : (["handled", "problem_fixed"].includes(n.event) ? "good" : "info"),
                         EVENT_LABELS[n.event] || human(n.event)))
        + td("Hospital", esc(n.hospital_id ? clinicName(n.hospital_id) : ""))
        + td("Message", esc(n.subject), "wrap")
        + td("Sent", sentTag(n))
        + `</tr>`).join("") + `</tbody></table>`;
}

$("mine").addEventListener("change", () => { MINE = $("mine").checked; refresh(); });
$("wMode").addEventListener("change", async () => {
  try { await watchCall("POST", "/mode", { mode: $("wMode").value }); }
  catch (err) { window.alert(`Not saved: ${err.message}`); }
  loadWatch();
});
$("wRepShow").addEventListener("click", async () => {
  $("wReport").textContent = "Building the summary...";
  try {
    const r = await watchCall("GET", `/report?hospital_id=${encodeURIComponent($("wRepHosp").value)}`
      + `&shift=${encodeURIComponent($("wRepShift").value)}`
      + ($("wRepDay").value ? `&day=${encodeURIComponent($("wRepDay").value)}` : ""));
    $("wReport").textContent = r.text;
  } catch (err) { $("wReport").textContent = `Could not build it: ${err.message}`; }
});
$("wRepCsv").addEventListener("click", () => {
  // 20 Nov: the chosen hospital's workbook (four sheets), not one CSV of all.
  const link = document.createElement("a");
  link.href = `/api/v2/dashboard/watch/summary.xlsx?hospital_id=${encodeURIComponent($("wRepHosp").value)}`
    + `&shift=${encodeURIComponent($("wRepShift").value)}`
    + ($("wRepDay").value ? `&day=${encodeURIComponent($("wRepDay").value)}` : "");
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
});
$("wTgLink").addEventListener("click", async () => {
  $("wTgLink").disabled = true;
  try {
    const r = await watchCall("POST", "/telegram/link");
    window.open(r.url, "_blank", "noopener");
    $("wTgState").innerHTML = `<span class="hint">Telegram opened: press <b>Start</b> there. `
      + `If it did not open, <a href="${esc(r.url)}" target="_blank" rel="noopener">tap here</a>.</span>`;
    watchWaiting = true;
    clearInterval(watchTimer);
    let polls = 0;
    watchTimer = setInterval(() => {
      if (++polls > 40 || !watchWaiting) { clearInterval(watchTimer); return; }
      loadWatch();
    }, 5000);
  } catch (err) {
    $("wTgState").innerHTML = tag("bad", "Could not connect") + ` <span class="hint">${esc(err.message)}</span>`;
  }
  $("wTgLink").disabled = false;
});
$("wTgTest").addEventListener("click", async () => {
  $("wTgTest").disabled = true;
  try {
    const r = await watchCall("POST", "/telegram/test");
    $("wTgState").innerHTML = r.sent ? tag("good", "Test alert sent")
      : tag("bad", "Test alert not sent") + ` <span class="hint">${esc(r.error || "")}</span>`;
  } catch (err) {
    $("wTgState").innerHTML = tag("bad", "Test failed") + ` <span class="hint">${esc(err.message)}</span>`;
  }
  $("wTgTest").disabled = false;
});
