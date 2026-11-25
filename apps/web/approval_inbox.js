"use strict";

const byId = (id) => document.getElementById(id);
const findings = byId("findings");
const refresh = byId("load");
const pendingCount = byId("pending-count");
const queueState = byId("inbox-state");

let signedIn = false;
let loading = false;
let decisionsInFlight = 0;
let accessGeneration = 0;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function money(micros) {
  return micros === null ? "Unknown" : `$${(micros / 1_000_000).toFixed(4)}`;
}

function addFact(list, term, value, isCode = false) {
  const fact = node("div");
  const description = node("dd");
  description.append(isCode ? node("code", value) : value);
  fact.append(node("dt", term), description);
  list.append(fact);
}

function showState(title, message, {error = false, link = false} = {}) {
  byId("state-title").textContent = title;
  byId("state-copy").textContent = message;
  byId("state-link").hidden = !link;
  queueState.classList.toggle("error", error);
  queueState.hidden = false;
}

function updateRefresh() {
  refresh.hidden = !signedIn;
  refresh.disabled = !signedIn || loading || decisionsInFlight > 0;
}

function updatePendingCount() {
  const count = findings.querySelectorAll(".status.pending").length;
  pendingCount.textContent = `${count} pending`;
  pendingCount.hidden = false;
}

function updateStatus(badge, status) {
  badge.textContent = status[0].toUpperCase() + status.slice(1);
  badge.className = `pill state-pill status ${status}`;
}

async function readResponse(response, fallback) {
  if (response.ok) return response.json();
  if (response.status === 401 || response.status === 403) {
    document.dispatchEvent(new Event("reviewer:signed-out"));
  }
  const body = await response.json().catch(() => null);
  throw new Error(typeof body?.detail === "string" ? body.detail : fallback);
}

function renderEvidence(entry) {
  const evidence = node("div", undefined, "inbox-evidence");
  evidence.append(node("strong", `Unverified model claim: ${entry.kind}`), node("p", entry.summary));
  if (entry.file_path) {
    const location = node("p");
    location.append(node("code", `${entry.file_path}${entry.start_line ? `:${entry.start_line}` : ""}`));
    evidence.append(location);
  }
  if (entry.command) {
    const command = node("p");
    command.append(
      "Claimed command: ",
      node("code", entry.command.join(" ")),
      ` (claimed exit ${entry.exit_code}; not executed by platform)`
    );
    evidence.append(command);
  }
  return evidence;
}

function renderDecision(item, badge) {
  const decision = node("div");
  const controls = node("fieldset", undefined, "inbox-decision");
  const label = node("label");
  const reason = node("textarea");
  reason.rows = 2;
  label.append("Decision reason (optional)", reason);
  const actions = node("div", undefined, "inbox-actions");
  const approveButton = node("button", "Approve finding", "button primary");
  const rejectButton = node("button", "Reject finding", "button reject");
  approveButton.type = rejectButton.type = "button";
  const receipt = node("p", undefined, "inbox-receipt");
  receipt.setAttribute("role", "status");

  async function record(choice) {
    controls.disabled = true;
    decisionsInFlight++;
    updateRefresh();
    receipt.classList.remove("error");
    receipt.textContent = "Recording decision…";
    try {
      const response = await reviewerSession.mutate(
        `/api/approval-inbox/${encodeURIComponent(item.finding_id)}/decision`,
        {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            schema_version: "1",
            head_sha: item.head_sha,
            decision: choice,
            reason: reason.value.trim() || null
          })
        }
      );
      const result = await readResponse(response, "Couldn’t record the decision. Try again.");
      if (!decision.isConnected) return;
      updateStatus(badge, result.decision);
      updatePendingCount();
      receipt.textContent = "Decision recorded. Publishing happens separately.";
    } catch (error) {
      if (decision.isConnected) {
        controls.disabled = false;
        receipt.classList.add("error");
        receipt.textContent = error.message;
      }
    } finally {
      decisionsInFlight--;
      updateRefresh();
    }
  }

  approveButton.addEventListener("click", () => record("approved"));
  rejectButton.addEventListener("click", () => record("rejected"));
  actions.append(approveButton, rejectButton);
  controls.append(node("legend", "Record a finding decision", "sr-only"), label, actions);
  decision.append(controls, receipt);
  return decision;
}

function renderFinding(item) {
  const card = node("article", undefined, "inbox-finding");
  const heading = node("header", undefined, "finding-heading");
  const badge = node("span");
  updateStatus(badge, item.status);
  heading.append(node("h3", `${item.repository_full_name} #${item.pull_request_number}`), badge);
  card.append(heading, node("p", "Unverified model claim", "claim-label eyebrow"), node("p", item.claim, "claim"));

  const facts = node("dl", undefined, "inbox-facts");
  addFact(facts, "Commit", item.head_sha, true);
  addFact(facts, "Reported cost", money(item.cost_usd_micros));
  addFact(facts, "Cost budget", money(item.cost_budget_usd_micros));
  card.append(facts, ...item.evidence.map(renderEvidence));

  if (item.run_id) {
    const review = node("a", "View run and sandbox checks", "inbox-run-link");
    review.href = `/dashboard?run=${encodeURIComponent(item.run_id)}`;
    card.append(review);
  }
  if (item.status === "pending") card.append(renderDecision(item, badge));
  return card;
}

async function load() {
  const generation = accessGeneration;
  loading = true;
  updateRefresh();
  pendingCount.hidden = true;
  findings.replaceChildren();
  showState("Loading findings…", "Checking the approval queue.");
  try {
    const response = await fetch("/api/approval-inbox", {credentials: "same-origin", cache: "no-store"});
    const items = await readResponse(response, "Refresh findings to try again.");
    if (generation !== accessGeneration) return;
    findings.replaceChildren(...items.map(renderFinding));
    updatePendingCount();
    if (items.length) queueState.hidden = true;
    else showState("No findings await approval.", "New findings will appear here when a review needs your decision.", {link: true});
  } catch (error) {
    if (generation === accessGeneration) {
      showState("Couldn’t load findings.", error.message, {error: true});
    }
  } finally {
    loading = false;
    updateRefresh();
  }
}

document.addEventListener("reviewer:signed-out", () => {
  const sessionEnded = signedIn;
  signedIn = false;
  accessGeneration++;
  findings.replaceChildren();
  pendingCount.hidden = true;
  byId("reviewer-name").hidden = true;
  updateRefresh();
  showState("Sign in to review findings", sessionEnded
    ? "Your session ended. Sign in with GitHub to reopen the approval queue."
    : "The inbox shows findings from repositories you can access.");
});

refresh.addEventListener("click", load);
reviewerSession.start(() => {
  signedIn = true;
  byId("reviewer-name").hidden = false;
  return load();
}, (error) => {
  if (error.status !== 401 && error.status !== 403) {
    showState("Couldn’t finish the request.", error.message, {error: true});
  }
});
