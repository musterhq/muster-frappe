const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadUI({user = "operator@example.test", roles = ["Muster Operator"]} = {}) {
  const handlers = {};
  const dialogs = [];
  const calls = [];
  const routes = [];
  const frappe = {
    session: {user},
    user: {has_role: (role) => roles.includes(role)},
    ui: {
      form: {on(_doctype, definition) { Object.assign(handlers, definition); }},
      Dialog: function Dialog(options) {
        this.options = options;
        this.show = () => dialogs.push(this);
        this.hide = () => {};
        this.disable_primary_action = () => {};
        this.enable_primary_action = () => {};
      },
    },
    utils: {escape_html: String, get_random() { return "random"; }},
    msgprint() {},
    async call(options) {
      calls.push(options);
      return {message: {mission: "MST-MSN-1"}};
    },
    set_route(...parts) { routes.push(parts); },
  };
  const window = {};
  const context = {window, frappe, __: String, console};
  vm.runInNewContext(
    fs.readFileSync(
      path.join(__dirname, "../muster/muster/doctype/muster_workflow_proposal/muster_workflow_proposal.js"),
      "utf8",
    ),
    context,
  );
  return {ui: window.MusterWorkflowProposalUI, handlers, dialogs, calls, routes};
}

function form(overrides = {}) {
  return {
    doc: {
      status: "Published",
      requested_by: "operator@example.test",
      published_workflow: "Reviewed workflow",
      published_version: "Reviewed workflow-v1",
      ...overrides,
    },
  };
}

test("offers Start only to the original mission-capable requester", () => {
  const {ui} = loadUI();
  assert.equal(ui.canStartPublishedProposal(form()), true);
  assert.equal(ui.canStartPublishedProposal(form({requested_by: "someone@example.test"})), false);
});

test("fails closed when publication evidence or a creator role is absent", () => {
  const {ui} = loadUI({roles: ["Muster Viewer"]});
  assert.equal(ui.canStartPublishedProposal(form()), false);
  const operator = loadUI().ui;
  assert.equal(operator.canStartPublishedProposal(form({status: "Approved"})), false);
  assert.equal(operator.canStartPublishedProposal(form({published_version: ""})), false);
  assert.equal(operator.canStartPublishedProposal(form({published_workflow: ""})), false);
});

test("registers the native proposal form controller", () => {
  const {handlers} = loadUI();
  assert.equal(typeof handlers.refresh, "function");
});

test("requires the visible confirmation before calling the Start API", async () => {
  const {ui, dialogs, calls, routes} = loadUI();
  ui.startProposal(form({name: "MST-WFP-1"}));
  assert.equal(dialogs.length, 1);
  await dialogs[0].options.primary_action({confirm_start: 0});
  assert.equal(calls.length, 0);
  await dialogs[0].options.primary_action({confirm_start: 1});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "muster.api.mission.start_proposal");
  assert.equal(calls[0].args.confirmed, 1);
  assert.equal(calls[0].args.proposal, "MST-WFP-1");
  assert.deepEqual(routes[0], ["Form", "Muster Mission", "MST-MSN-1"]);
});
