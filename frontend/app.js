const stationIds = ["queued", "doing", "review", "approved", "merged", "changes_requested", "stuck"];
const aliases = { in_flight: "doing", implementation: "doing", submitted: "review", needs_changes: "changes_requested" };

function key(value) {
  const raw = String(value ?? "").trim();
  const match = raw.match(/\d+/);
  return match ? match[0].padStart(3, "0") : raw;
}

function stateOf(task) {
  const raw = String(task.state || task.final_state || "queued").toLowerCase();
  return aliases[raw] || raw;
}

function titleFromMarkdown(content = "") {
  const lines = String(content).split(/\r?\n/);
  const marker = lines.findIndex((line) => /^##\s+(title|标题)\s*$/i.test(line.trim()));
  if (marker >= 0) {
    const next = lines.slice(marker + 1).find((line) => line.trim() && !line.trim().startsWith("#"));
    if (next) return next.trim();
  }
  const h1 = lines.find((line) => /^#\s+/.test(line));
  return h1 ? h1.replace(/^#\s+/, "").replace(/^Task\s+\d+\s*[—:-]?\s*/i, "").trim() : "";
}

function mergeTasks(...collections) {
  const map = new Map();
  collections.flat().filter(Boolean).forEach((item) => {
    if (!item || typeof item !== "object") return;
    const task = key(item.task ?? item.id ?? item.number);
    if (!task) return;
    const previous = map.get(task) || { task };
    const title = item.title || item.name || titleFromMarkdown(item.content || item.body || item.markdown);
    const liveState = item.state && item.final_state == null ? item.state : item.final_state;
    const liveRound = item.round != null && item.review_rounds == null ? item.round : item.review_rounds;
    map.set(task, {
      ...previous,
      ...item,
      task,
      ...(title ? { title } : {}),
      ...(liveState ? { final_state: liveState } : {}),
      ...(liveRound != null ? { review_rounds: liveRound } : {}),
    });
  });
  return [...map.values()];
}

function average(values) {
  const clean = values.map(Number).filter(Number.isFinite);
  return clean.length ? clean.reduce((sum, value) => sum + value, 0) / clean.length : 0;
}

function inferSummary(tasks, supplied = {}) {
  const active = tasks.filter((task) => stateOf(task) !== "merged");
  const merged = tasks.filter((task) => stateOf(task) === "merged");
  const reworked = tasks.filter((task) => Number(task.round ?? task.review_rounds ?? 0) >= 2);
  return {
    ...supplied,
    n_tasks: supplied.n_tasks ?? tasks.length,
    n_inflight: supplied.n_inflight ?? active.length,
    n_merged: supplied.n_merged ?? merged.length,
    n_rework: supplied.n_rework ?? reworked.length,
    rework_rate: supplied.rework_rate ?? (tasks.length ? reworked.length / tasks.length : 0),
    avg_e2e: supplied.avg_e2e ?? average(merged.map((task) => task.end_to_end)),
    throughput_per_day: supplied.throughput_per_day ?? 0,
  };
}

function adapt(raw) {
  if (Array.isArray(raw)) {
    const tasks = mergeTasks(raw);
    return { tasks, summary: inferSummary(tasks), activity: [], crew: [] };
  }
  const payload = raw && typeof raw === "object" ? raw : {};
  const analytics = payload.analytics && typeof payload.analytics === "object" ? payload.analytics : {};
  const tasks = mergeTasks(analytics.tasks, payload.tasks, payload.statuses, payload.task_specs, payload.taskSpecs, payload.task_metadata);
  return {
    tasks,
    summary: inferSummary(tasks, payload.summary || analytics.summary || {}),
    activity: payload.activity || [],
    crew: payload.crew || [],
  };
}

function duration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "N/A";
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  if (minutes < 60) return `${minutes}m${String(secs).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hours < 24) return `${hours}h${String(mins).padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d${String(hours % 24).padStart(2, "0")}h`;
}

function compact(value) {
  if (value == null || !Number.isFinite(Number(value))) return "N/A";
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value));
}

function recency(task) {
  const raw = task.updated || task.merged_at || task.verdict_at || task.submitted_at;
  const parsed = raw ? Date.parse(raw) : Number.NaN;
  if (Number.isFinite(parsed)) return parsed;
  return Number.parseInt(String(task.task || "0"), 10) || 0;
}

function taskCard(task) {
  const card = document.getElementById("task-card-template").content.firstElementChild.cloneNode(true);
  const state = stateOf(task);
  card.dataset.state = state;
  card.querySelector(".task-id").textContent = `#${key(task.task)}`;
  card.querySelector(".task-state").textContent = state.replaceAll("_", " ");
  card.querySelector(".task-title").textContent = task.title || String(task.task || "Task").replace(/^\d{3}-?/, "").replace(/[-_]/g, " ");
  const meta = [];
  const rounds = Number(task.round ?? task.review_rounds ?? 0);
  if (rounds > 0) meta.push(`round ${rounds}`);
  if (task.impl_seconds != null) meta.push(`impl ${duration(task.impl_seconds)}`);
  if (task.review_seconds != null) meta.push(`review ${duration(task.review_seconds)}`);
  if (task.end_to_end != null) meta.push(`e2e ${duration(task.end_to_end)}`);
  card.querySelector(".task-meta").textContent = meta.join(" · ") || "new task";
  return card;
}

function renderStations(tasks) {
  const buckets = new Map(stationIds.map((state) => [state, []]));
  tasks.forEach((task) => {
    let state = stateOf(task);
    if (!stationIds.includes(state)) state = task.end_to_end_open ? "doing" : "merged";
    buckets.get(state).push(task);
  });

  stationIds.forEach((state) => {
    const target = document.getElementById(`station-${state}`);
    if (!target) return;
    const items = buckets.get(state).sort((a, b) => recency(b) - recency(a));
    target.replaceChildren();
    target.classList.toggle("is-scrollable", items.length > 3);
    target.setAttribute("aria-label", `${items.length} tasks in ${state.replaceAll("_", " ")}`);
    if (items.length > 3) {
      target.tabIndex = 0;
      target.title = "Scroll to inspect older tasks";
    } else {
      target.removeAttribute("tabindex");
      target.removeAttribute("title");
    }

    const station = target.closest(".station");
    if (station) {
      let badge = station.querySelector(".station-count");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "station-count";
        station.querySelector(".station-header")?.append(badge);
      }
      badge.textContent = String(items.length);
    }

    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No buses";
      target.append(empty);
    } else {
      items.forEach((task) => target.append(taskCard(task)));
    }
  });
}

function renderSummary(summary, tasks) {
  document.getElementById("metric-total").textContent = summary.n_tasks ?? tasks.length;
  document.getElementById("metric-inflight").textContent = summary.n_inflight ?? tasks.filter((task) => stateOf(task) !== "merged").length;
  document.getElementById("metric-cycle").textContent = duration(summary.avg_e2e);
  document.getElementById("metric-rework").textContent = `${Math.round(Number(summary.rework_rate || 0) * 100)}%`;
  document.getElementById("metric-throughput").textContent = Number(summary.throughput_per_day || 0).toFixed(1);
  const oneShot = Math.max(0, Math.min(100, Math.round((1 - Number(summary.rework_rate || 0)) * 100)));
  document.getElementById("one-shot-donut").style.setProperty("--percent", oneShot);
  document.getElementById("one-shot-value").textContent = `${oneShot}%`;
  document.getElementById("one-shot-copy").textContent = `${oneShot}% passed without a second implementation round.`;
  document.getElementById("token-input").textContent = compact(summary.sum_in);
  document.getElementById("token-output").textContent = compact(summary.sum_out);
  document.getElementById("token-cache").textContent = compact(summary.sum_cr);
  document.getElementById("token-sessions").textContent = summary.sum_sessions ?? "N/A";
}

function renderBars(tasks) {
  const stages = [
    ["Queue", average(tasks.map((task) => task.queue_to_claim))],
    ["Implement", average(tasks.map((task) => task.impl_seconds))],
    ["Review", average(tasks.map((task) => task.review_seconds))],
    ["Owner gate", average(tasks.map((task) => task.final_seconds))],
  ];
  const max = Math.max(1, ...stages.map(([, value]) => value));
  const container = document.getElementById("stage-bars");
  container.replaceChildren();
  stages.forEach(([label, value]) => {
    const row = document.createElement("div");
    row.className = "stage-bar";
    const name = document.createElement("span");
    name.className = "stage-bar-label";
    name.textContent = label;
    const track = document.createElement("div");
    track.className = "stage-bar-track";
    const fill = document.createElement("div");
    fill.className = "stage-bar-fill";
    fill.style.width = `${Math.max(3, (value / max) * 100)}%`;
    track.append(fill);
    const out = document.createElement("span");
    out.className = "stage-bar-value";
    out.textContent = duration(value);
    row.append(name, track, out);
    container.append(row);
  });
}

const fallbackCrew = [
  { initials: "OW", role: "Owner / Dispatcher", model: "final gate", status: "waiting" },
  { initials: "M3", role: "Implementer", model: "worker agent", status: "working" },
  { initials: "RV", role: "Reviewer", model: "review agent", status: "reviewing" },
];

function renderCrew(members) {
  const safe = Array.isArray(members) && members.length ? members : fallbackCrew;
  document.getElementById("crew-count").textContent = `${safe.length} roles`;
  const container = document.getElementById("crew-list");
  container.replaceChildren();
  safe.forEach((member) => {
    const row = document.createElement("div");
    row.className = "crew-member";
    const identity = document.createElement("div");
    identity.className = "crew-identity";
    const avatar = document.createElement("span");
    avatar.className = "crew-avatar";
    avatar.textContent = member.initials || "—";
    const copy = document.createElement("span");
    copy.className = "crew-copy";
    const role = document.createElement("strong");
    role.textContent = member.role || "Agent";
    const model = document.createElement("small");
    model.textContent = member.model || "unassigned";
    copy.append(role, model);
    identity.append(avatar, copy);
    const status = document.createElement("span");
    status.className = "crew-state";
    status.textContent = member.label || member.status || "offline";
    row.append(identity, status);
    container.append(row);
  });
}

function renderActivity(events) {
  const safe = Array.isArray(events) ? events : [];
  const feed = document.getElementById("activity-feed");
  feed.replaceChildren();
  safe.forEach((event) => {
    const item = document.createElement("li");
    item.className = "activity-item";
    const time = document.createElement("time");
    time.textContent = event.time || "--:--";
    const copy = document.createElement("p");
    const task = document.createElement("code");
    task.textContent = `#${event.task ?? "—"}`;
    copy.append(task, ` ${event.text || "Pipeline event"}`);
    item.append(time, copy);
    feed.append(item);
  });
}

function syncWorkspaceHeight() {
  const workspace = document.querySelector(".workspace");
  const route = document.querySelector(".route-board");
  if (!workspace || !route || !matchMedia("(min-width: 1181px)").matches) {
    workspace?.style.removeProperty("--route-board-height");
    return;
  }
  workspace.style.setProperty("--route-board-height", `${Math.ceil(route.getBoundingClientRect().height)}px`);
}

function render(raw, label = "external data") {
  const data = adapt(raw);
  renderStations(data.tasks);
  renderSummary(data.summary, data.tasks);
  renderBars(data.tasks);
  renderCrew(data.crew);
  renderActivity(data.activity);
  document.getElementById("data-source").textContent = label;
  requestAnimationFrame(syncWorkspaceHeight);
  window.dispatchEvent(new CustomEvent("pipeline-bus:rendered"));
  return data;
}

async function load(url, label = url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return render(await response.json(), label);
}

window.pipelineBus = { render, load, adapt };
window.addEventListener("pipeline-bus:data", (event) => render(event.detail, "live event"));
window.addEventListener("resize", syncWorkspaceHeight, { passive: true });
new ResizeObserver(syncWorkspaceHeight).observe(document.querySelector(".route-board"));

const input = document.getElementById("analytics-file");
document.getElementById("load-json-button").addEventListener("click", () => input.click());
input.addEventListener("change", async () => {
  const [file] = input.files;
  if (!file) return;
  try { render(JSON.parse(await file.text()), file.name); }
  catch (error) { window.alert(`Could not load pipeline JSON: ${error.message}`); }
  finally { input.value = ""; }
});

function updateClock() {
  const now = new Date();
  const text = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
  document.getElementById("jst-clock").textContent = `${text} JST`;
}
updateClock();
setInterval(updateClock, 1000);

const canvas = document.getElementById("dispatch-canvas");
const ctx = canvas.getContext("2d");
ctx.imageSmoothingEnabled = false;
let animation = 0;

function rect(x, y, w, h, color) { ctx.fillStyle = color; ctx.fillRect(x * 4, y * 4, w * 4, h * 4); }
function drawClawd(x, y, inWindow = false) {
  const ink = "#202321", shell = "#df8a6c", light = "#f4efe4";
  rect(x + 2, y, 5, 2, shell); rect(x, y + 2, 9, 7, shell); rect(x + 1, y + 3, 7, 4, light);
  rect(x + 2, y + 4, 1, 1, ink); rect(x + 6, y + 4, 1, 1, ink); rect(x + 4, y + 6, 1, 1, ink);
  if (!inWindow) { rect(x + 1, y + 9, 2, 3, shell); rect(x + 6, y + 9, 2, 3, shell); }
}
function drawBus(x, doorOpen, passenger) {
  const ink = "#202321", salmon = "#df8a6c", dark = "#bc6650", glass = "#d8e8e5", paper = "#fbf8f0";
  rect(x, 19, 44, 27, ink); rect(x + 1, 20, 42, 24, salmon); rect(x + 3, 23, 27, 9, glass);
  for (let i = 0; i < 4; i += 1) rect(x + 4 + i * 7, 24, 5, 7, paper);
  rect(x + 32, 23, 8, 18, doorOpen ? paper : dark); rect(x + 33, 24, 3, 16, glass); rect(x + 37, 24, 2, 16, glass);
  rect(x + 4, 36, 26, 4, dark); rect(x + 2, 44, 40, 2, ink); rect(x + 7, 45, 7, 4, ink); rect(x + 31, 45, 7, 4, ink);
  if (passenger) drawClawd(x + 20, 24, true);
}
function drawScene(progress) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  rect(0, 50, 96, 2, "#202321");
  rect(6, 12, 18, 2, "#c9c1b0"); rect(6, 15, 12, 1, "#c9c1b0");
  let busX = -46, clawdX = 40, door = false, passenger = false;
  if (progress < .24) busX = -46 + (progress / .24) * 60;
  else if (progress < .43) { busX = 14; door = true; }
  else if (progress < .64) { busX = 14; door = true; clawdX = 40 + ((progress - .43) / .21) * 10; }
  else if (progress < .76) { busX = 14; passenger = true; }
  else { busX = 14 + ((progress - .76) / .24) * 95; passenger = true; }
  drawBus(busX, door, passenger);
  if (progress < .64) drawClawd(clawdX, 35);
}
function playBus() {
  cancelAnimationFrame(animation);
  const start = performance.now();
  document.getElementById("dispatch-copy").textContent = "Doors open. Clawd is boarding now.";
  const frame = (now) => {
    const progress = Math.min(1, (now - start) / 5200);
    drawScene(progress);
    if (progress < 1) animation = requestAnimationFrame(frame);
    else document.getElementById("dispatch-copy").textContent = "The task has departed for Implementation.";
  };
  animation = requestAnimationFrame(frame);
}
document.getElementById("replay-dispatch").addEventListener("click", playBus);
drawScene(.7);
if (!matchMedia("(prefers-reduced-motion: reduce)").matches) setTimeout(playBus, 650);

const params = new URLSearchParams(location.search);
const requested = params.get("data");
const candidates = requested
  ? [[requested, requested]]
  : [["./data/dashboard.json", "dashboard.json"], ["./data/analytics.json", "analytics.json"], ["./data/visual-qa.json", "synthetic stress data"]];
(async () => {
  for (const [url, label] of candidates) {
    try { await load(url, label); return; }
    catch { /* optional source may be absent */ }
  }
  render({ tasks: [], summary: {}, activity: [], crew: [] }, "empty checkout");
})();
