(() => {
  "use strict";

  const TERMINAL = new Set(["Completed", "Failed", "Cancelled"]);
  const WAITING = new Set(["Waiting for Approval", "Paused", "Needs Intervention"]);
  const QUIET_EVENTS = new Set(["lease_claimed", "lease_heartbeat"]);
  const UI_SURFACES = new Set(["browser", "desk", "frappe_desk", "ui", "computer"]);
  const SAFE_PAYLOAD_KEYS = new Set([
    "actionLabel", "actionType", "approval", "changedFields", "currentRoute",
    "doctype", "documentName", "executionSurface", "fields", "fieldsAffected",
    "nodeKind", "pointer", "recordName", "route", "targetRoute", "toolAction",
    "verification", "verificationStatus", "viewport", "customizationEvidence", "takeoverLabel",
  ]);
  const FORBIDDEN_KEY = /password|passwd|secret|api.?key|token|authorization|cookie|private.?key|reasoning|chain.?of.?thought/i;

  function parsePayload(value) {
    if (!value) return {};
    if (typeof value === "object" && !Array.isArray(value)) return filterPayload(value);
    if (typeof value !== "string" || value.length > 65536) return {};
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? filterPayload(parsed) : {};
    } catch (_error) {
      return {};
    }
  }

  function filterPayload(payload) {
    const safe = {};
    Object.entries(payload).forEach(([key, value]) => {
      if (!SAFE_PAYLOAD_KEYS.has(key) || FORBIDDEN_KEY.test(key)) return;
      if (typeof value === "string") safe[key] = value.slice(0, 500);
      else if (typeof value === "number" || typeof value === "boolean") safe[key] = value;
      else if (Array.isArray(value)) safe[key] = value.slice(0, 20).filter((item) => ["string", "number"].includes(typeof item)).map((item) => String(item).slice(0, 140));
      else if (value && typeof value === "object") {
        safe[key] = Object.fromEntries(Object.entries(value).filter(([childKey, childValue]) => !FORBIDDEN_KEY.test(childKey) && ["string", "number", "boolean"].includes(typeof childValue)).slice(0, 20));
      }
    });
    return safe;
  }

  function normalizedEvent(row) {
    return {
      sequence: Number(row.sequence) || 0,
      type: String(row.event_type || "activity").slice(0, 80),
      state: String(row.state || "").slice(0, 80),
      summary: String(row.summary || "Activity recorded").slice(0, 240),
      actor: String(row.actor || "").slice(0, 140),
      agent: String(row.agent || "").slice(0, 140),
      referenceDoctype: String(row.reference_doctype || "").slice(0, 140),
      referenceName: String(row.reference_name || "").slice(0, 140),
      creation: row.creation,
      payload: parsePayload(row.payload_json),
    };
  }

  function isExplicitUiAction(event) {
    if (!event || event.type !== "effect_started") return false;
    const payload = event.payload || {};
    return UI_SURFACES.has(String(payload.executionSurface || "").toLowerCase()) && Boolean(payload.actionLabel || payload.toolAction || payload.actionType);
  }

  function derivePresence(mission, events) {
    const status = String(mission?.status || "");
    if (WAITING.has(status)) return {key: "waiting", label: "Waiting for you"};
    if (TERMINAL.has(status) || !status) return {key: "user", label: "User control"};
    const latest = [...events].reverse().find((event) => !QUIET_EVENTS.has(event.type));
    if (latest?.type === "paused" || latest?.type === "pause_requested") return {key: "waiting", label: "Waiting for you"};
    if (isExplicitUiAction(latest)) return {key: "controlling", label: "Muster is controlling this work session"};
    return {key: "server", label: "Muster is working server-side"};
  }

  function routeFrom(events) {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const payload = events[index].payload || {};
      const value = payload.currentRoute || payload.targetRoute || payload.route;
      if (typeof value !== "string") continue;
      const route = value.trim().replace(/^https?:\/\/[^/]+/i, "");
      if (route.startsWith("/desk/") || route === "/desk") return route.slice(0, 300);
    }
    return "";
  }

  function detailsFrom(events) {
    const fields = [];
    const approvals = [];
    const verifications = [];
    let doctype = "";
    let recordName = "";
    let customization = null;
    events.forEach((event) => {
      const payload = event.payload || {};
      doctype = String(payload.doctype || doctype).slice(0, 140);
      recordName = String(payload.documentName || payload.recordName || recordName).slice(0, 140);
      if (payload.customizationEvidence && typeof payload.customizationEvidence === "object") customization = payload.customizationEvidence;
      [payload.fieldsAffected, payload.changedFields, payload.fields].forEach((values) => {
        if (Array.isArray(values)) values.forEach((value) => fields.push(String(value).slice(0, 140)));
      });
      if (payload.approval && typeof payload.approval === "object") approvals.push(payload.approval);
      if (event.type.includes("verification") || payload.nodeKind === "verification" || payload.verification || payload.verificationStatus) {
        verifications.push(String(payload.verification || payload.verificationStatus || event.summary).slice(0, 240));
      }
    });
    return {doctype, recordName, fields: [...new Set(fields)].slice(0, 12), approvals: approvals.slice(-4), verifications: [...new Set(verifications)].slice(-4), customization};
  }

  function viewModel(mission, rows) {
    const events = (Array.isArray(rows) ? rows : []).map(normalizedEvent).sort((a, b) => a.sequence - b.sequence);
    const active = [...events].reverse().find((event) => !QUIET_EVENTS.has(event.type));
    return {
      mission: mission || {},
      events,
      presence: derivePresence(mission, events),
      route: routeFrom(events),
      details: detailsFrom(events),
      activeAction: active?.payload?.actionLabel || active?.payload?.toolAction || active?.summary || "Waiting for the first verified action",
      pointer: isExplicitUiAction(active) ? active.payload.pointer : null,
    };
  }

  const html = (value) => window.frappe?.utils?.escape_html ? frappe.utils.escape_html(String(value ?? "")) : String(value ?? "").replace(/[&<>"']/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;"})[character]);
  const t = (value) => typeof window.__ === "function" ? __(value) : value;

  class LiveWorkSession {
    constructor() {
      this.missionName = null;
      this.cursor = 0;
      this.rows = [];
      this.loading = false;
      this.testMode = Boolean(window.frappe?.boot?.muster?.test_mode) && window.frappe?.session?.user === "Administrator";
      this.element = document.createElement("section");
      this.element.className = "muster-live-session";
      this.element.setAttribute("aria-label", t("Muster live work session"));
      this.element.setAttribute("aria-hidden", "true");
      document.body.appendChild(this.element);
      window.frappe?.realtime?.on("muster_activity", (event) => {
        if (event.mission === this.missionName) this.refresh().catch(() => {});
      });
      window.frappe?.realtime?.on("muster_mission_changed", (event) => {
        if (event.mission === this.missionName) this.refresh().catch(() => {});
      });
      this.poll = window.setInterval(() => {
        if (this.missionName && document.visibilityState === "visible") this.refresh().catch(() => {});
      }, 5000);
    }

    async open(name) {
      if (!name) return;
      this.missionName = name;
      this.cursor = 0;
      this.rows = [];
      this.element.classList.add("is-open");
      this.element.setAttribute("aria-hidden", "false");
      this.renderLoading();
      await this.refresh();
    }

    close() {
      this.missionName = null;
      this.element.classList.remove("is-open");
      this.element.setAttribute("aria-hidden", "true");
    }

    async refresh() {
      if (!this.missionName || this.loading) return;
      this.loading = true;
      const name = this.missionName;
      try {
        const [mission, response] = await Promise.all([
          frappe.db.get_doc("Muster Mission", name),
          frappe.call("muster.api.mission.activities", {mission: name, after_sequence: this.cursor, limit: 200}),
        ]);
        if (name !== this.missionName) return;
        const fresh = response.message || [];
        this.rows = [...this.rows, ...fresh].slice(-200);
        this.cursor = this.rows.reduce((maximum, row) => Math.max(maximum, Number(row.sequence) || 0), this.cursor);
        this.render(viewModel(mission, this.rows));
      } catch (error) {
        if (name === this.missionName) this.renderError();
        throw error;
      } finally {
        this.loading = false;
      }
    }

    renderLoading() {
      this.element.innerHTML = `<div class="muster-live-loading" role="status">${html(t("Loading verified work session…"))}</div>`;
    }

    renderError() {
      this.element.innerHTML = `<div class="muster-live-error" role="alert"><strong>${html(t("This work session could not be loaded."))}</strong><button type="button" class="btn btn-sm btn-default" data-live-close>${html(t("Close"))}</button></div>`;
      this.bind();
    }

    render(model) {
      const {mission, events, presence, route, details} = model;
      const canControl = mission.requested_by === frappe.session.user && !TERMINAL.has(mission.status) && Boolean(mission.root_run_id);
      const pointer = model.pointer && typeof model.pointer === "object" ? model.pointer : {};
      const pointerX = Math.max(4, Math.min(92, Number(pointer.x) || 64));
      const pointerY = Math.max(8, Math.min(84, Number(pointer.y) || 46));
      const timeline = events.slice(-30).reverse().map((event) => `<li data-event-type="${html(event.type)}"><span class="muster-live-event-dot"></span><div><strong>${html(event.summary)}</strong><small>${html(event.agent || event.actor || event.state || event.type)}${event.creation ? ` · ${html(frappe.datetime.prettyDate(event.creation))}` : ""}</small></div></li>`).join("");
      const fieldMarkup = details.fields.length ? details.fields.map((field) => `<span>${html(field)}</span>`).join("") : `<small>${html(t("No field changes reported yet"))}</small>`;
      const approvalMarkup = mission.status === "Waiting for Approval" || details.approvals.length
        ? `<div class="muster-live-proof is-approval"><b>${html(t("Approval"))}</b><span>${html(mission.status === "Waiting for Approval" ? t("A decision is required before Muster can continue") : t("Approval evidence recorded"))}</span></div>` : "";
      const verificationMarkup = details.verifications.length ? `<div class="muster-live-proof is-verified"><b>${html(t("Verification"))}</b><span>${html(details.verifications.at(-1))}</span></div>` : "";
      const customizationMarkup = details.customization ? `<div class="muster-live-proof is-verified"><b>${html(t("Effective form checked"))}</b><span>${html(`${t("Detected")} ${Number(details.customization.customFieldCount) || 0} ${t("custom field(s)")}, ${Number(details.customization.propertySetterCount) || 0} ${t("property setter(s)")}, ${Number(details.customization.customPermissionCount) || 0} ${t("custom permission row(s)")}, ${Number(details.customization.clientScriptCount) || 0} ${t("Client Script(s)")}, ${Number(details.customization.serverScriptCount) || 0} ${t("Server Script(s)")}${details.customization.workflowDetected ? `, ${t("and an active workflow")}` : ""}`)}</span></div>` : "";
      const cursorMarkup = presence.key === "controlling" ? `<div class="muster-virtual-cursor" style="--cursor-x:${pointerX}%;--cursor-y:${pointerY}%" aria-label="${html(t("Muster virtual cursor"))}"><i></i><span>${html(t("Muster has taken over"))}</span></div>` : "";
      const pauseAction = mission.status === "Paused" ? "resume" : "pause";
      this.element.innerHTML = `
        <header class="muster-live-header">
          <div><p>${html(t("Live autonomous work session"))}</p><h2>${html(mission.objective || mission.name)}</h2></div>
          <button type="button" class="muster-live-close" data-live-close aria-label="${html(t("Close live work session"))}">×</button>
        </header>
        <div class="muster-live-presence" data-presence="${presence.key}" role="status" aria-live="polite"><span></span><strong>${html(t(presence.label))}</strong><small>${html(mission.status || "")}</small></div>
        <div class="muster-live-controls">
          ${canControl ? `<button type="button" class="btn btn-sm btn-default" data-live-control="${pauseAction}">${html(t(pauseAction === "pause" ? "Pause and take control" : "Resume Muster"))}</button>` : ""}
          ${canControl ? `<button type="button" class="btn btn-sm btn-default" data-live-steer>${html(t("Guide"))}</button>` : ""}
          <a class="btn btn-sm btn-default" href="/desk/muster-mission/${encodeURIComponent(mission.name)}">${html(t("Audit record"))}</a>
        </div>
        <section class="muster-live-viewport" aria-label="${html(t("Observed Muster work surface"))}">
          <div class="muster-live-browser-bar"><span></span><span></span><span></span><code>${html(route || t("Server-side work — no Desk route reported"))}</code>${route ? `<a href="${html(route)}">${html(t("Open"))}</a>` : ""}</div>
          <div class="muster-live-canvas" data-presence="${presence.key}">
            ${cursorMarkup}
            <div class="muster-live-action"><small>${html(t("Current verified action"))}</small><strong>${html(model.activeAction)}</strong></div>
            <div class="muster-live-target"><div><small>${html(t("Target"))}</small><strong>${html([details.doctype, details.recordName].filter(Boolean).join(" · ") || t("Not reported yet"))}</strong></div><div class="muster-live-fields"><small>${html(t("Fields affected"))}</small>${fieldMarkup}</div></div>
            ${customizationMarkup}${approvalMarkup}${verificationMarkup}
          </div>
        </section>
        <section class="muster-live-timeline"><header><h3>${html(t("Verified activity"))}</h3><small>${html(t("High-level actions only — no private reasoning or secrets"))}</small></header><ol>${timeline || `<li class="is-empty">${html(t("Waiting for the first authenticated run event"))}</li>`}</ol></section>`;
      this.bind();
    }

    bind() {
      this.element.querySelectorAll("[data-live-close]").forEach((button) => button.addEventListener("click", () => this.close()));
      this.element.querySelectorAll("[data-live-control]").forEach((button) => button.addEventListener("click", () => this.control(button.dataset.liveControl)));
      this.element.querySelector("[data-live-steer]")?.addEventListener("click", () => this.steer());
    }

    async control(action, note) {
      const button = this.element.querySelector(`[data-live-control="${action}"]`);
      if (button) button.disabled = true;
      try {
        await frappe.call({method: "muster.api.mission.control", type: "POST", args: {mission: this.missionName, action, note, idempotency_key: frappe.utils.get_random(24)}});
        await this.refresh();
      } finally {
        if (button) button.disabled = false;
      }
    }

    steer() {
      const dialog = new frappe.ui.Dialog({
        title: t("Guide this work session"),
        fields: [{fieldname: "note", fieldtype: "Small Text", label: t("Instruction"), reqd: 1, description: t("This becomes a durable, audited steering command.")}],
        primary_action_label: t("Send guidance"),
        primary_action: async ({note}) => { await this.control("steer", note); dialog.hide(); },
      });
      dialog.show();
    }

    loadTestEvents(mission, rows) {
      if (!this.testMode) throw new Error("Muster deterministic playback requires explicit Administrator test mode.");
      this.missionName = mission.name;
      this.rows = rows;
      this.element.classList.add("is-open", "is-test-playback");
      this.element.setAttribute("aria-hidden", "false");
      this.render(viewModel(mission, rows));
    }
  }

  window.MusterLiveSessionModel = {parsePayload, normalizedEvent, derivePresence, viewModel};
  window.musterLiveSession = window.musterLiveSession || new LiveWorkSession();
})();
