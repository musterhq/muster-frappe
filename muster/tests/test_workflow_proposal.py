from __future__ import annotations

import copy
import json
from hashlib import sha256
from uuid import uuid4
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from muster.adapters.client import GatewayClientError
from muster.orchestration.workflow_proposal import (
    WorkflowProposalError,
    validate_compiled_graph,
    validate_workflow_descriptor,
)
from muster.orchestration.workflow_proposal import request_workflow_proposal
from muster.orchestration.workflow_proposal import publish_approved_proposal
from muster.orchestration.workflow_proposal import start_published_proposal_mission
from muster.orchestration.workflow_proposal import _materialize_attended_crud_bundle
from muster.orchestration.workflow_proposal import _attended_form_catalogs


def descriptor():
    return {
        "schemaVersion": 1,
        "id": "invoice.followup.proposal",
        "version": "0.1.0-proposal",
        "meta": {
            "name": "Invoice follow-up",
            "description": "Review-only proposal",
            "phases": [{"title": "Review"}, {"title": "Verify"}],
        },
        "goal": "Review overdue invoices",
        "resultSchema": {"type": "object"},
        "budget": {"runtimeMs": 1000, "toolCalls": 4, "modelCalls": 2, "tokens": 1000, "costMicros": 100, "artifactBytes": 1000},
        "limits": {"maxDepth": 4, "maxChildrenPerNode": 4, "maxActiveNodes": 8, "maxRetries": 2, "maxParallelism": 2, "maxPhases": 4, "maxSteps": 12},
        "steps": [
            {
                "kind": "parallel", "label": "Inspect", "maxConcurrency": 2,
                "branches": [
                    {"kind": "agent", "label": "Analyst", "prompt": "Read invoices", "capabilities": ["frappe.invoice.read"],
                     "subagents": [{"kind": "agent", "label": "RBAC", "prompt": "Test denied records", "capabilities": ["frappe.invoice.read"]}]},
                    {"kind": "agent", "label": "Reporter", "prompt": "Summarize", "capabilities": ["frappe.invoice.read"]},
                ],
            },
            {"kind": "approval", "label": "Review", "prompt": "Approve publication", "requiredRoles": ["Muster Automation Manager"]},
            {"kind": "verification", "label": "Verify", "criteria": "Evidence is complete"},
        ],
    }


def compiled_graph():
    return {
        "schemaVersion": 1,
        "id": "invoice.followup.proposal",
        "version": "0.1.0-proposal",
        "entryNodeId": "n1-inspect",
        "nodes": [
            {"id": "n1-inspect", "kind": "parallel_map", "retryLimit": 2},
            {"id": "n2-analyst", "kind": "agent", "requestedCapabilities": ["frappe.invoice.read"], "retryLimit": 2},
            {"id": "n3-rbac", "kind": "agent", "requestedCapabilities": ["frappe.invoice.read"], "retryLimit": 2},
            {"id": "n4-reporter", "kind": "agent", "requestedCapabilities": ["frappe.invoice.read"], "retryLimit": 2},
            {"id": "n5-inspect-join", "kind": "transform", "retryLimit": 2},
            {"id": "n6-review", "kind": "approval", "retryLimit": 2},
            {"id": "n7-verify", "kind": "verification", "retryLimit": 2},
        ],
        "edges": [
            {"from": "n2-analyst", "to": "n3-rbac"},
            {"from": "n1-inspect", "to": "n2-analyst"},
            {"from": "n3-rbac", "to": "n5-inspect-join"},
            {"from": "n1-inspect", "to": "n4-reporter"},
            {"from": "n4-reporter", "to": "n5-inspect-join"},
            {"from": "n5-inspect-join", "to": "n6-review"},
            {"from": "n6-review", "to": "n7-verify"},
        ],
        "budget": descriptor()["budget"],
        "limits": {"maxDepth": 4, "maxChildrenPerNode": 4, "maxActiveNodes": 8, "maxRetries": 2},
    }


