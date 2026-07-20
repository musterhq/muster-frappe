(() => {
  "use strict";
  if (window.MusterSpaAssistantModel) return;

  const KNOWN_SPA = /^\/(?:crm|helpdesk)(?:\/|$)/;
  const SYSTEM_ROUTE = /^\/(?:app|desk|api|assets|files|private|login|logout)(?:\/|$)/;
  const model = Object.freeze({
    eligible(pathname, hasSpaRoot = false) {
      const path = String(pathname || "");
      return hasSpaRoot === true && (KNOWN_SPA.test(path)
        || (path.startsWith("/") && path !== "/" && !SYSTEM_ROUTE.test(path)));
    },
    scope(pathname) {
      const path = String(pathname || "/");
      const family = path.split("/").filter(Boolean)[0] || "spa";
      const segments = path.split("/").filter(Boolean);
      const crmDoctype = /^\/crm\/leads(?:\/|$)/.test(path) ? "CRM Lead"
        : /^\/crm\/deals(?:\/|$)/.test(path) ? "CRM Deal"
          : /^\/crm\/contacts(?:\/|$)/.test(path) ? "Contact"
            : /^\/crm\/organizations(?:\/|$)/.test(path) ? "CRM Organization" : null;
      let docname = null;
      if (crmDoctype && segments.length === 3 && !["view", "new"].includes(segments[2])) {
        try { docname = decodeURIComponent(segments[2]); } catch (_error) { docname = null; }
        if (!docname || docname.length > 500 || /[\u0000-\u001f\u007f]/.test(docname)) docname = null;
      }
      return {
        source: "spa-assistant", scope_mode: "context", route: path,
        page_type: "SPA", page_name: family, ...(crmDoctype ? {doctype: crmDoctype} : {}),
        ...(docname ? {docname} : {}),
      };
    },
    clarification(value, expected = {}) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const valid = typeof value.turn_id === "string" && value.turn_id.length > 0 && value.turn_id.length <= 140
        && typeof value.handoff_id === "string" && value.handoff_id.length <= 140
        && typeof value.token === "string" && /^[A-Za-z0-9_-]{32,128}$/.test(value.token)
        && typeof value.conversation_id === "string" && value.conversation_id === expected.conversationId
        && typeof value.prompt_hash === "string" && /^[a-f0-9]{64}$/.test(value.prompt_hash)
        && value.bound_scope && typeof value.bound_scope === "object" && !Array.isArray(value.bound_scope)
        && (!expected.turnId || value.turn_id === expected.turnId)
        && (expected.handoffId === undefined || value.handoff_id === expected.handoffId);
      return valid ? {
        turnId: value.turn_id, handoffId: value.handoff_id, token: value.token,
        promptHash: value.prompt_hash, boundScope: value.bound_scope,
      } : null;
    },
    dispatchAttended(handoff, prepare) {
      if (handoff?.kind !== "attended_browser" || typeof prepare !== "function") return false;
      void prepare();
      return true;
    },
    safeError() { return "Muster could not complete that request. Nothing was changed."; },
  });
  window.MusterSpaAssistantModel = model;
  const hasSpaRoot = Boolean(window.document?.querySelector?.("#app, [data-v-app], [data-reactroot], [data-muster-spa-root]"));
  if (!model.eligible(window.location?.pathname, hasSpaRoot) || !window.document?.body || window.document.querySelector(".muster-spa-assistant")) return;

  const html = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
  const random = () => window.crypto?.randomUUID?.().replaceAll("-", "") || `${Date.now()}${Math.random()}`.replace(/\D/g, "").padEnd(24, "0");
  const conversationKey = "muster.spa.conversation";
  let conversation = window.sessionStorage?.getItem(conversationKey);
  if (!conversation) {
    conversation = `spa-${random().slice(0, 32)}`;
    try { window.sessionStorage?.setItem(conversationKey, conversation); } catch (_error) { /* memory-only fallback */ }
  }
  let csrfToken = "";
  let pendingClarification = null;

  async function json(url, options = {}) {
    const response = await window.fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
    if (!response.ok) throw new Error("request-unavailable");
    const value = await response.json();
    return value?.message;
  }

  async function csrf() {
    if (csrfToken) return csrfToken;
    const meta = window.document.querySelector('meta[name="csrf-token"]')?.content;
    const existing = window.csrf_token || meta;
    if (typeof existing === "string" && existing.trim()) csrfToken = existing.trim();
    else {
      const surface = ["crm", "helpdesk"].includes(model.scope(window.location.pathname).page_name)
        ? model.scope(window.location.pathname).page_name : "";
      const query = surface ? `surface=${encodeURIComponent(surface)}` : `route=${encodeURIComponent(window.location.pathname)}`;
      const issued = await json(`/api/method/muster.api.surface.bootstrap?${query}`);
      if (!issued || issued.schema_version !== 1 || typeof issued.csrf_token !== "string" || !issued.csrf_token.trim()) throw new Error("csrf-unavailable");
      csrfToken = issued.csrf_token.trim();
    }
    return csrfToken;
  }

  async function method(name, args = {}, verb = "POST") {
    const url = `/api/method/${encodeURIComponent(name)}`;
    if (verb === "GET") {
      const query = new URLSearchParams(args).toString();
      return json(`${url}${query ? `?${query}` : ""}`);
    }
    const token = await csrf();
    return json(url, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "X-Frappe-CSRF-Token": token},
      body: new URLSearchParams(args).toString(),
    });
  }

  const style = window.document.createElement("style");
  style.textContent = `.muster-spa-assistant{position:fixed;z-index:2147483000;right:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom));pointer-events:auto;font:13px/1.45 system-ui,sans-serif;color:#17151c}.muster-spa-toggle{min-width:44px;min-height:44px;border:0;border-radius:999px;padding:11px 16px;background:#6d28d9;color:#fff;font-weight:700;box-shadow:0 12px 32px #31185a42}.muster-spa-panel{display:none;box-sizing:border-box;width:min(390px,calc(100vw - 24px));max-height:min(650px,80dvh);overflow:auto;margin-bottom:8px;padding:14px;border:1px solid #ddd6e7;border-radius:16px;background:#fff;box-shadow:0 18px 52px #21172e33}.muster-spa-assistant[data-open=true] .muster-spa-panel{display:block}.muster-spa-head{display:flex;justify-content:space-between;gap:10px;margin-bottom:9px}.muster-spa-log{display:grid;gap:7px;max-height:300px;overflow:auto}.muster-spa-message{padding:8px 9px;border-radius:10px;background:#f4f1f8;white-space:pre-wrap;overflow-wrap:anywhere}.muster-spa-message[data-kind=user]{margin-left:30px;background:#ede9fe}.muster-spa-prompt{box-sizing:border-box;width:100%;min-height:82px;margin-top:9px;padding:9px;border:1px solid #d5cede;border-radius:10px;font:inherit;resize:vertical}.muster-spa-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:8px}.muster-spa-actions button{min-height:44px;border:1px solid #d5cede;border-radius:8px;padding:7px 10px;background:#fff}.muster-spa-actions .is-primary{border-color:#6d28d9;background:#6d28d9;color:#fff}.muster-spa-handoffs{display:flex;flex-wrap:wrap;gap:5px}.muster-spa-handoffs a,.muster-spa-handoffs button{min-height:44px;font:inherit}@media(max-width:767px){.muster-spa-assistant{right:max(8px,env(safe-area-inset-right));bottom:max(8px,env(safe-area-inset-bottom));left:max(8px,env(safe-area-inset-left))}.muster-spa-panel{width:100%;max-height:calc(100dvh - max(76px,env(safe-area-inset-top)) - max(64px,env(safe-area-inset-bottom)));padding:12px}.muster-spa-toggle{float:right}.muster-spa-head{align-items:flex-start;flex-direction:column;gap:2px}.muster-spa-actions>*{flex:1}}`;
  window.document.head.appendChild(style);

  const root = window.document.createElement("aside");
  root.className = "muster-spa-assistant";
  root.dataset.open = "false";
  root.hidden = true;
  root.innerHTML = `<div class="muster-spa-panel"><div class="muster-spa-head"><strong>Ask Muster</strong><small>Uses your current Frappe access</small></div><div class="muster-spa-log" aria-live="polite"></div><textarea class="muster-spa-prompt" placeholder="Ask about this app, your work, reports, records, or what to do next…"></textarea><div class="muster-spa-actions"><button type="button" data-close>Close</button><button type="button" class="is-primary" data-send>Ask Muster</button></div></div><button type="button" class="muster-spa-toggle" aria-expanded="false">Ask Muster</button>`;
  window.document.body.appendChild(root);
  const currentSurface = ["crm", "helpdesk"].includes(model.scope(window.location.pathname).page_name)
    ? model.scope(window.location.pathname).page_name : "";
  const supportArgs = currentSurface ? {surface: currentSurface} : {route: window.location.pathname};
  Promise.all([
    method("frappe.auth.get_logged_user", {}, "GET"),
    method("muster.api.surface.bootstrap", supportArgs, "GET"),
  ]).then(([user, support]) => {
    if (typeof user === "string" && user && user !== "Guest"
      && support?.schema_version === 1 && support?.adapter_contract === 1 && support?.supported === true) root.hidden = false;
    else root.remove();
  }).catch(() => root.remove());
  const log = root.querySelector(".muster-spa-log");
  const prompt = root.querySelector(".muster-spa-prompt");
  const send = root.querySelector("[data-send]");
  const toggle = root.querySelector(".muster-spa-toggle");

  function append(kind, text) {
    const item = window.document.createElement("div");
    item.className = "muster-spa-message";
    item.dataset.kind = kind;
    item.textContent = text;
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
    return item;
  }

  function proposalLink(proposal) {
    const link = window.document.createElement("a");
    link.href = `/desk/muster-workflow-proposal/${encodeURIComponent(proposal)}`;
    link.textContent = "Open audit record";
    return link;
  }

  function handoffs(turnId, rows) {
    if (!turnId || !Array.isArray(rows)) return;
    const offered = rows.filter((row) => row?.state === "offered" && row.requires === "explicit_confirmation");
    if (!offered.length) return;
    const box = window.document.createElement("div");
    box.className = "muster-spa-handoffs";
    offered.forEach((handoff) => {
      const button = window.document.createElement("button");
      button.type = "button";
      button.textContent = handoff.label || "Prepare reviewed work";
      const prepare = async () => {
        const attended = handoff.kind === "attended_browser";
        if (!attended && !window.confirm("Prepare an inert proposal for review? Nothing will run or change yet.")) return;
        button.disabled = true;
        let proposal = "";
        try {
          const accepted = await method("muster.api.ask.accept_handoff", {turn_id: turnId, handoff_id: handoff.id, confirmed: "1", idempotency_key: random().slice(0, 24)});
          if (accepted?.status === "clarification") {
            const continuation = model.clarification(accepted.continuation, {
              conversationId: conversation, turnId, handoffId: handoff.id,
            });
            if (!continuation || typeof accepted.reason !== "string" || !accepted.reason.trim()) throw new Error("invalid-clarification-receipt");
            pendingClarification = continuation;
            append("assistant", accepted.reason);
            append("assistant", "Reply with only the missing details. I’ll show the complete merged request before continuing.");
            prompt.placeholder = "Add the missing detail for the request above…";
            prompt.focus();
            box.remove();
            return;
          }
          proposal = accepted?.proposal;
          if (typeof proposal !== "string" || !proposal) throw new Error("proposal-unavailable");
          let confirmationRendered = false;
          if (attended) {
            const receipt = await method("muster.api.mission.prepare_attended_preview", {proposal, confirmed: "1", idempotency_key: random().slice(0, 24)});
            if (!window.musterSurfaceAdapters?.start) throw new Error("surface-unavailable");
            const session = await window.musterSurfaceAdapters.start(receipt);
            const nativeAction = typeof session?.confirmationLabel === "string"
              ? session.confirmationLabel.replace(/^Confirm /, "")
              : (receipt.operation === "update" ? "Save" : "Create");
            append("assistant", `Attended preview opened. Muster paused before the native ${nativeAction} action.`);
            const renderNativeConfirmation = (confirmAction, label, operation) => {
              const confirmation = window.document.createElement("button");
              confirmation.type = "button";
              confirmation.textContent = label;
              confirmation.addEventListener("click", async () => {
                if (confirmation.disabled) return;
                confirmation.disabled = true;
                try {
                  const verified = await confirmAction();
                  const committed = operation === "update" ? verified?.updated === true : verified?.created === true;
                  if (!committed || verified?.verified !== true || verified?.operation !== operation
                    || typeof verified.recordName !== "string") {
                    throw new Error("unverified-native-save");
                  }
                  confirmation.replaceWith(proposalLink(proposal));
                  append("assistant", `${operation === "update" ? "Updated" : "Created"} and verified ${verified.recordName}.`);
                } catch (_error) {
                  append("assistant", "Muster stopped safely. Nothing else will be clicked. Review the native form and audit record before retrying.");
                }
              });
              box.replaceChildren(confirmation, proposalLink(proposal));
              log.scrollTop = log.scrollHeight;
              confirmation.scrollIntoView({block: "nearest", inline: "nearest"});
              confirmation.focus({preventScroll: true});
              confirmationRendered = true;
            };
            if (typeof session?.confirm === "function" && typeof session.confirmationLabel === "string") {
              renderNativeConfirmation(() => session.confirm(), session.confirmationLabel, receipt.operation);
            } else if (["create", "update"].includes(receipt?.operation) && receipt.save_requires_confirmation === true
              && receipt.save_authorized === false) {
              renderNativeConfirmation(async () => {
                const reviewed = await method("muster.api.mission.review_proposal", {
                  proposal, action: "approve", idempotency_key: random().slice(0, 24),
                });
                if (reviewed?.proposal !== proposal || reviewed?.status !== "Approved" || reviewed?.executed !== false) {
                  throw new Error("approval-unavailable");
                }
                const approvedReceipt = await method("muster.api.mission.prepare_attended_preview", {
                  proposal, confirmed: "1", idempotency_key: random().slice(0, 24),
                });
                const approvedSession = await window.musterSurfaceAdapters.start(approvedReceipt);
                const allowedLabels = receipt.operation === "update"
                  ? new Set(["Confirm Save"]) : new Set(["Confirm Create", "Confirm Submit"]);
                if (typeof approvedSession?.confirm !== "function" || !allowedLabels.has(approvedSession.confirmationLabel)) {
                  throw new Error("confirmation-unavailable");
                }
                return approvedSession.confirm();
              }, receipt.operation === "update" ? "Confirm Save" : "Confirm Create", receipt.operation);
            }
          }
          if (!confirmationRendered) box.replaceChildren(proposalLink(proposal));
        } catch (_error) {
          append("assistant", attended ? "Muster could not open the attended form. Nothing was saved." : model.safeError());
          if (proposal) box.replaceChildren(proposalLink(proposal));
          else button.disabled = false;
        }
      };
      button.addEventListener("click", prepare);
      box.appendChild(button);
      if (handoff.kind === "attended_browser") {
        button.hidden = true;
        append("assistant", "Muster is opening the native form and will pause before Save.");
        model.dispatchAttended(handoff, prepare);
      }
    });
    log.appendChild(box);
  }

  async function poll(runId, pending) {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const state = await method("muster.api.ask.poll", {run_id: runId, wait_ms: "10000"}, "GET");
      if (state?.status === "completed") { pending.textContent = state.answer || "Muster completed the request."; return; }
      if (state?.status === "failed") { pending.textContent = model.safeError(); return; }
    }
    pending.textContent = "This is taking longer than expected. You can retry safely.";
  }

  async function ask() {
    const text = prompt.value.trim();
    if (!text || send.disabled) return;
    send.disabled = true;
    append("user", text);
    prompt.value = "";
    const pending = append("assistant", "Working with your permitted site context…");
    try {
      const clarification = pendingClarification;
      const submitted = await method("muster.api.ask.submit", {
        prompt: text,
        conversation_id: conversation,
        scope: JSON.stringify(clarification?.boundScope || model.scope(window.location.pathname)),
        idempotency_key: random().slice(0, 24),
        ...(clarification ? {
          clarification_turn_id: clarification.turnId,
          clarification_handoff_id: clarification.handoffId,
          clarification_token: clarification.token,
          clarification_prompt_hash: clarification.promptHash,
        } : {}),
      });
      if (clarification) {
        if (typeof submitted?.merged_objective !== "string" || !submitted.merged_objective.trim()) throw new Error("missing-merged-objective");
        pendingClarification = null;
        prompt.placeholder = "Ask about this app, your work, reports, records, or what to do next…";
        append("assistant", `Merged request (original + your reply):\n${submitted.merged_objective}`);
      }
      if (submitted?.status === "clarification") {
        pending.textContent = submitted.reason;
        const next = model.clarification(submitted.continuation, {
          conversationId: conversation, turnId: submitted.turn_id, handoffId: "intent",
        });
        if (next) {
          pendingClarification = next;
          prompt.placeholder = "Add the missing detail for the request above…";
        }
        return;
      }
      if (submitted?.status === "needs_read_plan") { pending.textContent = submitted.reason; return; }
      handoffs(submitted?.turn_id, submitted?.handoffs);
      if (!submitted?.run_id) throw new Error("run-unavailable");
      await poll(submitted.run_id, pending);
    } catch (_error) {
      pending.textContent = model.safeError();
    } finally {
      send.disabled = false;
      prompt.focus();
    }
  }

  toggle.addEventListener("click", () => {
    const open = root.dataset.open !== "true";
    root.dataset.open = String(open);
    toggle.setAttribute("aria-expanded", String(open));
    if (open) prompt.focus();
  });
  root.querySelector("[data-close]").addEventListener("click", () => { root.dataset.open = "false"; toggle.setAttribute("aria-expanded", "false"); });
  send.addEventListener("click", ask);
  prompt.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); ask(); } });
})();
