from __future__ import annotations

import json
from hashlib import sha256
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

import frappe
from frappe.utils import now_datetime

from muster.automation.models import NativeChange, Plan, canonical_json, digest
from muster.change_ir.security import permission_epoch, schema_revision


def _json_value(value: Any) -> Any:
    """Convert Frappe child rows/dates to the same plain values used in a manifest."""
    return json.loads(frappe.as_json(value))


class FrappeNativeBackend:
    """Frappe adapter; all effects go through Documents and their lifecycle hooks."""

    @property
    def site(self) -> str:
        return frappe.local.site

    def actor_enabled(self, actor: str) -> bool:
        return bool(frappe.db.get_value("User", actor, "enabled"))

    def has_permission(self, actor: str, doctype: str, permission: str,
                       name: str | None = None) -> bool:
        try:
            if not frappe.db.exists("DocType", doctype):
                return False
            if name and frappe.db.exists(doctype, name):
                return bool(frappe.get_doc(doctype, name).has_permission(permission, user=actor))
            return bool(frappe.has_permission(doctype, permission, user=actor))
        except (frappe.DoesNotExistError, frappe.PermissionError):
            return False

    def snapshot(self, doctype: str, name: str,
                 fields: tuple[str, ...]) -> tuple[dict[str, Any] | None, str | None]:
        if not frappe.db.exists(doctype, name):
            return None, None
        doc = frappe.get_doc(doctype, name)
        snapshot = {field: _json_value(doc.get(field)) for field in fields if field != "name"}
        if "name" in fields:
            snapshot["name"] = doc.name
        return snapshot, str(doc.modified)

    def insert(self, doctype: str, name: str, values: Mapping[str, Any]) -> str:
        payload = dict(values)
        payload.pop("doctype", None)
        payload.pop("modified", None)
        payload.setdefault("name", name)
        return frappe.get_doc({"doctype": doctype, **payload}).insert().name

    def update(self, doctype: str, name: str, values: Mapping[str, Any]) -> None:
        payload = dict(values)
        payload.pop("doctype", None)
        payload.pop("name", None)
        payload.pop("modified", None)
        doc = frappe.get_doc(doctype, name)
        doc.update(payload)
        doc.save()

    def delete(self, doctype: str, name: str) -> None:
        frappe.get_doc(doctype, name).delete()

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        # Redis locks work across web workers and background workers for the site.
        with frappe.cache.lock(key, timeout=120, blocking_timeout=15):
            yield

    def resolve_trusted_artifact(self, kind: str, key: str) -> Mapping[str, Any]:
        configured = frappe.get_hooks("muster_trusted_artifact_builders") or {}
        if not isinstance(configured, dict):
            frappe.throw("muster_trusted_artifact_builders must be a hook mapping", frappe.ValidationError)
        dotted = configured.get(f"{kind}.{key}")
        if isinstance(dotted, (list, tuple)):
            dotted = dotted[-1] if dotted else None
        if not isinstance(dotted, str):
            frappe.throw("trusted artifact implementation is not installed", frappe.ValidationError)
        definition = frappe.get_attr(dotted)()
        if not isinstance(definition, Mapping):
            frappe.throw("trusted artifact implementation returned an invalid definition", frappe.ValidationError)
        return definition

    def find_receipt(self, idempotency_key: str) -> Mapping[str, Any] | None:
        rows = frappe.get_all(
            "Muster Change Operation",
            filters={"idempotency_key": idempotency_key, "receipt_json": ["is", "set"]},
            fields=["receipt_json", "parent"], order_by="modified desc", limit=20,
        )
        for row in rows:
            if (row.receipt_json and
                    frappe.db.get_value("Muster Change Set", row.parent, "status") == "Verified"):
                value = json.loads(row.receipt_json)
                return value if isinstance(value, dict) else None
        return None

    def validate_definition(self, definition, change_set) -> None:
        if definition.doctype != "Muster Artifact":
            return
        file_url = definition.values["file"]
        file_name = frappe.db.get_value(
            "File", {"file_url": file_url, "is_private": 1}, "name"
        )
        if not file_name:
            frappe.throw("office artifact does not reference an existing private File",
                         frappe.ValidationError)
        file_doc = frappe.get_doc("File", file_name)
        if not file_doc.has_permission("read", user=change_set.actor):
            frappe.throw("execution actor cannot read the office artifact File",
                         frappe.PermissionError)
        if not frappe.get_doc("Muster Mission", change_set.mission).has_permission(
                "read", user=change_set.actor):
            frappe.throw("execution actor cannot read the artifact Mission",
                         frappe.PermissionError)
        if definition.values.get("work_unit") and not frappe.get_doc(
                "Muster Work Unit", definition.values["work_unit"]).has_permission(
                    "read", user=change_set.actor):
            frappe.throw("execution actor cannot read the artifact Work Unit",
                         frappe.PermissionError)
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if len(content) != int(definition.values["size_bytes"]):
            frappe.throw("office artifact size does not match the private File",
                         frappe.ValidationError)
        if sha256(content).hexdigest() != definition.values["checksum"]:
            frappe.throw("office artifact SHA-256 does not match the private File",
                         frappe.ValidationError)

    def begin_execution(self, plan: Plan) -> str:
        existing = frappe.get_all(
            "Muster Change Set",
            filters={"plan_hash": plan.plan_hash, "mission": plan.source.mission,
                     "actor": plan.source.actor,
                     "status": ["in", ["Preflighted", "Awaiting Approval", "Approved"]]},
            fields=["name"], order_by="creation desc", limit=1,
        )
        if existing:
            doc = frappe.get_doc("Muster Change Set", existing[0].name)
            persisted = json.loads(doc.evidence_json or "{}")
            if persisted.get("kind") != "native_artifact_plan" or persisted.get("plan") != plan.as_dict():
                frappe.throw("persisted native artifact plan evidence does not match",
                             frappe.ValidationError)
            doc.db_set("status", "Applying", update_modified=True)
            return doc.name
        return self._insert_plan(plan, "Applying")

    def persist_preview(self, plan: Plan) -> str:
        existing = frappe.get_all(
            "Muster Change Set",
            filters={"plan_hash": plan.plan_hash, "mission": plan.source.mission,
                     "actor": plan.source.actor,
                     "status": ["in", ["Preflighted", "Awaiting Approval", "Approved"]]},
            fields=["name"], order_by="creation desc", limit=1,
        )
        if existing:
            return existing[0].name
        status = "Preflighted" if plan.approval_class == "None" else "Awaiting Approval"
        return self._insert_plan(plan, status)

    def _insert_plan(self, plan: Plan, status: str) -> str:
        risk = {"None": "Low", "Standard": "Moderate", "Sensitive": "High",
                "Privileged Code": "Critical", "Destructive": "Critical"}[plan.approval_class]
        doc = frappe.get_doc({
            "doctype": "Muster Change Set", "mission": plan.source.mission,
            "status": status, "risk_class": risk, "approval_class": plan.approval_class,
            "target_site": plan.source.target_site, "actor": plan.source.actor,
            "permission_epoch": permission_epoch(plan.source.actor),
            "schema_revision": schema_revision(), "plan_hash": plan.plan_hash,
            "evidence_json": canonical_json({"kind": "native_artifact_plan", "plan": plan.as_dict()}),
            "operations": [{
                "operation_id": change.artifact_id, "operation_type": f"native_{change.kind}",
                "target_doctype": change.target_doctype, "target_name": change.target_name,
                "approval_class": change.approval_class,
                "before_json": canonical_json(change.before or {}),
                "after_json": canonical_json(change.after),
                "concurrency_token": change.before_revision,
                "idempotency_key": change.idempotency_key,
                "postcondition_json": canonical_json({"after_hash": digest(change.after)}),
            } for change in plan.changes],
        }).insert()
        return doc.name

    def record_receipt(self, execution_id: str, change: NativeChange,
                       receipt: Mapping[str, Any]) -> None:
        row = frappe.get_all(
            "Muster Change Operation",
            filters={"parent": execution_id, "parenttype": "Muster Change Set",
                     "operation_id": change.artifact_id},
            fields=["name"], limit=1,
        )
        if not row:
            frappe.throw("native artifact audit row is missing", frappe.ValidationError)
        frappe.db.set_value("Muster Change Operation", row[0].name, "receipt_json",
                            canonical_json(receipt), update_modified=False)

    def finish_execution(self, execution_id: str, status: str, *,
                         inverses: list[dict[str, Any]], evidence: Mapping[str, Any],
                         repairs: list[dict[str, Any]] | None = None) -> None:
        persisted = "Repaired" if status == "Rolled Back" else status
        doc = frappe.get_doc("Muster Change Set", execution_id)
        doc.db_set("inverse_json", canonical_json(inverses), update_modified=False)
        doc.db_set("verification_json", canonical_json(evidence), update_modified=False)
        if repairs is not None:
            doc.db_set("repair_status", canonical_json(repairs), update_modified=False)
        doc.db_set("status", persisted, update_modified=True)