class TestWorkflowProposalValidation(IntegrationTestCase):
    def test_home_page_create_uses_bounded_host_discovery_not_current_page_as_scope_ceiling(self):
        snapshot = {"doctype": "Customer", "schema_hash": "a" * 64, "revision": "b" * 64, "authority": {"read": True, "create": True, "write": True}, "fields": [{"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "required": True, "has_default": False, "writable": True}]}
        with (
            patch("muster.orchestration.workflow_proposal.frappe.get_all", return_value=[frappe._dict(name="Customer")]) as discovery,
            patch("muster.orchestration.workflow_proposal.effective_form_schema", return_value=snapshot),
        ):
            catalogs = _attended_form_catalogs({"source": "desk", "route": "/desk", "scope_mode": "context"}, "sales@example.test", "Create a customer called ACME")
        self.assertEqual([catalog["doctype"] for catalog in catalogs], ["Customer"])
        self.assertIn("create", catalogs[0]["actions"])
        self.assertLessEqual(discovery.call_args.kwargs["limit_page_length"], 50)

    def test_ambiguous_or_unmatched_home_target_fails_closed_before_proposal_materialization(self):
        with patch("muster.orchestration.workflow_proposal.frappe.get_all", return_value=[]):
            catalogs = _attended_form_catalogs({"source": "desk", "route": "/desk"}, "sales@example.test", "Create a new record")
        self.assertEqual(catalogs, [])
        with self.assertRaisesRegex(WorkflowProposalError, "Name a live-readable DocType"):
            _materialize_attended_crud_bundle(descriptor(), compiled_graph(), catalogs, ["frappe.browser.navigate"])

    def test_governed_record_change_compiles_to_host_bound_attended_crud(self):
        effect = {
            "schemaVersion": 1, "capability": "frappe.record.update",
            "operation": {"kind": "record", "action": "update", "doctype": "Customer", "docname": "ACME", "values": {"customer_name": "Acme Ltd", "custom_service_tier": "Gold"}},
            "postconditions": [{"path": "$.customer_name", "operator": "equals", "expected": "Acme Ltd"}],
            "approvalClass": "single",
        }
        proposal = descriptor()
        proposal["steps"] = [{"kind": "execution", "label": "Update customer", "capabilities": ["frappe.record.update"], "execution": {"surface": "server_effect", "plan": effect}}]
        graph = compiled_graph()
        graph["entryNodeId"] = "n1-change"
        graph["nodes"] = [{"id": "n1-change", "kind": "command", "requestedCapabilities": ["frappe.record.update"], "retryLimit": 0, "executionIntent": {"surface": "server_effect", "plan": effect}}]
        graph["edges"] = []
        catalog = {
            "doctype": "Customer", "record_name": "ACME", "actions": ["read", "update"],
            "authority": {"read": True, "create": False, "write": True},
            "schema_hash": "a" * 64, "revision": "b" * 64,
            "fields": [
                {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "required": True, "has_default": False, "writable": True},
                {"fieldname": "custom_service_tier", "label": "Service Tier", "fieldtype": "Select", "required": False, "has_default": False, "writable": True},
            ],
        }
        authority = ["frappe.record.update", "frappe.browser.navigate", "frappe.browser.fill", "frappe.browser.select", "frappe.browser.click"]
        admitted_proposal, admitted_graph = _materialize_attended_crud_bundle(proposal, graph, catalog, authority)
        validated_proposal = validate_workflow_descriptor(admitted_proposal, authority)
        validated_graph = validate_compiled_graph(admitted_graph, validated_proposal, authority)
        plan = validated_graph["nodes"][0]["executionIntent"]["plan"]
        self.assertEqual(validated_graph["nodes"][0]["executionIntent"]["surface"], "browser")
        self.assertEqual(plan["attendedCrud"]["schema_hash"], "a" * 64)
        self.assertEqual(plan["attendedCrud"]["revision"], "b" * 64)
        self.assertEqual(plan["attendedCrud"]["fields"], ["custom_service_tier", "customer_name"])
        self.assertEqual(plan["actions"][-1]["postcondition"]["kind"], "record_saved")
        self.assertFalse(any("schema_hash" in json.dumps(step.get("execution", {})) for step in proposal["steps"]), "provider output was not trusted to author hashes")

    def test_attended_compiler_rejects_model_fields_outside_live_catalog(self):
        effect = {
            "schemaVersion": 1, "capability": "frappe.record.update",
            "operation": {"kind": "record", "action": "update", "doctype": "Customer", "docname": "ACME", "values": {"harmless_alias": "injected"}},
            "postconditions": [{"path": "$.harmless_alias", "operator": "equals", "expected": "injected"}], "approvalClass": "single",
        }
        proposal = descriptor()
        proposal["steps"] = [{"kind": "execution", "label": "Unsafe", "capabilities": ["frappe.record.update"], "execution": {"surface": "server_effect", "plan": effect}}]
        graph = compiled_graph(); graph["entryNodeId"] = "n1"; graph["nodes"] = [{"id": "n1", "kind": "command", "requestedCapabilities": ["frappe.record.update"], "retryLimit": 0, "executionIntent": {"surface": "server_effect", "plan": effect}}]; graph["edges"] = []
        catalog = {"doctype": "Customer", "record_name": "ACME", "actions": ["update"], "authority": {"read": True, "create": False, "write": True}, "schema_hash": "a" * 64, "revision": "b" * 64, "fields": []}
        with self.assertRaisesRegex(WorkflowProposalError, "unavailable"):
            _materialize_attended_crud_bundle(proposal, graph, catalog, ["frappe.browser.navigate", "frappe.browser.fill", "frappe.browser.click"])

    def test_attended_read_discards_model_routes_and_uses_the_host_record_scope(self):
        untrusted = {"surface": "browser", "plan": {"schemaVersion": 1, "actionBudget": 1, "actions": [{"kind": "navigate", "route": "/desk/Evil"}]}}
        proposal = descriptor(); proposal["steps"] = [{"kind": "execution", "label": "Read customer", "capabilities": ["frappe.browser.navigate"], "execution": untrusted}]
        graph = compiled_graph(); graph["entryNodeId"] = "n1"; graph["nodes"] = [{"id": "n1", "kind": "command", "requestedCapabilities": ["frappe.browser.navigate"], "retryLimit": 0, "executionIntent": untrusted}]; graph["edges"] = []
        catalog = {"doctype": "Customer", "record_name": "ACME", "actions": ["read"], "authority": {"read": True, "create": False, "write": False}, "schema_hash": "a" * 64, "revision": "b" * 64, "fields": []}
        authority = ["frappe.browser.navigate", "frappe.browser.read_visible"]
        admitted, compiled = _materialize_attended_crud_bundle(proposal, graph, catalog, authority, requested_kind="attended_browser")
        validated = validate_compiled_graph(compiled, validate_workflow_descriptor(admitted, authority), authority)
        plan = validated["nodes"][0]["executionIntent"]["plan"]
        self.assertEqual(plan["attendedCrud"]["operation"], "read")
        self.assertEqual(plan["actions"][0]["route"], "/desk/customer/ACME")
        self.assertNotIn("Evil", json.dumps(plan))

    def test_accepts_inert_nested_descriptor(self):
        self.assertEqual(
            validate_workflow_descriptor(descriptor(), ["frappe.invoice.read"])["schemaVersion"], 1
        )

    def test_rejects_arbitrary_javascript(self):
        with self.assertRaisesRegex(WorkflowProposalError, "source is forbidden"):
            validate_workflow_descriptor("export default agent({})", [])

    def test_accepts_compiled_graph_bound_to_descriptor(self):
        self.assertEqual(
            validate_compiled_graph(
                compiled_graph(), descriptor(), ["frappe.invoice.read"]
            )["entryNodeId"],
            "n1-inspect",
        )

    def test_rejects_compiled_graph_capability_mismatch(self):
        unsafe = copy.deepcopy(compiled_graph())
        unsafe["nodes"][1]["requestedCapabilities"] = []
        with self.assertRaisesRegex(WorkflowProposalError, "capability evidence"):
            validate_compiled_graph(unsafe, descriptor(), ["frappe.invoice.read"])

    def test_rejects_compiled_graph_raw_cycle(self):
        unsafe = copy.deepcopy(compiled_graph())
        unsafe["edges"].append({"from": "n7-verify", "to": "n1-inspect"})
        with self.assertRaisesRegex(WorkflowProposalError, "raw cycle"):
            validate_compiled_graph(unsafe, descriptor(), ["frappe.invoice.read"])

    def test_rejects_capability_escalation(self):
        unsafe = copy.deepcopy(descriptor())
        unsafe["steps"][0]["branches"][0]["capabilities"] = ["frappe.invoice.write"]
        with self.assertRaisesRegex(WorkflowProposalError, "exceeds caller capability"):
            validate_workflow_descriptor(unsafe, ["frappe.invoice.read"])

    def test_rejects_oversized_descriptor(self):
        unsafe = copy.deepcopy(descriptor())
        unsafe["goal"] = "x" * 1_000_001
        with self.assertRaisesRegex(WorkflowProposalError, "safe size"):
            validate_workflow_descriptor(unsafe, ["frappe.invoice.read"])

    def test_rejects_unknown_fields(self):
        unsafe = copy.deepcopy(descriptor())
        unsafe["javascript"] = "process.exit()"
        with self.assertRaisesRegex(WorkflowProposalError, "unknown field"):
            validate_workflow_descriptor(unsafe, ["frappe.invoice.read"])

    def test_rejects_excessive_valid_budget(self):
        unsafe = copy.deepcopy(descriptor())
        unsafe["budget"]["runtimeMs"] = 999_999_999
        with self.assertRaisesRegex(WorkflowProposalError, "safe planning ceiling"):
            validate_workflow_descriptor(unsafe, ["frappe.invoice.read"])

    def test_untrusted_runtime_fails_before_planning_request(self):
        frappe.set_user("Administrator")
        with patch(
            "muster.orchestration.workflow_proposal.trusted_binding",
            side_effect=GatewayClientError("Muster gateway trust is not active"),
        ):
            with self.assertRaisesRegex(GatewayClientError, "trust is not active"):
                request_workflow_proposal(
                    "Review overdue invoices", {"route": "List/Sales Invoice"}, "untrusted-plan-idempotency"
                )

    def test_approved_proposal_materializes_and_publishes_without_execution(self):
        frappe.set_user("Administrator")
        suffix = uuid4().hex[:10]
        policy = frappe.get_doc({
            "doctype": "Muster Policy",
            "policy_name": f"Proposal policy {suffix}",
            "enabled": 1,
            "rules": [{
                "effect": "Allow", "capability": "frappe.invoice.read",
                "action": "read", "resource_type": "Site", "resource_pattern": "*",
                "approval_class": "None",
            }],
        }).insert()
        agent = frappe.get_doc({
            "doctype": "Muster Agent",
            "agent_name": f"Proposal supervisor {suffix}",
            "status": "Active", "agent_type": "Supervisor",
            "description": "Test proposal materialization", "policy": policy.name,
            "instructions": "Execute only the admitted workflow graph.",
            "max_depth": 4, "max_fan_out": 4, "max_tool_calls": 8,
            "capabilities": [{
                "capability": "frappe.invoice.read", "resource_pattern": "*",
                "risk_class": "Low", "requires_approval": 0,
            }],
        }).insert()
        proposal_ir = descriptor()
        graph = compiled_graph()
        descriptor_json = json.dumps(proposal_ir, ensure_ascii=False, indent=2, sort_keys=True)
        graph_json = json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True)
        proposal = frappe.get_doc({
            "doctype": "Muster Workflow Proposal",
            "objective": proposal_ir["goal"], "status": "Approved",
            "requested_by": "Administrator", "requested_at": frappe.utils.now_datetime(),
            "request_id": f"proposal-request-{suffix}",
            "gateway_request_id": f"gateway-request-{suffix}",
            "reviewed_by": "Administrator", "reviewed_at": frappe.utils.now_datetime(),
            "requested_scope_json": json.dumps({"doctype": "Muster Mission", "docname": "scope-proof"}),
            "requested_scope_hash": sha256(json.dumps({"doctype": "Muster Mission", "docname": "scope-proof"}, separators=(",", ":"), sort_keys=True).encode()).hexdigest(),
            "context_json": "{}", "capabilities_json": '["frappe.invoice.read"]',
            "descriptor_json": descriptor_json,
            "descriptor_hash": sha256(json.dumps(proposal_ir, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest(),
            "compiled_graph_json": graph_json,
            "compiled_graph_hash": sha256(json.dumps(graph, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest(),
        }).insert()
        with patch(
            "muster.orchestration.workflow_proposal._caller_capabilities",
            return_value=["frappe.invoice.read"],
        ):
            published = publish_approved_proposal(
                proposal.name, agent.name, policy.name, f"publish-{suffix}"
            )
        self.assertEqual(published["status"], "Published")
        self.assertFalse(published["executed"])
        version = frappe.get_doc("Muster Workflow Version", published["version"])
        self.assertEqual(version.docstatus, 1)
        compiled = json.loads(version.graph_json)
        self.assertEqual(compiled["entryNodeId"], "n1-inspect")
        self.assertTrue(all(
            node.get("agentId") == agent.name
            for node in compiled["nodes"] if node["kind"] == "agent"
        ))
        replayed = publish_approved_proposal(
            proposal.name, agent.name, policy.name, "different-key-is-still-replay"
        )
        self.assertTrue(replayed["replayed"])

        mission_key = f"start-proposal-{suffix}"
        with self.assertRaisesRegex(WorkflowProposalError, "Explicit Start confirmation"):
            start_published_proposal_mission(
                proposal.name, mission_key, confirmed=0
            )
        self.assertFalse(frappe.db.exists("Muster Mission", {"idempotency_key": mission_key}))

        with (
            patch(
                "muster.orchestration.gateway_runtime._caller_capabilities",
                return_value=["frappe.invoice.read"],
            ),
            patch(
                "muster.orchestration.workflow_proposal._caller_capabilities",
                return_value=["frappe.invoice.read"],
            ),
            patch("muster.orchestration.workflow_proposal.frappe.enqueue") as enqueue,
        ):
            started = start_published_proposal_mission(
                proposal.name, mission_key, confirmed=1
            )
            replayed_start = start_published_proposal_mission(
                proposal.name, mission_key, confirmed="1"
            )
        self.assertFalse(started["replayed"])
        self.assertTrue(replayed_start["replayed"])
        enqueue.assert_called_once()
        mission = frappe.get_doc("Muster Mission", started["mission"])
        self.assertEqual(mission.status, "Queued")
        self.assertEqual(mission.requested_by, "Administrator")
        self.assertEqual(mission.source_proposal, proposal.name)
        self.assertEqual(mission.workflow, published["workflow"])
        self.assertEqual(mission.workflow_version, published["version"])
        self.assertEqual(mission.root_agent, agent.name)
        self.assertEqual(json.loads(mission.scope_json), {"doctype": "Muster Mission", "docname": "scope-proof"})
        mission.workflow_version = None
        with self.assertRaisesRegex(frappe.ValidationError, "reviewed plan is immutable"):
            mission.save()
        mission.reload()

        original_hash = proposal.descriptor_hash
        proposal.db_set("descriptor_hash", "0" * 64, update_modified=False)
        with (
            patch(
                "muster.orchestration.workflow_proposal._caller_capabilities",
                return_value=["frappe.invoice.read"],
            ),
            self.assertRaisesRegex(WorkflowProposalError, "descriptor hash"),
        ):
            start_published_proposal_mission(
                proposal.name, f"tampered-{suffix}", confirmed=1
            )
        proposal.db_set("descriptor_hash", original_hash, update_modified=False)

        original_scope_hash = proposal.requested_scope_hash
        proposal.db_set("requested_scope_hash", "0" * 64, update_modified=False)
        with (
            patch(
                "muster.orchestration.workflow_proposal._caller_capabilities",
                return_value=["frappe.invoice.read"],
            ),
            self.assertRaisesRegex(WorkflowProposalError, "scope hash"),
        ):
            start_published_proposal_mission(
                proposal.name, f"tampered-scope-{suffix}", confirmed=1
            )
        proposal.db_set("requested_scope_hash", original_scope_hash, update_modified=False)

        policy.db_set("enabled", 0, update_modified=False)
        with (
            patch(
                "muster.orchestration.workflow_proposal._caller_capabilities",
                return_value=["frappe.invoice.read"],
            ),
            self.assertRaisesRegex(WorkflowProposalError, "policy is not currently active"),
        ):
            start_published_proposal_mission(
                proposal.name, f"disabled-policy-{suffix}", confirmed=1
            )
        policy.db_set("enabled", 1, update_modified=False)

        with (
            patch(
                "muster.orchestration.workflow_proposal._caller_capabilities",
                return_value=[],
            ),
            self.assertRaisesRegex(WorkflowProposalError, "exceeds caller capability"),
        ):
            start_published_proposal_mission(
                proposal.name, f"revoked-authority-{suffix}", confirmed=1
            )

        frappe.set_user("Guest")
        try:
            with self.assertRaises(frappe.PermissionError):
                start_published_proposal_mission(
                    proposal.name, f"wrong-actor-{suffix}", confirmed=1
                )
        finally:
            frappe.set_user("Administrator")
