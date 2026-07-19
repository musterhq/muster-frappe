(() => {
  const model = {
    scope(route, routeString) {
      const scope = {source: "desk-dock", scope_mode: "context", route: routeString || "/app"};
      if (Array.isArray(route) && typeof route[1] === "string" && route[1].trim()) {
        if (route[0] === "List") {
          Object.assign(scope, {page_type: "List", page_name: route[1], doctype: route[1]});
        } else if (route[0] === "Form") {
          Object.assign(scope, {page_type: "Form", page_name: route[1], doctype: route[1]});
          if (typeof route[2] === "string" && route[2].trim()) scope.docname = route[2];
        } else {
          Object.assign(scope, {page_type: String(route[0] || "Page"), page_name: route[1]});
        }
      }
      return scope;
    },
    terminal(status) { return status === "completed" || status === "failed"; },
    submitMethod(intent) {
      return intent === "workflow" ? "muster.api.mission.plan" : "muster.api.ask.submit";
    },
  };
  window.MusterAskDockModel = model;

  function boot() {
    if (!frappe.boot?.muster?.available || document.querySelector(".muster-dock")) return;
    const dock = document.createElement("aside");
    dock.className = "muster-dock is-collapsed";
    dock.setAttribute("aria-label", __("Muster assistant"));
    const connected = Boolean(frappe.boot.muster.execution_enabled);
    const canAdminister = Boolean(frappe.boot.muster.can_administer);
    dock.innerHTML = `<button class="muster-dock-toggle" aria-expanded="false"><img src="/assets/muster/images/muster-mark.png" alt=""/><span>${__("Ask Muster")}</span><b class="muster-dock-count">0</b></button><div class="muster-dock-body">
      <section class="muster-dock-compose" aria-label="${__("Ask Muster")}">
        <div class="muster-dock-compose-head"><strong>${__("Ask anything about your work")}</strong><span class="muster-runtime-state" data-connected="${connected}">${connected ? __("Trusted gateway connected") : __("Setup required")}</span></div>
        <div class="muster-intent-switch" role="group" aria-label="${__("Request type")}">
          <button class="btn btn-xs is-active" type="button" data-muster-intent="ask" aria-pressed="true">${__("Ask")}</button>
          <button class="btn btn-xs" type="button" data-muster-intent="workflow" aria-pressed="false">${__("Build workflow")}</button>
        </div>
        <div class="muster-chat-log" aria-live="polite"></div>
        <textarea class="form-control muster-dock-prompt" rows="3" placeholder="${__("Ask about this site, a record, a process, a report, or what to do next…")}"></textarea>
        <div class="muster-dock-compose-actions"><small>${__("This page is useful context, not a limit. Your live Frappe permissions remain authoritative.")}</small><button class="btn btn-primary btn-sm muster-dock-submit" type="button">${__("Ask Muster")} <span aria-hidden="true">→</span></button></div>
      </section>
      <header><strong>${__("Active work")}</strong><a href="/desk/muster-control">${__("Open control")}</a></header><div class="muster-dock-list"></div></div>`;
    document.body.appendChild(dock);
    const prompt = dock.querySelector(".muster-dock-prompt");
    const submit = dock.querySelector(".muster-dock-submit");
    const chat = dock.querySelector(".muster-chat-log");
    let intent = "ask";
    let conversationId = conversationKey();

    dock.querySelector(".muster-dock-toggle").addEventListener("click", () => {
      const collapsed = dock.classList.toggle("is-collapsed");
      dock.querySelector(".muster-dock-toggle").setAttribute("aria-expanded", String(!collapsed));
      if (!collapsed) window.setTimeout(() => prompt.focus(), 80);
    });
    dock.querySelector(".muster-intent-switch").addEventListener("click", (event) => {
      const button = event.target.closest("[data-muster-intent]");
      if (!button) return;
      intent = button.dataset.musterIntent;
      dock.querySelectorAll("[data-muster-intent]").forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      const workflow = intent === "workflow";
      submit.firstChild.textContent = workflow ? __("Create plan") + " " : __("Ask Muster") + " ";
      prompt.placeholder = workflow
        ? __("Describe the multi-step outcome. Muster will create an inert plan for your review; nothing runs yet.")
        : __("Ask about this site, a record, a process, a report, or what to do next…");
    });
    prompt.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submitPrompt();
      }
    });
    submit.addEventListener("click", submitPrompt);
    dock.querySelector(".muster-dock-list").addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-live-mission]");
      if (trigger) window.musterLiveSession?.open(trigger.dataset.liveMission);
    });

    function conversationKey() {
      const storageKey = `muster.ask.conversation.${frappe.session?.user || "user"}`;
      try {
        const existing = window.sessionStorage?.getItem(storageKey);
        if (existing) return existing;
        const created = `desk-${frappe.utils.get_random(32)}`;
        window.sessionStorage?.setItem(storageKey, created);
        return created;
      } catch (_error) {
        return `desk-${frappe.utils.get_random(32)}`;
      }
    }

    function currentScope() {
      const route = typeof frappe.get_route === "function" ? frappe.get_route() : [];
      const routeString = typeof frappe.get_route_str === "function" ? frappe.get_route_str() : "/app";
      return model.scope(route, routeString);
    }

    function appendMessage(kind, text, artifacts = []) {
      const item = document.createElement("article");
      item.className = `muster-chat-message is-${kind}`;
      const label = kind === "user" ? __("You") : __("Muster");
      item.innerHTML = `<small>${label}</small><div>${frappe.utils.escape_html(text || "").replace(/\n/g, "<br>")}</div>${artifacts.map((artifact) => `<a href="${frappe.utils.escape_html(artifact.download_url)}" target="_blank" rel="noopener">↧ ${frappe.utils.escape_html(artifact.name)}</a>`).join("")}`;
      chat.appendChild(item);
      chat.scrollTop = chat.scrollHeight;
      return item;
    }

    function appendHandoffs(turnId, handoffs) {
      if (!turnId || !Array.isArray(handoffs) || !handoffs.length) return;
      const item = document.createElement("article");
      item.className = "muster-chat-message is-handoff";
      item.innerHTML = `<small>${__("Optional next steps")}</small><div>${__("Nothing will run from Ask. Choose a reviewed next step only if you want to continue.")}</div><div class="muster-handoff-actions"></div>`;
      const actions = item.querySelector(".muster-handoff-actions");
      handoffs.forEach((handoff) => {
        if (!handoff || handoff.state !== "offered" || handoff.requires !== "explicit_confirmation") return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-xs";
        button.textContent = handoff.label || __("Prepare reviewed plan");
        button.addEventListener("click", () => {
          const accept = async (extra = {}) => {
            button.disabled = true;
            try {
              const response = await frappe.call({
                method: "muster.api.ask.accept_handoff",
                type: "POST",
                args: {
                  turn_id: turnId,
                  handoff_id: handoff.id,
                  confirmed: 1,
                  idempotency_key: frappe.utils.get_random(24),
                  ...extra,
                },
                freeze: false,
              });
              const proposal = response.message.proposal;
              const development = response.message.proposal_doctype === "Muster Development Proposal";
              const route = development ? "muster-development-proposal" : "muster-workflow-proposal";
              actions.innerHTML = `<a href="/desk/${route}/${encodeURIComponent(proposal)}">${development ? __("Review the inert development proposal") : __("Review the inert proposal")}</a>`;
            } catch (error) {
              button.disabled = false;
              throw error;
            }
          };
          if (handoff.kind === "development_workflow") {
            frappe.prompt([
              {fieldname: "development_app", fieldtype: "Link", options: "Muster Development App", label: __("Registered app"), reqd: 1},
              {fieldname: "policy", fieldtype: "Link", options: "Muster Policy", label: __("Policy"), reqd: 1},
            ], (values) => accept(values), __("Bind this proposal to reviewed source"), __("Create inert proposal"));
            return;
          }
          frappe.confirm(
            __("Create an inert proposal for review? This will not publish, start, open a browser, or change Frappe."),
            () => accept(),
          );
        });
        actions.appendChild(button);
      });
      chat.appendChild(item);
      chat.scrollTop = chat.scrollHeight;
    }

    async function pollAnswer(runId, answerItem) {
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const response = await frappe.call({
          method: "muster.api.ask.poll",
          type: "GET",
          args: {run_id: runId, wait_ms: 10000},
          freeze: false,
        });
        const state = response.message;
        if (state.status === "completed") {
          answerItem.remove();
          appendMessage("assistant", state.answer, state.artifacts || []);
          return;
        }
        if (state.status === "failed") {
          answerItem.remove();
          appendMessage("error", state.error || __("Muster could not complete this answer."));
          return;
        }
        answerItem.querySelector("div").textContent = state.partial_text || __("Thinking with your permitted site context…");
      }
      answerItem.querySelector("div").textContent = __("This is taking longer than expected. You can ask again safely.");
    }

    async function submitPrompt() {
      const text = prompt.value.trim();
      if (!text) {
        frappe.show_alert({message: __("Type a question or describe the workflow you want."), indicator: "orange"});
        prompt.focus();
        return;
      }
      if (!connected) {
        const action = canAdminister
          ? `<a href="/desk/muster-settings/Muster%20Settings">${__("Open Muster Settings")}</a>`
          : __("Ask a Muster administrator to connect the AI runtime.");
        frappe.msgprint({title: __("AI runtime is not connected"), indicator: "orange", message: `${__("This request was not queued because no trusted Muster gateway is active.")}<br>${action}`});
        return;
      }
      submit.disabled = true;
      const requestIntent = intent;
      let pendingAnswer;
      try {
        if (requestIntent === "workflow") {
          const response = await frappe.call({
            method: model.submitMethod(requestIntent),
            type: "POST",
            args: {objective: text, scope: JSON.stringify(currentScope()), idempotency_key: frappe.utils.get_random(24)},
            freeze: false,
          });
          prompt.value = "";
          const proposal = response.message.proposal;
          frappe.msgprint({
            title: __("Workflow proposal ready"),
            indicator: "blue",
            message: `${__("Muster created an inert plan for review. Nothing has executed.")}<br><a href="/desk/muster-workflow-proposal/${encodeURIComponent(proposal)}">${__("Review workflow proposal")}</a>`,
          });
          return;
        }
        appendMessage("user", text);
        prompt.value = "";
        const answerItem = appendMessage("assistant", __("Thinking with your permitted site context…"));
        pendingAnswer = answerItem;
        const response = await frappe.call({
          method: model.submitMethod(requestIntent),
          type: "POST",
          args: {
            prompt: text,
            conversation_id: conversationId,
            scope: JSON.stringify(currentScope()),
            idempotency_key: frappe.utils.get_random(24),
          },
          freeze: false,
        });
        if (response.message.status === "clarification") {
          answerItem.remove();
          appendMessage("assistant", response.message.reason);
          return;
        }
        if (response.message.status === "needs_read_plan") {
          answerItem.remove();
          appendMessage("assistant", response.message.reason);
          return;
        }
        appendHandoffs(response.message.turn_id, response.message.handoffs || []);
        await pollAnswer(response.message.run_id, answerItem);
      } catch (error) {
        if (requestIntent === "ask") {
          pendingAnswer?.remove();
          appendMessage("error", __("Muster could not answer that request. Nothing was changed."));
        }
        throw error;
      } finally {
        submit.disabled = false;
        prompt.focus();
      }
    }

    async function refresh() {
      const rows = await frappe.db.get_list("Muster Mission", {filters: {status: ["not in", ["Completed", "Failed", "Cancelled"]]}, fields: ["name", "objective", "status", "progress"], order_by: "modified desc", limit: 8});
      dock.querySelector(".muster-dock-count").textContent = rows.length;
      dock.querySelector(".muster-dock-list").innerHTML = rows.map((row) => `<div class="muster-dock-item"><button type="button" data-live-mission="${frappe.utils.escape_html(row.name)}" title="${frappe.utils.escape_html(row.objective)}"><strong>${frappe.utils.escape_html(row.objective)}</strong><small>${frappe.utils.escape_html(row.status)} · ${Math.round(row.progress || 0)}% · ${__("Watch live")}</small></button><a href="/desk/muster-mission/${encodeURIComponent(row.name)}" aria-label="${__("Open mission record")}">↗</a></div>`).join("") || `<p>${connected ? __("No active workflows") : __("Connect the AI runtime to start work")}</p>`;
    }
    frappe.realtime.on("muster_mission_changed", refresh);
    frappe.realtime.on("muster_activity", refresh);
    const poll = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh().catch(() => {});
    }, 10000);
    dock.addEventListener("remove", () => window.clearInterval(poll), {once: true});
    refresh().catch((error) => console.debug("Muster dock unavailable", error));
  }
  $(document).on("app_ready", boot);
})();
