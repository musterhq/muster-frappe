const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadModel() {
  const window = {};
  const context = {
    window,
    frappe: {},
    document: {},
    $() { return {on() {}}; },
    __: String,
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../muster/public/js/activity_dock.js"), "utf8"),
    context,
  );
  return window.MusterAskDockModel;
}

const model = loadModel();
const source = fs.readFileSync(path.join(__dirname, "../muster/public/js/activity_dock.js"), "utf8");

test("Ask is a universal conversation path and workflow planning is explicit", () => {
  assert.equal(model.submitMethod("ask"), "muster.api.ask.submit");
  assert.equal(model.submitMethod("workflow"), "muster.api.mission.plan");
});

test("current Desk route contributes context without changing the request intent", () => {
  const context = model.scope(["Form", "Sales Invoice", "SINV-0001"], "Form/Sales Invoice/SINV-0001");
  assert.equal(context.scope_mode, "context");
  assert.equal(context.doctype, "Sales Invoice");
  assert.equal(context.docname, "SINV-0001");
});

test("only completed and failed Ask states are terminal", () => {
  assert.equal(model.terminal("queued"), false);
  assert.equal(model.terminal("running"), false);
  assert.equal(model.terminal("completed"), true);
  assert.equal(model.terminal("failed"), true);
});

test("Ask handoffs require a separate visible confirmation and create only an inert proposal", () => {
  assert.match(source, /muster\.api\.ask\.accept_handoff/);
  assert.match(source, /frappe\.confirm/);
  assert.match(source, /confirmed:\s*1/);
  assert.match(source, /This will not publish, start, open a browser, or change Frappe/);
  assert.doesNotMatch(source, /muster\.api\.mission\.start_proposal[\s\S]{0,500}handoff/);
});
