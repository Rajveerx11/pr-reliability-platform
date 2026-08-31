"use strict";

const state = { token: "", offset: 0, limit: 20, total: 0 };
const byId = (id) => document.getElementById(id);

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function authHeaders() {
  return { Authorization: `Bearer ${state.token}` };
}

async function getJson(path, authenticated = true) {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    headers: authenticated ? authHeaders() : {}
  });
  let body;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const error = new Error(body?.detail || body?.status || `Request failed (${response.status})`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function setNotice(message, kind = "") {
  const notice = byId("notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`.trim();
}

function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined) return "Unknown";
  if (milliseconds < 1000) return `${milliseconds} ms`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function formatMoney(micros) {
  return micros === null ? "Unknown" : `$${(micros / 1_000_000).toFixed(4)}`;
}

function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(value));
}

function humanState(value) {
  return value.split("_").map((word) => word[0].toUpperCase() + word.slice(1)).join(" ");
}

function renderOverview(data) {
  byId("metric-total").textContent = data.total_runs;
  byId("metric-active").textContent = `${data.active_runs} active now`;
  byId("metric-pending").textContent = data.pending_findings;
  byId("metric-awaiting").textContent = `${data.awaiting_approval_runs} runs await approval`;
  byId("metric-p95").textContent = formatDuration(data.p95_duration_ms);
  byId("metric-p50").textContent = `p50 ${formatDuration(data.p50_duration_ms)}`;
  byId("metric-published").textContent = data.published_runs;
  byId("metric-failed").textContent = `${data.failed_runs} failed runs`;
  const retries = byId("metric-retries");
  retries.textContent = data.activity_retry_count ?? "Unknown";
  retries.classList.toggle("word-value", data.activity_retry_count === null);
  byId("metric-retries-note").textContent = data.activity_retry_count === null
    ? "Not persisted per run yet"
    : "Retried activity attempts";
  byId("usage-complete").textContent = data.usage_complete_runs;
  byId("usage-partial").textContent = data.usage_partial_runs;
  byId("usage-unknown").textContent = data.usage_unknown_runs;
  byId("known-cost").textContent = formatMoney(data.exact_known_cost_usd_micros);
  const coverage = data.total_runs === 0 ? 0 : Math.round((data.usage_complete_runs / data.total_runs) * 100);
  byId("coverage-number").textContent = `${coverage}%`;
}

function statusPill(value) {
  return node("span", humanState(value), `state-pill ${value}`);
}

function renderRuns(page) {
  state.total = page.total;
  const rows = byId("run-rows");
  rows.replaceChildren();
  if (!page.items.length) {
    const row = node("tr");
    const cell = node("td", "No runs match these filters.", "empty-cell");
    cell.colSpan = 6;
    row.append(cell);
    rows.append(row);
  }
  for (const run of page.items) {
    const row = node("tr");
    const identity = node("td");
    identity.append(
      node("span", `${run.repository_full_name} #${run.pull_request_number}`, "repo-name"),
      node("span", `${run.head_sha.slice(0, 9)} · generation ${run.generation}`, "commit")
    );
    const statusCell = node("td");
    statusCell.append(statusPill(run.state));
    const findings = node("td", `${run.finding_count} · ${run.pending_finding_count} pending`, "secondary");
    const retries = node("td", run.retry_count ?? "Unknown", "secondary");
    const duration = node("td", formatDuration(run.duration_ms), "secondary");
    const actionCell = node("td");
    const open = node("button", "Inspect", "open-run");
    open.type = "button";
    open.setAttribute("aria-label", `Inspect run for ${run.repository_full_name} pull request ${run.pull_request_number}`);
    open.addEventListener("click", () => openRun(run.run_id));
    actionCell.append(open);
    row.append(identity, statusCell, findings, retries, duration, actionCell);
    rows.append(row);
  }
  byId("run-count").textContent = `${page.total} run${page.total === 1 ? "" : "s"}`;
  const pageNumber = Math.floor(state.offset / state.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / state.limit));
  byId("page-status").textContent = `Page ${pageNumber} of ${pageCount}`;
  byId("previous-page").disabled = state.offset === 0;
  byId("next-page").disabled = state.offset + state.limit >= page.total;
}

async function loadRuns() {
  const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
  const status = byId("status-filter").value;
  const repository = byId("repository-filter").value.trim();
  if (status) params.set("status", status);
  if (repository) params.set("repository", repository);
  renderRuns(await getJson(`/api/dashboard/runs?${params}`));
}

function renderHealth(body, ok) {
  byId("health-api").textContent = "Responding";
  byId("health-database").textContent = humanState(body?.dependencies?.database || "unknown");
  byId("health-workflow").textContent = humanState(body?.dependencies?.workflow || "unknown");
  const badge = byId("health-overall");
  badge.textContent = ok ? "Ready" : "Needs attention";
  badge.className = `status-dot ${ok ? "ready" : "not-ready"}`;
}

