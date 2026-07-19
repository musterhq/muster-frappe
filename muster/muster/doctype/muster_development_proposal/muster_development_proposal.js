frappe.ui.form.on("Muster Development Proposal", {
  refresh(frm) {
    if (frm.doc.status === "Proposed") {
      frm.add_custom_button(__("Approve"), () => review(frm, "approve"), __("Review"));
      frm.add_custom_button(__("Reject"), () => review(frm, "reject"), __("Review"));
    }
    if (frm.doc.status === "Approved") {
      frm.add_custom_button(__("Generate isolated patch"), () => frappe.confirm(
        __("Run Codex offline in a fresh exported workspace? The registered source will remain unchanged."),
        () => call(frm, "muster.api.development.generate", {proposal: frm.doc.name, confirmed: 1}),
      ));
    }
    if (frm.doc.status === "Ready") {
      frm.add_custom_button(__("Apply reviewed patch"), () => frappe.confirm(
        __("Apply only the exact reviewed patch to the unchanged registered revision? This does not deploy, migrate, build, or restart."),
        () => call(frm, "muster.api.development.apply", {proposal: frm.doc.name, confirmed: 1}),
      ));
    }
  },
});

function review(frm, action) {
  frappe.confirm(
    action === "approve"
      ? __("Approve this exact source revision and allowed-path boundary? Approval does not run the worker.")
      : __("Reject this development proposal?"),
    () => call(frm, "muster.api.development.review", {proposal: frm.doc.name, action}),
  );
}

async function call(frm, method, args) {
  await frappe.call({method, type: "POST", args, freeze: true});
  await frm.reload_doc();
}

