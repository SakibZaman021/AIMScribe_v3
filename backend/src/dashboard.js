// AIMS LAB server dashboard script. Served from /api/v2/dashboard/static/.
// Vanilla JS, no packages. The admin key lives in memory for this tab only.
"use strict";

let ADMIN_KEY = "";
// Signed in with Google (portal.py): the session cookie is the credential,
// and every request carries X-AIMS-Portal so no other site can use it.
let PORTAL = null;          // {email, role} when signed in
const portalHeaders = () => (PORTAL ? { "X-AIMS-Portal": "1" } : { "X-Admin-Key": ADMIN_KEY });
let CLINICS = [];
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

async function api(path, params, retried) {
  if (!PORTAL && !ADMIN_KEY) await askKey();
  const query = new URLSearchParams(
    Object.entries(params || {}).filter(([, v]) => v !== "" && v != null));
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
  pcs: ["PCs", "The recorders in the consulting rooms", true],
  downloads: ["Downloads", "Recordings in numbered batches, one ZIP each", false],
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
        + `<span>${tag("warn", human(r.close_reason))} ${who(r)}</span></li>`).join("")
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
  ["all", "All", () => true],
  ["approval", "Waiting for approval", (q) => q.approval === "pending" && !q.revoked],
  ["offline", "Offline", (q) => q.silent && !q.revoked],
  ["outdated", "Needs update", (q) => q.outdated && !q.revoked],
  ["unencrypted", "Disk not encrypted", (q) => q.disk_encrypted === false && !q.revoked],
  ["microphone", "Not on the Jabra", (q) => q.microphone_is_clinic === false && !q.revoked],
  ["holding", "Audio waiting", (q) => (q.pending_segments || 0) > 0],
  ["held", "Holding a recording", (q) => (q.held_count || 0) > 0],
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
      const state = (q.revoked ? tag("quiet", "Revoked")
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
      return `<tr class="${rowTone}">`
        + td("PC", `<b>${esc(q.machine_name || String(q.device_id).slice(0, 8))}</b>`)
        + td("State", state)
        + td("Last seen", esc(ago(q.minutes_since_seen)))
        + td("Version", version)
        + td("Audio waiting", waiting, "num")
        + td("Held", held, "num")
        + td("Microphone", mic)
        + td("Disk", disk)
        + td("Clock", clock, "num")
        + td("Offline (7 days)", offlineText(q.offline_periods))
        + `</tr>`;
    }).join("");
  });
  $("rooms").innerHTML = `<table><thead><tr><th>PC</th><th>State</th><th>Last seen</th>`
    + `<th>Version</th><th class="num">Audio waiting</th><th class="num">Held</th><th>Microphone</th><th>Disk</th>`
    + `<th class="num">Clock</th><th>Offline (7 days)</th></tr></thead><tbody>${body}</tbody></table>`;
}

// A PC that registered itself: a person approves or rejects it, by name.
$("rooms").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-decide]");
  if (!b) return;
  const approve = b.dataset.decide === "approve";
  const who = (window.prompt(`${approve ? "Approve" : "Reject"} this PC. Your name, for the audit log:`) || "").trim();
  if (who.length < 2) return;
  if (!approve && !window.confirm("Reject and revoke this PC? Anything it recorded that CMED did not confirm is erased on its date.")) return;
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
// How a recording ended. The first three are the normal ends (dashboard.py,
// _ENDED_NORMALLY); the others are "stopped early".
const ENDED = {
  superseded_by_new_patient: ["good", "Next patient"],
  counselling_limit: ["good", "2 min after prescription"],
  prescription_built: ["good", "Prescription"],
  doctor_stopped: ["warn", "Stopped by the doctor"],
  stopped_from_tray: ["warn", "Stopped by the doctor"],
  pc_shutdown: ["warn", "PC shut down"],
  agent_shutdown: ["warn", "Recorder closed"],
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
  if (!r.copied || r.confirmation !== "confirmed") return `<span class="hint">–</span>`;
  if (!PORTAL) return `<span class="hint">sign in</span>`;
  const base = `/api/v2/dashboard/recordings/${encodeURIComponent(r.session_id)}/audio`;
  const files = r.files || [];
  const link = (href, label, title) =>
    `<a class="copy dl" href="${esc(href)}" title="${esc(title)}">${esc(label)}</a>`;
  if (files.length > 1) {
    return files.map((f) => link(`${base}?part=${f.part_no}`, `Part ${f.part_no}`,
      `Download patient file ${f.part_no} (WAV)`)).join(" ");
  }
  return link(base, "Download", "Download the WAV");
}

function recordingsTable(list) {
  if (!list.length) {
    return STATE
      ? empty(`No recordings: ${STATE_LABELS[STATE] || human(STATE)}`, "Nothing in this state for this range.", true)
      : empty("No recordings in this range", "Pick a wider range or another clinic.");
  }
  const showClinic = !$("clinic").value;
  return `<table><thead><tr><th>Date</th>${showClinic ? "<th>Clinic</th>" : ""}<th>Doctor</th>`
    + `<th>Patient files and rule</th><th class="num">Length</th><th>CMED</th><th>Status</th>`
    + `<th>Stored</th><th>Audio</th><th>Folder on server</th></tr></thead><tbody>`
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
        if (!status.length) status.push(`<span class="hint">${esc(human(r.status) || "–")}</span>`);
        const tone = r.quarantine_reason || r.confirmation === "unconfirmed" ? "row-bad"
          : r.confirmation === "pending" ? "row-warn" : "";
        return `<tr class="${tone}">`
          + td("Date", `${esc(r.date || "")}${t ? `<span class="sub2">${esc(t)}</span>` : ""}`)
          + (showClinic ? td("Clinic", esc(clinicName(r.hospital_id))) : "")
          + td("Doctor", esc(r.doctor_id))
          + td("Patient", filesText(r), "wrap")
          + td("Length", `${num(r.minutes || 0)} min`, "num")
          + td("CMED", confirmTag(r.confirmation))
          + td("Status", `<span class="tags">${status.join("")}</span>`)
          + td("Stored", storedTag(r))
          + td("Audio", audioLinks(r))
          + td("Folder", folder
            ? `<span title="${esc(folder)}">${esc(folder)}</span>`
              + ` <button type="button" class="copy" data-copy="${esc(folder)}">Copy</button>`
            : `<span class="hint">–</span>`, "path")
          + `</tr>`;
      }).join("") + `</tbody></table>`;
}

