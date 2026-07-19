frappe.ui.form.on("Muster Workflow Proposal", {
  refresh(frm) {
    render_proposal(frm);
    if (frm.is_new()) return;
    const canManage = ["Muster Administrator", "Muster Automation Manager", "System Manager"].some((role) => frappe.user.has_role(role));
    if (frm.doc.status === "Proposed" && canManage) {
      frm.add_custom_button(__("Approve proposal"), () => review(frm, "approve"), __("Review"));
      frm.add_custom_button(__("Reject"), () => review(frm, "reject"), __("Review"));
    }
    if (frm.doc.status === "Approved" && canManage) {
      frm.add_custom_button(__("Create & publish workflow"), () => publishProposal(frm), __("Workflow"));
    }
    if (frm.doc.status === "Published" && frm.doc.published_workflow) {
      frm.add_custom_button(__("Open published workflow"), () => frappe.set_route("Form", "Muster Workflow", frm.doc.published_workflow), __("Workflow"));
    }
    if (canStartPublishedProposal(frm)) {
      frm.add_custom_button(__("Start governed mission"), () => startProposal(frm), __("Mission"));
    }
    frm.dashboard.add_comment(__("This is inert JSON. Approval never executes it; publication creates a governed, immutable workflow version, and execution is a separate action."), "blue", true);
  },
});

function render_proposal(frm) {
  const target = frm.fields_dict.proposal_preview?.$wrapper?.empty();
  if (!target || !frm.doc.descriptor_json) return;
  let proposal;
  try { proposal = JSON.parse(frm.doc.descriptor_json); } catch { return; }
  const escape = frappe.utils.escape_html;
  const render_step = (step) => {
    const children = step.steps || step.branches || step.subagents || [];
    const capabilities = (step.capabilities || []).map((item) => `<code>${escape(item)}</code>`).join(" ");
    return `<li><strong>${escape(step.label || step.kind)}</strong> <small>${escape(step.kind)}</small>${capabilities ? `<div>${capabilities}</div>` : ""}${children.length ? `<ol>${children.map(render_step).join("")}</ol>` : ""}</li>`;
  };
  const budget = proposal.budget || {};
  let run = {};
  try { run = JSON.parse(frm.doc.run_metadata_json || "{}"); } catch { /* evidence stays unavailable */ }
  target.html(`<div class="muster-proposal-preview">
    <p>${escape(proposal.meta?.description || "")}</p>
    <div class="muster-proposal-budget"><span>${__("Runtime")}: ${Math.round((budget.runtimeMs || 0) / 60000)}m</span><span>${__("Tool calls")}: ${budget.toolCalls || 0}</span><span>${__("Model calls")}: ${budget.modelCalls || 0}</span><span>${__("Tokens")}: ${budget.tokens || 0}</span></div>
    ${run.runId ? `<p class="text-muted">${__("Planned by")} ${escape(run.providerId)} / ${escape(run.model)} · ${escape(run.runId)} · ${__("read-only, offline")}</p>` : ""}
    <ol class="muster-proposal-steps">${(proposal.steps || []).map(render_step).join("")}</ol>
    <p class="text-muted">${__("Review-only proposal. No tools or Frappe mutations run from this screen.")}</p>
  </div>`);
}

function canStartPublishedProposal(frm) {
  const creatorRole = ["Muster Administrator", "Muster Automation Manager", "Muster Operator", "System Manager"]
    .some((role) => frappe.user.has_role(role));
  return Boolean(
    creatorRole
    && frm.doc.status === "Published"
    && frm.doc.published_workflow
    && frm.doc.published_version
    && frm.doc.requested_by === frappe.session.user
  );
}

async function review(frm, action) {
  await frappe.call({
    method: "muster.api.mission.review_proposal",
    type: "POST",
    args: {proposal: frm.doc.name, action, idempotency_key: frappe.utils.get_random(24)},
    freeze: true,
  });
  await frm.reload_doc();
}

function publishProposal(frm) {
  const dialog = new frappe.ui.Dialog({
    title: __("Publish governed workflow"),
    fields: [
      {fieldname: "root_agent", fieldtype: "Link", options: "Muster Agent", label: __("Root agent"), reqd: 1,
        get_query: () => ({filters: {status: "Active"}})},
      {fieldname: "policy", fieldtype: "Link", options: "Muster Policy", label: __("Policy"), reqd: 1,
        get_query: () => ({filters: {enabled: 1}})},
      {fieldname: "notice", fieldtype: "HTML", options: `<p class="text-muted">${__("This creates a native draft, validates it against current RBAC, and publishes an immutable version. It does not start a mission or perform any business change.")}</p>`},
    ],
    primary_action_label: __("Create & publish"),
    async primary_action(values) {
      dialog.disable_primary_action();
      try {
        const result = await frappe.call({
          method: "muster.api.mission.publish_proposal", type: "POST",
          args: {...values, proposal: frm.doc.name, idempotency_key: frappe.utils.get_random(24)},
          freeze: true,
        });
        dialog.hide();
        await frm.reload_doc();
        frappe.show_alert({message: `${__("Published workflow")}: ${frappe.utils.escape_html(result.message.workflow)}`, indicator: "green"}, 8);
      } finally {
        dialog.enable_primary_action();
      }
    },
  });
  dialog.show();
}

function startProposal(frm) {
  const dialog = new frappe.ui.Dialog({
    title: __("Start governed mission"),
    fields: [
      {fieldname: "summary", fieldtype: "HTML", options: `<p>${__("Muster will create a mission pinned to the reviewed, immutable workflow version.")}</p><p class="text-muted">${__("Current RBAC, policy, agent availability, and evidence hashes are checked again before any work is queued.")}</p>`},
      {fieldname: "confirm_start", fieldtype: "Check", label: __("I confirm that Muster may start this mission"), reqd: 1, default: 0},
    ],
    primary_action_label: __("Start mission"),
    async primary_action(values) {
      if (!values.confirm_start) {
        frappe.msgprint(__("Confirm Start before queueing the mission."));
        return;
      }
      dialog.disable_primary_action();
      try {
        const result = await frappe.call({
          method: "muster.api.mission.start_proposal", type: "POST",
          args: {
            proposal: frm.doc.name,
            confirmed: 1,
            idempotency_key: frappe.utils.get_random(24),
          },
          freeze: true,
        });
        dialog.hide();
        frappe.set_route("Form", "Muster Mission", result.message.mission);
      } finally {
        dialog.enable_primary_action();
      }
    },
  });
  dialog.show();
}

if (typeof window !== "undefined") {
  window.MusterWorkflowProposalUI = Object.freeze({canStartPublishedProposal, startProposal});
}