async function loadHealth() {
  try {
    const body = await getJson("/health/ready", false);
    renderHealth(body, true);
  } catch (error) {
    renderHealth(error.body, false);
  }
}

function fact(label, value) {
  const item = node("div", undefined, "detail-fact");
  item.append(node("span", label), node("strong", value));
  return item;
}

function renderRunDetail(detail) {
  const run = detail.run;
  byId("run-dialog-title").textContent = `${run.repository_full_name} #${run.pull_request_number}`;
  byId("run-dialog-subtitle").textContent = `${humanState(run.state)} · ${formatTime(run.created_at)}`;
  const root = byId("run-detail");
  const facts = node("div", undefined, "detail-facts");
  facts.append(
    fact("Commit", run.head_sha),
    fact("Trace ID", detail.trace_id || "Not recorded"),
    fact("Duration", formatDuration(run.duration_ms)),
    fact("Retries", run.retry_count ?? "Unknown")
  );

  const timelineSection = node("section", undefined, "detail-section");
  timelineSection.append(node("h3", "Run timeline"));
  const timeline = node("ol", undefined, "timeline");
  if (!detail.events.length) timeline.append(node("li", "No persisted events."));
  for (const event of detail.events) {
    const item = node("li");
    item.append(node("strong", event.summary), node("time", formatTime(event.occurred_at)));
    timeline.append(item);
  }
  timelineSection.append(timeline);

  const stagesSection = node("section", undefined, "detail-section");
  stagesSection.append(node("h3", "API and worker stages"));
  const stages = node("div", undefined, "stage-list");
  for (const stage of detail.stages) {
    const item = node("div", undefined, `stage-item ${stage.status}`);
    item.append(node("strong", humanState(stage.name)), node("span", humanState(stage.status)));
    stages.append(item);
  }
  stagesSection.append(stages);

  const findingsSection = node("section", undefined, "detail-section");
  findingsSection.append(node("h3", `Findings (${detail.findings.length})`));
  if (!detail.findings.length) findingsSection.append(node("p", "No findings recorded for this run."));
  for (const finding of detail.findings) {
    const card = node("article", undefined, "finding-card");
    const meta = node("div", undefined, "finding-meta");
    meta.append(
      node("span", humanState(finding.severity), `state-pill ${finding.severity}`),
      node("span", finding.category, "state-pill"),
      node("span", humanState(finding.approval_status), "state-pill")
    );
    card.append(meta, node("p", finding.claim));
    for (const evidence of finding.evidence) {
      const location = evidence.file_path ? ` · ${evidence.file_path}${evidence.start_line ? `:${evidence.start_line}` : ""}` : "";
      card.append(node("div", `${evidence.summary}${location}`, "evidence-line"));
    }
    findingsSection.append(card);
  }
  root.replaceChildren(facts, stagesSection, timelineSection, findingsSection);
}

async function openRun(runId) {
  const dialog = byId("run-dialog");
  byId("run-detail").replaceChildren(node("p", "Loading run evidence…"));
  dialog.showModal();
  try {
    renderRunDetail(await getJson(`/api/dashboard/runs/${encodeURIComponent(runId)}`));
  } catch (error) {
    byId("run-detail").replaceChildren(node("p", error.message));
  }
}

async function loadDashboard() {
  if (!state.token) return;
  const connect = byId("connect");
  connect.disabled = true;
  connect.textContent = "Loading…";
  byId("metric-grid").classList.add("loading");
  byId("metric-grid").setAttribute("aria-busy", "true");
  try {
    const [overview] = await Promise.all([getJson("/api/dashboard/overview"), loadRuns(), loadHealth()]);
    renderOverview(overview);
    byId("refresh").disabled = false;
    setNotice("Dashboard current. Private data loaded for this tab only.", "success");
  } catch (error) {
    if (error.status === 401) state.token = "";
    setNotice(error.message, "error");
  } finally {
    connect.disabled = false;
    connect.textContent = "Connect";
    byId("metric-grid").classList.remove("loading");
    byId("metric-grid").setAttribute("aria-busy", "false");
  }
}

byId("access-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.token = byId("token").value;
  state.offset = 0;
  loadDashboard();
});
byId("refresh").addEventListener("click", loadDashboard);
byId("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  state.offset = 0;
  loadRuns().catch((error) => setNotice(error.message, "error"));
});
byId("previous-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadRuns().catch((error) => setNotice(error.message, "error"));
});
byId("next-page").addEventListener("click", () => {
  state.offset += state.limit;
  loadRuns().catch((error) => setNotice(error.message, "error"));
});
byId("close-dialog").addEventListener("click", () => byId("run-dialog").close());
byId("run-dialog").addEventListener("click", (event) => {
  if (event.target === byId("run-dialog")) byId("run-dialog").close();
});
