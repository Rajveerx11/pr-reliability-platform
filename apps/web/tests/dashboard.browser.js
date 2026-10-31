// Copy to a temp path and run through the installed Playwright skill runner.
// DASHBOARD_TEST_ROOT points to the checkout; the server serves only source UI assets.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const root = process.env.DASHBOARD_TEST_ROOT;
const output = process.env.DASHBOARD_TEST_OUTPUT || path.join(require("node:os").tmpdir(), "pr62-dashboard");
const assets = {
  "/dashboard": ["dashboard.html", "text/html"],
  "/dashboard/assets/dashboard.css": ["dashboard.css", "text/css"],
  "/dashboard/assets/dashboard.js": ["dashboard.js", "text/javascript"],
  "/auth/assets/auth.js": ["auth.js", "text/javascript"]
};

(async () => {
  assert.ok(root, "Set DASHBOARD_TEST_ROOT to the checkout path");
  fs.mkdirSync(output, {recursive: true});
  const server = http.createServer((request, response) => {
    const asset = assets[new URL(request.url, "http://localhost").pathname];
    if (!asset) { response.writeHead(404).end(); return; }
    response.writeHead(200, {"Content-Type": asset[1]});
    response.end(fs.readFileSync(path.join(root, "apps/web", asset[0])));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  let browser;
  let assertions = 0;
  try {
    browser = await chromium.launch({headless: true});
    for (const width of [1280, 390]) {
      const context = await browser.newContext({viewport: {width, height: 900}});
      const page = await context.newPage();
      page.setDefaultTimeout(10000);
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      const run = {run_id: "run-test", repository_full_name: "test/repo", pull_request_number: 62,
        state: "awaiting_approval", created_at: "2026-10-08T14:00:00Z", head_sha: "a".repeat(40),
        generation: 1, finding_count: 3, pending_finding_count: 3, retry_count: 0, duration_ms: 1234};
      const findings = Array.from({length: 3}, (_, i) => ({severity: "minor", category: "correctness",
        approval_status: "pending", claim: `Finding ${i + 1} <img src=x onerror=alert(1)>`,
        evidence: [{summary: "Source evidence", file_path: "test.py", start_line: i + 1}]}));
      const detail = {run, trace_id: "test-trace", events: [], stages: [], findings};
      const references = ["a", "b", "c"].map(value => `ev_${value.repeat(64)}`);
      const items = references.map((reference, i) => ({reference, check_name: `check-${i + 1}`, expired: i === 2}));
      items.push({reference: "javascript:alert(1)", check_name: "invalid", expired: false});
      let evidenceUnavailable = false;
      let decryptRequests = 0;
      await page.route("**/auth/logout-token", route => route.fulfill({json: {csrf_token: "test-only"}}));
      await page.route("**/auth/session", route => route.fulfill({json: {login: "reviewer", role: "reviewer", csrf_token: "test-only"}}));
      await page.route("**/health/ready", route => route.fulfill({json: {dependencies: {database: "ready", workflow: "ready"}}}));
      await page.route("**/api/dashboard/overview", route => route.fulfill({json: {total_runs: 1,
        active_runs: 1, pending_findings: 3, awaiting_approval_runs: 1, p95_duration_ms: 1234,
        p50_duration_ms: 1234, published_runs: 0, failed_runs: 0, activity_retry_count: 0,
        usage_complete_runs: 0, usage_partial_runs: 0, usage_unknown_runs: 1, exact_known_cost_usd_micros: null}}));
      await page.route("**/api/dashboard/runs?*", route => route.fulfill({json: {total: 1, items: [run]}}));
      await page.route("**/api/dashboard/runs/run-test", route => route.fulfill({json: detail}));
      await page.route("**/api/evidence/runs/run-test", route => route.fulfill({status: evidenceUnavailable ? 503 : 200,
        json: evidenceUnavailable ? {detail: "unavailable"} : {items}}));
      await page.route("**/api/evidence/ev_*", route => {
        decryptRequests++;
        return route.fulfill({json: {stdout: "<script>untrusted log</script>", summary: "redacted"}});
      });
      await page.goto(`${base}/dashboard`);
      const inspect = page.getByRole("button", {name: "Inspect run for test/repo pull request 62"});
      await inspect.click();
      const pointers = page.getByRole("link", {name: "View run verification checks"});
      await pointers.first().waitFor();
      assert.equal(await page.locator(".finding-card").count(), 3); assertions++;
      assert.equal(await page.locator(".verification-evidence").count(), 1); assertions++;
      assert.equal(await page.getByRole("button", {name: /summary and logs/}).count(), 2); assertions++;
      assert.equal(await page.getByRole("link", {name: "Download redacted evidence"}).count(), 2); assertions++;
      assert.equal(await page.locator(".verification-evidence pre").count(), 2); assertions++;
      assert.equal(await page.getByText("check-3: evidence expired", {exact: true}).count(), 1); assertions++;
      assert.equal(await page.locator(".finding-card img").count(), 0); assertions++;
      assert.equal(await pointers.count(), 3); assertions++;
      for (let i = 0; i < 3; i++) {
        assert.equal(await pointers.nth(i).getAttribute("href"), "#run-verification-evidence"); assertions++;
        await pointers.nth(i).focus();
        await page.keyboard.press("Enter");
        await page.waitForFunction(() => document.activeElement?.id === "run-verification-evidence");
        assert.ok(await page.locator("#run-verification-evidence").isVisible()); assertions++;
      }
      // Tab from the target section goes to its one shared set of controls.
      await page.keyboard.press("Tab");
      assert.equal(await page.evaluate(() => document.activeElement.textContent), "View check-1 summary and logs"); assertions++;
      assert.equal(decryptRequests, 0); assertions++;
      await page.keyboard.press("Enter");
      await page.locator(".verification-evidence pre").first().filter({hasText: "untrusted log"}).waitFor();
      assert.equal(decryptRequests, 1); assertions++;
      assert.equal(await page.locator(".verification-evidence pre script").count(), 0); assertions++;
      assert.equal(await page.getByRole("link", {name: "Download redacted evidence"}).first().getAttribute("href"),
        `/api/evidence/${references[0]}?download=true`); assertions++;
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true); assertions++;
      assert.equal(await page.locator("#run-dialog").evaluate(element => element.scrollWidth <= element.clientWidth), true); assertions++;
      await page.screenshot({path: path.join(output, `dashboard-evidence-${width}.png`), fullPage: true});
      await page.getByRole("button", {name: "Close run detail", exact: true}).click();
      evidenceUnavailable = true;
      await inspect.click();
      await page.getByText("Verification evidence unavailable: unavailable", {exact: true}).waitFor();
      assert.equal(await page.getByText("Verification evidence unavailable: unavailable", {exact: true}).count(), 1); assertions++;
      assert.equal(await pointers.count(), 3); assertions++;
      assert.equal(await page.locator(".verification-evidence").count(), 1); assertions++;
      assert.equal(await page.getByRole("button", {name: /summary and logs/}).count(), 0); assertions++;
      assert.deepEqual(errors, []); assertions++;
      await context.close();
    }
    console.log(`Dashboard browser: ${assertions} assertions passed (desktop/mobile, three findings, shared evidence, keyboard anchors, safe text, expired evidence, single error).`);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
