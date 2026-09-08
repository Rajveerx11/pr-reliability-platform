const findings = document.querySelector("#findings");
      const notice = document.querySelector("#notice");

      function node(tag, text, className) {
        const element = document.createElement(tag);
        if (text !== undefined) element.textContent = text;
        if (className) element.className = className;
        return element;
      }

      function money(micros) {
        return micros === null ? "Unknown" : `$${(micros / 1_000_000).toFixed(4)}`;
      }

      function addFact(list, term, value) {
        list.append(node("dt", term), node("dd", value));
      }

      async function decide(item, decision, reason, card) {
        const response = await reviewerSession.mutate(`/api/approval-inbox/${item.finding_id}/decision`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            schema_version: "1",
            head_sha: item.head_sha,
            decision,
            reason: reason.value.trim() || null
          })
        });
        if (!response.ok) throw new Error((await response.json()).detail || "Decision failed");
        const receipt = await response.json();
        card.querySelectorAll("button, textarea").forEach((control) => control.disabled = true);
        card.querySelector(".status").textContent = receipt.decision;
        notice.textContent = `${receipt.decision} recorded for ${receipt.finding_id}. Nothing published.`;
      }

      function render(item) {
        const card = node("article");
        const meta = node("div", undefined, "meta");
        meta.append(
          node("span", `${item.repository_full_name} #${item.pull_request_number}`, "pill"),
          node("span", item.verification, "pill"),
          node("span", item.status, "pill status")
        );
        card.append(meta, node("p", item.claim, "claim"));

        const facts = node("dl");
        addFact(facts, "Commit", item.head_sha);
        addFact(facts, "Reported cost", money(item.cost_usd_micros));
        addFact(facts, "Cost budget", money(item.cost_budget_usd_micros));
        card.append(facts);

        item.evidence.forEach((entry) => {
          const evidence = node("div", undefined, "evidence");
          evidence.append(node("strong", entry.kind), node("p", entry.summary));
          if (entry.file_path) evidence.append(node("p", `${entry.file_path}${entry.start_line ? `:${entry.start_line}` : ""}`));
          if (entry.command) evidence.append(node("p", `${entry.command.join(" ")} (exit ${entry.exit_code})`));
          card.append(evidence);
        });

        const decision = node("div", undefined, "decision");
        const reason = node("textarea");
        reason.rows = 2;
        reason.placeholder = "Optional decision reason";
        reason.setAttribute("aria-label", "Decision reason");
        const actions = node("div", undefined, "actions");
        const approve = node("button", "Approve finding", "approve");
        const reject = node("button", "Reject finding", "reject");
        approve.type = reject.type = "button";
        approve.addEventListener("click", () => decide(item, "approved", reason, card).catch(showError));
        reject.addEventListener("click", () => decide(item, "rejected", reason, card).catch(showError));
        if (item.status !== "pending") approve.disabled = reject.disabled = reason.disabled = true;
        actions.append(approve, reject);
        decision.append(reason, actions);
        card.append(decision);
        return card;
      }

      function showError(error) { notice.textContent = error.message; }
      document.addEventListener("reviewer:signed-out", () => findings.replaceChildren());

      async function load() {
        notice.textContent = "Loading…";
        const response = await fetch("/api/approval-inbox", {
          credentials: "same-origin", cache: "no-store"
        });
        if (!response.ok) {
          findings.replaceChildren();
          if (response.status === 401 || response.status === 403) document.dispatchEvent(new Event("reviewer:signed-out"));
          throw new Error((await response.json()).detail || "Load failed");
        }
        const items = await response.json();
        findings.replaceChildren(...items.map(render));
        notice.textContent = items.length ? `${items.length} finding${items.length === 1 ? "" : "s"} ready.` : "No findings await approval.";
      }

      document.querySelector("#load").addEventListener("click", () => load().catch(showError));

reviewerSession.start(load, showError);