$("recTable").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-copy]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.copy);
    b.textContent = "Copied";
    setTimeout(() => { b.textContent = "Copy"; }, 1200);
  } catch { b.textContent = "Select it"; }   // clipboard blocked: the path is on screen
});

function drawStateFilters() {
  const c = (LAST && LAST.confirmation.totals) || {};
  const i = (LAST && LAST.integrity) || {};
  const counts = {
    pending: c.pending, unconfirmed: c.unconfirmed, confirmed: c.confirmed,
    quarantined: i.quarantined_total, stopped_early: i.stopped_early_total,
    uncopied: LAST && LAST.copies.recordings_without_a_cloud_copy,
    refused: c.refused, expired: c.expired, legacy: c.legacy,
  };
  const order = ["", "pending", "unconfirmed", "confirmed", "quarantined", "stopped_early",
                 "unarchived", "uncopied", "refused", "expired", "legacy"];
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
async function loadRecordings() {
  const clinic = $("clinic").value;
  const label = clinic ? $("clinic").selectedOptions[0].textContent : "All clinics";
  $("recTitle").textContent = STATE ? `Recordings · ${STATE_LABELS[STATE] || human(STATE)}` : "Recordings";
  const seq = ++recSeq;
  if (!$("recTable").firstChild) $("recTable").innerHTML = empty("Loading");
  try {
    const params = { from: $("from").value, to: $("to").value, hospital_id: clinic, limit: 200 };
    if (STATE) params.state = STATE;
    const d = await api("/recordings", params);
    if (seq !== recSeq) return;
    const list = d.recordings || [];
    $("recTable").innerHTML = recordingsTable(list);
    $("recHint").textContent = `${label} · ${num(list.length)}${list.length >= 200 ? " (first 200)" : ""} · newest first`;
  } catch (err) {
    if (seq !== recSeq) return;
    $("recTable").innerHTML = empty("Could not load recordings", err.message);
  }
}

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
    PORTAL = { email: me.email, role: me.role };
    $("who").textContent = `${me.email} \u00b7 ${me.role}`;
    $("who").hidden = false;
    $("signout").hidden = false;
    if (me.role === "admin") {
      $("peoplePanel").hidden = false;
      $("navPeople").hidden = false;
      loadPeople();
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

async function loadPeople() {
  try {
    const { people, mail } = await adminCall("GET", "/api/v2/admin/people");
    $("peopleHint").textContent = mail ? "An invitation email is sent when you give access"
      : "No mail account is set, so send them the link yourself";
    const when = (iso) => (iso ? new Date(iso).toLocaleString([], { timeZone: "Asia/Dhaka" }) : "never");
    $("people").innerHTML = `<table><thead><tr><th>Google account</th><th>Role</th>`
      + `<th>Given by</th><th>Last signed in</th><th>Database login</th><th></th></tr></thead><tbody>`
      + people.map((p) => `<tr>`
        + td("Google account", `<b>${esc(p.email)}</b>${p.display_name ? `<br><span class="hint">${esc(p.display_name)}</span>` : ""}`)
        + td("Role", p.role === "admin" ? tag("warn", "Admin") : tag("good", "Researcher"))
        + td("Given by", esc(p.added_by || ""))
        + td("Last signed in", esc(when(p.last_signed_in)))
        + td("Database login", (p.db_user ? `<code>${esc(p.db_user)}</code><br>` : "")
          + `<button type="button" class="chip" data-dblogin="${esc(p.email)}">`
          + `${p.db_user ? "New password" : "Create login"}</button>`)
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
    const r = await adminCall("POST", "/api/v2/admin/people", { email, role: $("pRole").value });
    $("peopleMsg").textContent = r.invited ? `Invitation sent to ${r.email}.`
      : `${r.email} can now sign in at ${r.link}`;
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

$("people").addEventListener("click", async (e) => {
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

function startBulk() {
  $("bulkPanel").hidden = false;
  $("batchesPanel").hidden = false;
  $("navDownloads").hidden = false;
  $("bEveryoneLabel").hidden = PORTAL.role !== "admin";
  $("bClinic").innerHTML = `<option value="">All clinics</option>`
    + CLINICS.map((c) => `<option value="${esc(c.hospital_id)}">${esc(c.name || c.hospital_id)}</option>`).join("");
  $("bClinic").value = $("clinic").value;
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
    const { clinics } = await api("/clinics", {});
    CLINICS = clinics || [];
    $("clinic").innerHTML = `<option value="">All clinics</option>`
      + CLINICS.map((c) => `<option value="${esc(c.hospital_id)}">`
        + `${esc(c.name || c.hospital_id)}</option>`).join("");
  } catch { /* refresh reports it */ }
  if (PORTAL) startBulk();
  showView(location.hash.slice(1));
  await refresh();
}

start();
