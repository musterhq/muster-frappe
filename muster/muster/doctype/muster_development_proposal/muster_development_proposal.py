from __future__ import annotations

import hashlib
import json

import frappe
from frappe.model.document import Document


class MusterDevelopmentProposal(Document):
    def validate(self):
        immutable = (
            "ask_turn", "app", "policy", "requested_by", "requested_at", "request_id",
            "objective_hash", "source_revision", "source_status_hash", "allowed_paths_json",
            "allowed_paths_hash", "policy_revision_hash",
        )
        if not self.is_new() and any(self.has_value_changed(field) for field in immutable):
            frappe.throw("Reviewed development proposal evidence is immutable")
        try:
            paths = json.loads(self.allowed_paths_json or "[]")
        except (TypeError, ValueError) as error:
            frappe.throw("Reviewed development paths are invalid", frappe.ValidationError)
            raise error
        canonical = json.dumps(paths, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not isinstance(paths, list) or hashlib.sha256(canonical.encode()).hexdigest() != self.allowed_paths_hash:
            frappe.throw("Reviewed development path evidence does not match", frappe.ValidationError)
        objective = self.objective_secret if self.is_new() else self.get_password("objective_secret", raise_exception=False)
        if not objective or hashlib.sha256(objective.encode()).hexdigest() != self.objective_hash:
            frappe.throw("Reviewed development objective evidence does not match", frappe.ValidationError)

