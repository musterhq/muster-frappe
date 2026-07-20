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

test("active mission polling backs off without an unbounded retry storm", () => {
  assert.equal(model.refreshBackoff(0), 0);
  assert.equal(model.refreshBackoff(1), 2000);
  assert.equal(model.refreshBackoff(2), 4000);
  assert.equal(model.refreshBackoff(5), 32000);
  assert.equal(model.refreshBackoff(99), 32000);
});

test("background mission polling bypasses Frappe's modal-producing request wrapper", () => {
  assert.match(source, /async function fetchActiveMissions\(\)/);
  assert.match(source, /window\.fetch\(url/);
  assert.doesNotMatch(source, /frappe\.db\.get_list\("Muster Mission"/);
  assert.match(source, /if \(refreshInFlight\)/);
});

test("Desk clarification receipts are conversation and lineage bound", () => {
  const continuation = model.clarification({
    turn_id: "MST-ASK-1", handoff_id: "handoff-a", token: "a".repeat(43),
    conversation_id: "desk-safe", prompt_hash: "b".repeat(64), bound_scope: {doctype: "Customer"},
  }, {conversationId: "desk-safe", turnId: "MST-ASK-1", handoffId: "handoff-a"});
  assert.equal(continuation.turnId, "MST-ASK-1");
  assert.equal(continuation.boundScope.doctype, "Customer");
  assert.equal(model.clarification({...continuation, conversation_id: "desk-other"}, {conversationId: "desk-safe"}), null);
  assert.equal(model.clarification({turn_id: "x", handoff_id: "y", token: "short", conversation_id: "desk-safe", prompt_hash: "b".repeat(64), bound_scope: {}}, {conversationId: "desk-safe"}), null);
});

test("attended Ask handoffs navigate immediately while inert proposals retain confirmation", () => {
  assert.match(source, /muster\.api\.ask\.accept_handoff/);
  assert.match(source, /frappe\.confirm/);
  assert.match(source, /handoff\.kind === "attended_browser"[\s\S]{0,240}button\.hidden = true/);
  assert.match(source, /queueMicrotask\(\(\) => button\.click\(\)\)/);
  assert.match(source, /confirmed:\s*1/);
  assert.match(source, /This will not publish, start, open a browser, or change Frappe/);
  assert.doesNotMatch(source, /muster\.api\.mission\.start_proposal[\s\S]{0,500}handoff/);
});

test("Desk keeps a clarified reply bound to the original Ask and displays the merged objective", () => {
  assert.match(source, /response\.message\?\.status === "clarification"/);
  assert.match(source, /clarification_turn_id:\s*clarification\.turnId/);
  assert.match(source, /clarification_handoff_id:\s*clarification\.handoffId/);
  assert.match(source, /clarification_token:\s*clarification\.token/);
  assert.match(source, /clarification_prompt_hash:\s*clarification\.promptHash/);
  assert.match(source, /clarification\?\.boundScope \|\| currentScope\(\)/);
  assert.match(source, /response\.message\.merged_objective/);
  assert.match(source, /appendMessage\("assistant", response\.message\.reason\)/);
});

test("attended handoff prepares its non-saving receipt and starts the real-form preview immediately", async () => {
  const events = [];
  const opened = await model.startAttendedHandoff(
    "attended_browser",
    "MST-WFP-ATTENDED",
    async (proposal) => {
      events.push(["prepare", proposal]);
      return {proposal, executed: false, save_requires_confirmation: true};
    },
    async (receipt) => events.push(["start", receipt]),
  );
  assert.equal(opened, true);
  assert.deepEqual(events, [
    ["prepare", "MST-WFP-ATTENDED"],
    ["start", {proposal: "MST-WFP-ATTENDED", executed: false, save_requires_confirmation: true}],
  ]);
  assert.match(source, /muster\.api\.mission\.prepare_attended_preview/);
  assert.match(source, /window\.musterSurfaceAdapters\.start\(receipt\)/);
  assert.ok(source.indexOf("muster.api.ask.accept_handoff") < source.indexOf("muster.api.mission.prepare_attended_preview"));
});

test("attended navigation truthfully promises real form work without Save and keeps audit recovery secondary", async () => {
  let called = false;
  assert.equal(await model.startAttendedHandoff("governed_change", "MST-WFP-1", async () => { called = true; }, async () => { called = true; }), false);
  assert.equal(called, false);
  assert.match(source, /opening the actual Frappe form and will pause before Save/);
  assert.match(source, /Open audit or recover this preview/);
  assert.match(source, /could not open the attended form\. Nothing was saved/);
});

test("permission-filtered catalog separates slash commands from governed mentions", () => {
  const items = model.catalog({schema_version: 1, items: [
    {kind: "command", id: "status", label: "Status", description: "Current status", token: "/status"},
    {kind: "agent", id: "finance", label: "Finance", description: "Finance agent", token: "@agent:finance"},
    {kind: "workflow", id: "close", label: "Close", description: "Month close", token: "@workflow[close]"},
    {kind: "skill", id: "pdf", label: "PDF", description: "Create PDFs", token: "@skill:pdf"},
    {kind: "mcp", id: "drive", label: "Drive", description: "Drive MCP", token: "@mcp:drive"},
    {kind: "secret", id: "bad", label: "Bad", description: "Bad", token: "bad"},
    {kind: "agent", id: "bad", label: "Bad", description: "Bad token", token: "javascript:alert(1)"},
  ]});
  assert.deepEqual(Array.from(model.filterCatalog(items, "/", "stat"), (row) => row.kind), ["command"]);
  assert.deepEqual(Array.from(model.filterCatalog(items, "@", ""), (row) => row.kind), ["agent", "workflow", "mcp", "skill"]);
});

test("toolbar slash selection creates a real leading command and preserves prompt text as arguments", () => {
  const command = model.applySelection("monthly sales", 13, 13, "/", "/reports");
  assert.equal(command.value, "/reports monthly sales");
  assert.equal(command.caret, 9);
  const replacement = model.applySelection("/sta this week", 4, 4, "/", "/status");
  assert.equal(replacement.value, "/status this week");
  assert.match(source, /if \(paletteTrigger === "\/"\) setIntent\("ask"\)/);
});

test("mention selection stays within a free-form Ask prompt", () => {
  const selected = model.applySelection("Please ask @fin", 15, 15, "@", "@agent:finance");
  assert.equal(selected.value, "Please ask @agent:finance ");
  assert.equal(selected.caret, selected.value.length);
});

test("tool-call cards accept only bounded presentation records", () => {
  const calls = model.presentableCalls([
    {kind: "mcp", status: "completed", label: "Customer form", summary: "Read permitted fields", details: {scope: "Customer"}},
    {kind: "tool", status: "failed", label: "provider backend trace", summary: `model failed at /srv/private ${"a".repeat(64)}`, details: {outcome: "stack trace localhost"}},
    {kind: "provider_trace", status: "completed", label: "Internal", summary: "raw trace"},
    {kind: "tool", status: "unknown", label: "Bad", summary: "Bad"},
  ]);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].label, "Customer form");
  assert.equal(calls[1].label, "Muster step");
  assert.equal(calls[1].summary, "This step could not be completed. Nothing was changed.");
  assert.equal(calls[1].details, undefined);
  assert.match(source, /details\.purpose/);
  assert.doesNotMatch(source, /details\.raw|details\.arguments|details\.stack/);
  assert.match(source, /<summary>\$\{__\("What Muster did"\)\}/);
  assert.doesNotMatch(source, /call\.kind === "mcp" \? "MCP"/);
});

test("the dock discovers commands from the governed server catalog", () => {
  assert.match(source, /muster\.api\.catalog\.get_palette/);
  assert.match(source, /data-muster-palette="\/"/);
  assert.match(source, /data-muster-palette="@"/);
  assert.doesNotMatch(source, /state\.partial_text/);
  assert.doesNotMatch(source, /throw error;/);
  assert.doesNotMatch(source, /console\.debug\([^\n]*error/);
});
