from __future__ import annotations

import json
from hashlib import sha256
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from muster.demo.native_desk_rbac_evidence import EVIDENCE_ROLES, _proposal_evidence, capture
from muster.orchestration.workflow_proposal import WorkflowProposalError


class TestNativeDeskRbacEvidence(IntegrationTestCase):
    def test_maker_uses_ordinary_erpnext_authority_not_system_manager(self):
        self.assertEqual(set(EVIDENCE_ROLES["maker"]), {"Muster Operator", "Sales Manager"})
        self.assertNotIn("System Manager", EVIDENCE_ROLES["maker"])

    def test_capture_is_read_only_and_server_sealed(self):
        cases = [
            {"proposal": "MST-WFP-U", "operation": "update", "executed": False},
            {"proposal": "MST-WFP-D", "operation": "delete", "executed": False},
        ]
        with (
            patch("muster.demo.native_desk_rbac_evidence.frappe.session", frappe._dict(user="Administrator")),
            patch("muster.demo.native_desk_rbac_evidence._proposal_evidence", side_effect=cases) as evidence,
        ):
            result = capture("MST-WFP-U", "MST-WFP-D", "auditor@example.test", True)
        evidence.assert_any_call("MST-WFP-U", "update", "auditor@example.test")
        evidence.assert_any_call("MST-WFP-D", "delete", "auditor@example.test")
        self.assertTrue(result["read_only"])
        self.assertTrue(all(item["executed"] is False for item in result["cases"]))
        seal = result.pop("evidence_sha256")
        canonical = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.assertEqual(seal, sha256(canonical.encode()).hexdigest())

    def test_capture_requires_administrator_and_explicit_confirmation(self):
        with patch("muster.demo.native_desk_rbac_evidence.frappe.session", frappe._dict(user="maker@example.test")):
            with self.assertRaises(frappe.PermissionError):
                capture("MST-WFP-U", "MST-WFP-D", confirm=True)
        with patch("muster.demo.native_desk_rbac_evidence.frappe.session", frappe._dict(user="Administrator")):
            with self.assertRaises(frappe.ValidationError):
                capture("MST-WFP-U", "MST-WFP-D", confirm=False)

    def test_update_probe_requires_distinct_identities_and_rejects_stale_revision(self):
        proposal = frappe._dict(
            name="MST-WFP-U", status="Approved", requested_by="maker@example.test",
            reviewed_by="checker@example.test", descriptor_hash="a" * 64,
            compiled_graph_hash="b" * 64,
        )
        preview = {
            "proposal": proposal.name, "operation": "update", "doctype": "Customer",
            "record_name": "DISPOSABLE-U", "record_revision": "rev-1",
        }
        current = {
            **preview, "current": True, "executed": False,
            "fields": [{"fieldname": "customer_name", "label": "Customer Name", "control": "fill", "value": "Acme"}],
        }

        def reviewer(_proposal, user):
            if user == "maker@example.test":
                raise frappe.PermissionError("different reviewer required")

        def preview_for(_name, user):
            if user != "maker@example.test":
                raise frappe.PermissionError("requester only")
            return preview

        def preflight(_name, _user, _record, revision):
            if revision != "rev-1":
                raise WorkflowProposalError("stale")
            return current

        with (
            patch("muster.demo.native_desk_rbac_evidence.frappe.get_doc", return_value=proposal),
            patch("muster.demo.native_desk_rbac_evidence.proposal_attended_operation", return_value="update"),
            patch("muster.demo.native_desk_rbac_evidence.assert_attended_reviewer", side_effect=reviewer),
            patch("muster.demo.native_desk_rbac_evidence.attended_proposal_preview", side_effect=preview_for),
            patch("muster.demo.native_desk_rbac_evidence.preflight_attended_proposal_save", side_effect=preflight),
        ):
            result = _proposal_evidence(proposal.name, "update", None)
        self.assertTrue(result["maker_self_approval_denied"])
        self.assertTrue(result["checker_preview_denied"])
        self.assertTrue(result["stale_revision_denied"])
        self.assertEqual(result["reviewed_field_names"], ["customer_name"])
        self.assertFalse(result["executed"])

    def test_delete_probe_rejects_stale_approval_binding_without_execution(self):
        proposal = frappe._dict(
            name="MST-WFP-D", status="Approved", requested_by="maker@example.test",
            reviewed_by="checker@example.test", descriptor_hash="a" * 64,
            compiled_graph_hash="b" * 64,
        )
        preview = {
            "proposal": proposal.name, "operation": "delete", "doctype": "Customer",
            "record_name": "DISPOSABLE-D", "record_revision": "rev-1", "approval_proof": "c" * 64,
        }

        def reviewer(_proposal, user):
            if user == "maker@example.test":
                raise frappe.PermissionError("different reviewer required")

        def preview_for(_name, user):
            if user != "maker@example.test":
                raise frappe.PermissionError("requester only")
            return preview

        def delete_revision(_name, _user, _record, revision, _proof):
            if revision != "rev-1":
                raise WorkflowProposalError("stale")
            return {"current": True, "executed": False}

        with (
            patch("muster.demo.native_desk_rbac_evidence.frappe.get_doc", return_value=proposal),
            patch("muster.demo.native_desk_rbac_evidence.proposal_attended_operation", return_value="delete"),
            patch("muster.demo.native_desk_rbac_evidence.assert_attended_reviewer", side_effect=reviewer),
            patch("muster.demo.native_desk_rbac_evidence.attended_proposal_preview", side_effect=preview_for),
            patch("muster.demo.native_desk_rbac_evidence.assert_attended_delete_revision", side_effect=delete_revision),
        ):
            result = _proposal_evidence(proposal.name, "delete", None)
        self.assertTrue(result["stale_revision_denied"])
        self.assertIsNone(result["reviewed_values_sha256"])
        self.assertFalse(result["executed"])
