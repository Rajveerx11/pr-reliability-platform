// Run through the installed Playwright skill runner against a local source-assets server.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const base = process.env.OPERATIONS_TEST_URL || "http://127.0.0.1:8763";
const output = process.env.OPERATIONS_TEST_OUTPUT || path.join(require("node:os").tmpdir(), "issue43-browser");

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true});
  let assertions = 0;
  try {
    for (const width of [1280, 390]) {
      const context = await browser.newContext({viewport: {width, height: 900}});
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      let apiStatus = 200;
      let drained = false;
      let mutationHeaders = null;
      const data = {queue_depth: 2, current_wait_seconds: 12, p50_wait_seconds: null,
        p95_wait_seconds: null, active_workers: 1, active_capacity: 4, active_slots: 2,
        utilization: .5, job_pass_rate: .75, pass_rate_samples: 4, wait_samples: 0,
        unknown_wait_runs: 5, queued: 1, assigned: 1, running: 1, awaiting_approval: 1,
        cancelled: 2, completed: 4, runners: [{runner_id: "review-1", version: "0.1.0",
          workload: "review", state: "busy", active: 2, capacity: 4}]};
      await page.route("**/auth/logout-token", route => route.fulfill({json: {csrf_token: "test-only"}}));
      await page.route("**/auth/session", route => route.fulfill({json: {login: "operator", role: "admin", csrf_token: "test-only"}}));
      await page.route("**/api/operations/overview", route => route.fulfill({status: apiStatus,
        json: apiStatus === 200 ? {...data, runners: data.runners.map(r => ({...r, state: drained ? "draining" : "busy"}))} : {detail: "unavailable"}}));
      await page.route("**/api/operations/runners/review-1/drain", route => {
        mutationHeaders = route.request().headers();
        assert.equal(route.request().method(), "POST");
        drained = true;
        return route.fulfill({status: 202, json: {drain_requested: true}});
      });
      await page.goto(`${base}/operations`);
      await page.getByRole("button", {name: "Drain review-1"}).waitFor();
      assert.equal(await page.locator("#summary dd").count(), 18); assertions++;
      assert.equal(await page.getByText("Unknown", {exact: true}).count(), 2); assertions++;
      await page.getByRole("button", {name: "Drain review-1"}).click();
      await page.getByText(/review-1 · 0.1.0 · review · draining/).waitFor();
      assert.equal(mutationHeaders["x-csrf-token"], "test-only"); assertions++;
      assert.equal(await page.getByRole("button", {name: "Drain review-1"}).isDisabled(), true); assertions++;
      await page.screenshot({path: path.join(output, `operations-${width}.png`), fullPage: true});
      apiStatus = 503;
      await page.getByRole("button", {name: "Refresh operations"}).click();
      await page.getByRole("alert").waitFor();
      assert.equal(await page.locator("#runners").textContent(), ""); assertions++;
      apiStatus = 403;
      await page.getByRole("button", {name: "Refresh operations"}).click();
      await page.getByRole("alert").filter({hasText: "Administrator access required."}).waitFor();
      assert.equal(await page.locator("#summary dd").count(), 0); assertions++;
      await page.route("**/auth/session", route => route.fulfill({status: 401, json: {detail: "signed out"}}));
      await page.reload();
      await page.getByRole("link", {name: "Sign in with GitHub"}).waitFor();
      assert.equal(await page.locator("#runners").textContent(), ""); assertions++;
      assert.deepEqual(errors, []); assertions++;
      await context.close();
    }
    console.log(`Operations browser: ${assertions} assertions passed (desktop/mobile, drain, 503, 403, signed out).`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
