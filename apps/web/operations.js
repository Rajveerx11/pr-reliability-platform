"use strict";

const summary = document.getElementById("summary");
const runners = document.getElementById("runners");
const error = document.getElementById("error");
const refresh = document.getElementById("refresh");
let generation = 0;
function fail(message) {
  generation++;
  summary.replaceChildren(); runners.replaceChildren();
  error.textContent = message; error.hidden = false;
  refresh.disabled = false;
}
function node(tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
}
function display(value, suffix = "") {
  return value === null || value === undefined ? "Unknown" : `${value}${suffix}`;
}
async function load() {
  const current = ++generation;
  refresh.disabled = true;
  try {
    const response = await fetch("/api/operations/overview", {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error(response.status === 403 ? "Administrator access required." : "Operations unavailable. Sign in or retry.");
    const data = await response.json();
    if (current !== generation) return;
    summary.replaceChildren(); runners.replaceChildren(); error.hidden = true;
    const fields = {queue_observation_unknown: "Unobserved activity queues", queue_depth: "Queue depth", current_wait_seconds: "Current oldest wait (s)",
      p50_wait_seconds: "p50 wait (s)", p95_wait_seconds: "p95 wait (s)",
      active_workers: "Active review workers", active_capacity: "Active activity capacity",
      active_slots: "Busy activity slots", utilization: "Activity utilization (fraction)",
      job_pass_rate: "Verification pass rate (fraction)", pass_rate_samples: "Verification samples",
      wait_samples: "Wait samples", unknown_wait_runs: "Missing historical waits",
      queued: "Queued", assigned: "Assigned to Temporal", running: "Running review stage",
      awaiting_approval: "Awaiting approval", cancelled: "Cancelled", completed: "Completed"};
    for (const [key, label] of Object.entries(fields)) {
      summary.append(node("dt", label), node("dd", display(data[key])));
    }
    if (!data.runners.length) runners.append(node("p", "No runners recorded."));
    for (const runner of data.runners) {
      const row = node("p", `${runner.runner_id} · ${runner.version} · ${runner.workload} · ${runner.state} · ${display(runner.active)}/${display(runner.capacity)} slots · heartbeat ${display(runner.heartbeat_at)}`);
      const drain = node("button", `Drain ${runner.runner_id}`);
      drain.disabled = runner.state === "draining" || runner.state === "offline";
      drain.addEventListener("click", async () => {
        const started = generation;
        drain.disabled = true;
        try {
          const result = await window.reviewerSession.mutate(`/api/operations/runners/${encodeURIComponent(runner.runner_id)}/drain`, {method: "POST"});
          if (started !== generation) return;
          if (!result.ok) throw new Error("Drain request failed. Retry after refreshing.");
          await load();
        } catch (e) { if (started === generation) fail(e.message); }
      });
      row.append(drain); runners.append(row);
    }
  } catch (e) { if (current === generation) fail(e.message); }
  finally { if (current === generation) refresh.disabled = false; }
}
refresh.addEventListener("click", load);
document.addEventListener("reviewer:signed-out", () => fail("Sign in to view operations."));
window.reviewerSession.start(load, e => fail(e.message));
