from __future__ import annotations

import unittest
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from muster.automation import (
    ApprovalEvidence,
    ArtifactChangeSet,
    GovernanceContext,
    apply,
    preview,
    rollback,
)
from muster.automation.models import (
    AutomationConflictError,
    AutomationPermissionError,
    AutomationValidationError,
    plan_from_dict,
)


class MemoryBackend:
    site = "test.local"

    def __init__(self):
        self.records = {}
        self.revisions = {}
        self.receipts = {}
        self.executions = {}
        self.enabled = {"agent@example.com"}
        self.fail_on = None

    def actor_enabled(self, actor):
        return actor in self.enabled

    def has_permission(self, actor, doctype, permission, name=None):
        return actor in self.enabled

    def snapshot(self, doctype, name, fields):
        record = self.records.get((doctype, name))
        if record is None:
            return None, None
        return {field: deepcopy(record.get(field)) for field in fields}, str(self.revisions[(doctype, name)])

    def insert(self, doctype, name, values):
        if self.fail_on == name:
            raise RuntimeError("injected failure")
        if (doctype, name) in self.records:
            raise RuntimeError("duplicate")
        self.records[(doctype, name)] = deepcopy(dict(values))
        self.revisions[(doctype, name)] = 1
        return name

    def update(self, doctype, name, values):
        if self.fail_on == name:
            raise RuntimeError("injected failure")
        self.records[(doctype, name)].update(deepcopy(dict(values)))
        self.revisions[(doctype, name)] += 1

    def delete(self, doctype, name):
        self.records.pop((doctype, name), None)
        self.revisions.pop((doctype, name), None)

    def lock(self, key):
        return nullcontext()

    def find_receipt(self, key):
        receipt = self.receipts.get(key)
        if not receipt or self.executions[receipt["execution_id"]]["status"] != "Verified":
            return None
        return deepcopy(receipt)

    def begin_execution(self, plan):
        name = f"execution-{len(self.executions) + 1}"
        self.executions[name] = {"status": "Applying", "plan": plan}
        return name

    def record_receipt(self, execution_id, change, receipt):
        self.receipts[change.idempotency_key] = deepcopy(dict(receipt))

    def finish_execution(self, execution_id, status, *, inverses, evidence, repairs=None):
        self.executions[execution_id].update({"status": status, "inverses": deepcopy(inverses),
                                              "evidence": deepcopy(dict(evidence)),
                                              "repairs": deepcopy(repairs)})

    def resolve_trusted_artifact(self, kind, key):
        definitions = {
            ("script_report", "sales-safe-v1"): {
                "report_script": "result = trusted_execute(filters)", "module": "Muster"
            },
            ("print_format", "invoice-safe-v1"): {"html": "<h1>{{ doc.name }}</h1>"},
        }
        if (kind, key) not in definitions:
            raise AutomationValidationError("not installed")
        return definitions[(kind, key)]


def manifest(kind, name, values, *, target_doctype=None, module="Muster", suffix=None):
    suffix = suffix or name.lower().replace(" ", "-")
    return {
        "artifact_id": f"artifact-{suffix}", "kind": kind, "target_name": name,
        "idempotency_key": f"idempotency-{suffix}", "target_doctype": target_doctype,
        "module": module, "values": values,
    }


def changeset(*artifacts):
    return ArtifactChangeSet.from_dict({
        "schema_version": "1.0", "target_site": "test.local",
        "actor": "agent@example.com", "mission": "MST-MSN-00001",
        "artifacts": list(artifacts),
    })


def approval(plan, approval_class=None):
    now = datetime.now(timezone.utc)
    return ApprovalEvidence(
        plan_hash=plan.plan_hash, approval_class=approval_class or plan.approval_class,
        requested_by="agent@example.com", decided_by="approver@example.com",
        decided_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        approver_roles=frozenset({"Muster Approver"}),
    )


class NativeArtifactBuilderTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend()
        self.governance = GovernanceContext.from_values({"artifact.*"})

    def test_all_native_surfaces_compile_to_a_deterministic_reviewable_plan(self):
        artifacts = [
            manifest("custom_field", "customer_tier", {"label": "Tier", "fieldtype": "Select", "options": "A\nB"}, target_doctype="Customer"),
            manifest("property_setter", "Customer-tier-hidden", {"field_name": "customer_tier", "property": "hidden", "value": "0", "property_type": "Check"}, target_doctype="Customer", suffix="setter"),
            manifest("doctype", "Muster Visit", {"fields": [{"fieldname": "subject", "label": "Subject", "fieldtype": "Data", "reqd": 1}], "permissions": [{"role": "System Manager", "read": 1}]}, suffix="doctype"),
            manifest("page", "muster-operations", {"title": "Operations", "roles": [{"role": "System Manager"}]}, suffix="page"),
            manifest("query_report", "Open Opportunities", {"source_doctype": "Opportunity", "fields": ["name", "status"], "filters": [{"fieldname": "status", "operator": "=", "parameter": "status"}], "order_by": [{"fieldname": "name", "direction": "desc"}], "limit": 250}, target_doctype="Opportunity", suffix="query"),
            manifest("script_report", "Governed Sales", {"implementation_key": "sales-safe-v1", "ref_doctype": "Sales Order"}, target_doctype="Sales Order", suffix="script"),
            manifest("print_format", "Safe Invoice", {"doc_type": "Sales Invoice", "html": "<h1>{{ doc.name }}</h1>"}, target_doctype="Sales Invoice", suffix="print"),
            manifest("web_page", "muster-about", {"title": "About", "route": "muster/about", "main_section": "<h1>Safe</h1>", "published": 1}, suffix="webpage"),
            manifest("web_form", "muster-request", {"doc_type": "Issue", "title": "Request", "route": "request", "web_form_fields": [{"fieldname": "subject", "label": "Subject", "fieldtype": "Data", "reqd": 1}]}, target_doctype="Issue", suffix="webform"),
            manifest("office_artifact", "artifact-file-1", {"mission": "MST-MSN-00001", "file_url": "/private/files/board-pack.pdf", "mime_type": "application/pdf", "size_bytes": 1200, "checksum": "a" * 64, "title": "Board pack"}, suffix="office"),
        ]
        plan = preview(changeset(*artifacts), self.backend, self.governance)
        self.assertEqual(len(plan.changes), 10)
        self.assertEqual(plan.approval_class, "Privileged Code")
        query = next(item for item in plan.changes if item.kind == "query_report")
        self.assertIn("FROM `tabOpportunity`", query.after["query"])
        self.assertNotIn("query", artifacts[4]["values"])
        self.assertEqual(preview(plan.source, self.backend, self.governance).plan_hash, plan.plan_hash)

    def test_untrusted_code_and_active_html_are_rejected_before_permissions_or_effects(self):
        bad = manifest("web_page", "unsafe-page", {
            "title": "Unsafe", "route": "unsafe", "main_section": '<img src=x onerror="alert(1)">'
        }, suffix="unsafe")
        with self.assertRaisesRegex(AutomationValidationError, "active HTML"):
            preview(changeset(bad), self.backend, self.governance)
        raw_script = manifest("script_report", "raw-script", {
            "implementation_key": "missing-key", "ref_doctype": "User", "report_script": "frappe.db.sql('x')"
        }, target_doctype="User", suffix="raw-script")
        with self.assertRaisesRegex(AutomationValidationError, "unsupported fields"):
            preview(changeset(raw_script), self.backend, self.governance)

    def test_policy_is_deny_by_default_and_intersects_frappe_permission(self):
        item = manifest("custom_field", "customer_tier", {"label": "Tier", "fieldtype": "Data"}, target_doctype="Customer")
        with self.assertRaisesRegex(AutomationPermissionError, "capability not granted"):
            preview(changeset(item), self.backend, GovernanceContext.from_values(set()))
        denied = GovernanceContext.from_values({"artifact.*"}, denied_capabilities={"artifact.custom_field.*"})
        with self.assertRaisesRegex(AutomationPermissionError, "capability not granted"):
            preview(changeset(item), self.backend, denied)
        self.backend.has_permission = lambda *args, **kwargs: False
        with self.assertRaisesRegex(AutomationPermissionError, "Frappe write permission denied"):
            preview(changeset(item), self.backend, self.governance)

    def test_apply_requires_bound_separated_approval_and_is_idempotent(self):
        item = manifest("custom_field", "customer_tier", {"label": "Tier", "fieldtype": "Data"}, target_doctype="Customer")
        plan = preview(changeset(item), self.backend, self.governance)
        with self.assertRaises(AutomationPermissionError):
            apply(plan, self.backend, self.governance)
        evidence = apply(plan, self.backend, self.governance, approval(plan))
        self.assertEqual(evidence.status, "Verified")
        self.assertIn(("Custom Field", "Customer-customer_tier"), self.backend.records)
        replay = apply(plan, self.backend, self.governance, approval(plan))
        self.assertTrue(replay.receipts[0]["replayed"])
        self.assertEqual(len(self.backend.executions), 1)

    def test_concurrency_conflict_is_detected_without_effect(self):
        key = ("Custom Field", "Customer-customer_tier")
        self.backend.records[key] = {"dt": "Customer", "fieldname": "customer_tier", "label": "Old", "fieldtype": "Data"}
        self.backend.revisions[key] = 1
        item = manifest("custom_field", "customer_tier", {"label": "New", "fieldtype": "Data"}, target_doctype="Customer")
        plan = preview(changeset(item), self.backend, self.governance)
        self.backend.records[key]["label"] = "User edit"
        self.backend.revisions[key] = 2
        with self.assertRaises(AutomationConflictError):
            apply(plan, self.backend, self.governance, approval(plan))
        self.assertEqual(self.backend.records[key]["label"], "User edit")

    def test_failed_change_set_compensates_prior_effect_and_records_repair(self):
        first = manifest("custom_field", "field_one", {"label": "One", "fieldtype": "Data"}, target_doctype="Customer", suffix="one")
        second = manifest("custom_field", "field_two", {"label": "Two", "fieldtype": "Data"}, target_doctype="Customer", suffix="two")
        plan = preview(changeset(first, second), self.backend, self.governance)
        self.backend.fail_on = "Customer-field_two"
        evidence = apply(plan, self.backend, self.governance, approval(plan))
        self.assertEqual(evidence.status, "Repaired")
        self.assertNotIn(("Custom Field", "Customer-field_one"), self.backend.records)
        execution = next(iter(self.backend.executions.values()))
        self.assertEqual(execution["status"], "Repaired")
        self.assertEqual(execution["repairs"][0]["status"], "Repaired")

    def test_explicit_rollback_requires_destructive_approval_and_preserves_evidence(self):
        item = manifest("custom_field", "customer_tier", {"label": "Tier", "fieldtype": "Data"}, target_doctype="Customer")
        plan = preview(changeset(item), self.backend, self.governance)
        executed = apply(plan, self.backend, self.governance, approval(plan))
        with self.assertRaisesRegex(AutomationPermissionError, "Destructive"):
            rollback(plan, executed, self.backend, self.governance, approval(plan))
        destructive = approval(plan, "Destructive")
        result = rollback(plan, executed, self.backend, self.governance, destructive)
        self.assertEqual(result.status, "Rolled Back")
        self.assertNotIn(("Custom Field", "Customer-customer_tier"), self.backend.records)
        self.assertEqual(result.repairs[0]["status"], "Repaired")

    def test_manifest_rejects_duplicate_idempotency_keys(self):
        first = manifest("custom_field", "field_one", {"label": "One", "fieldtype": "Data"}, target_doctype="Customer", suffix="same")
        second = manifest("custom_field", "field_two", {"label": "Two", "fieldtype": "Data"}, target_doctype="Customer", suffix="same")
        second["artifact_id"] = "artifact-different"
        with self.assertRaisesRegex(AutomationValidationError, "idempotency_key values must be unique"):
            changeset(first, second)

    def test_serialized_plan_is_hash_bound_and_rejects_tampering(self):
        item = manifest("custom_field", "customer_tier", {"label": "Tier", "fieldtype": "Data"}, target_doctype="Customer")
        plan = preview(changeset(item), self.backend, self.governance)
        serialized = plan.as_dict()
        self.assertEqual(plan_from_dict(serialized).plan_hash, plan.plan_hash)
        serialized["changes"][0]["after"]["label"] = "Tampered"
        with self.assertRaisesRegex(AutomationValidationError, "plan hash"):
            plan_from_dict(serialized)


if __name__ == "__main__":
    unittest.main()
