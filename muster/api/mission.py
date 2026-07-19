from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from muster.change_ir.executor import apply_document, preflight as preflight_change_set
from muster.change_ir.schema import ChangeSet
from muster.orchestration.service import create_mission, request_control
from muster.orchestration.workflow_proposal import (
    publish_approved_proposal,
    request_workflow_proposal,
    start_published_proposal_mission,
    validate_workflow_descriptor,
)


def _require_post() -> None:
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(_("This endpoint only accepts POST requests"), frappe.PermissionError)


def _idempotency_key() -> str:
    key = frappe.get_request_header("Idempotency-Key") or frappe.form_dict.get("idempotency_key")
    if not key or len(key) > 140:
        frappe.throw(_("A valid Idempotency-Key is required"), frappe.ValidationError)
    return key


@frappe.whitelist()
def start(objective: str, workflow: str | None = None, scope: str | None = None) -> dict[str, Any]:
    _require_post()
    return create_mission(
        objective=objective,
        workflow=workflow,
        scope=json.loads(scope) if isinstance(scope, str) and scope else (scope or {}),
        idempotency_key=_idempotency_key(),
    )


@frappe.whitelist()
def plan(objective: str, scope: str | dict | None = None) -> dict[str, Any]:
    """Ask the trusted gateway for an inert, reviewable workflow proposal."""
    _require_post()
    try:
        parsed_scope = json.loads(scope) if isinstance(scope, str) and scope else (scope or {})
    except (TypeError, ValueError) as error:
        frappe.throw(_("Planning scope must be valid JSON"), frappe.ValidationError)
        raise error  # unreachable; keeps type checkers precise
    return request_workflow_proposal(objective, parsed_scope, _idempotency_key())


@frappe.whitelist()
def review_proposal(proposal: str, action: str) -> dict[str, Any]:
    """Record review only. Approval deliberately does not publish or execute."""
    _require_post()
    _idempotency_key()
    roles = set(frappe.get_roles())
    if frappe.session.user != "Administrator" and not roles.intersection(
        {"System Manager", "Muster Administrator", "Muster Automation Manager"}
    ):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if action not in {"approve", "reject"}:
        frappe.throw(_("Review action must be approve or reject"), frappe.ValidationError)
    doc = frappe.get_doc("Muster Workflow Proposal", proposal)
    if not doc.has_permission("write"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if doc.status != "Proposed":
        frappe.throw(_("Only a proposed workflow can be reviewed"), frappe.ValidationError)
    # Re-validate the immutable snapshot and original maximum authority at the
    # decision boundary. Publication will perform live authority checks again.
    validate_workflow_descriptor(json.loads(doc.descriptor_json), json.loads(doc.capabilities_json))
    doc.db_set({
        "status": "Approved" if action == "approve" else "Rejected",
        "reviewed_by": frappe.session.user,
        "reviewed_at": frappe.utils.now_datetime(),
    }, update_modified=True)
    return {"proposal": doc.name, "status": doc.status, "executed": False}


@frappe.whitelist()
def publish_proposal(proposal: str, root_agent: str, policy: str) -> dict[str, Any]:
    """Create and publish a native, versioned workflow from approved inert IR."""
    _require_post()
    return publish_approved_proposal(
        proposal, root_agent, policy, _idempotency_key()
    )


@frappe.whitelist()
def start_proposal(proposal: str, confirmed: int | str = 0) -> dict[str, Any]:
    """Explicitly start a Mission pinned to a published proposal snapshot."""
    _require_post()
    return start_published_proposal_mission(
        proposal, _idempotency_key(), confirmed=confirmed
    )


@frappe.whitelist()
def control(mission: str, action: str, note: str | None = None) -> dict[str, Any]:
    _require_post()
    return request_control(mission, action, note, _idempotency_key())


@frappe.whitelist()
def preflight(change_set: dict | str) -> dict[str, Any]:
    _require_post()
    payload = json.loads(change_set) if isinstance(change_set, str) else change_set
    compiled = ChangeSet.from_dict(payload)
    return preflight_change_set(compiled)


@frappe.whitelist()
def apply(change_set: str) -> dict[str, Any]:
    _require_post()
    _idempotency_key()
    return apply_document(change_set)


@frappe.whitelist()
def activities(mission: str, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    if not frappe.has_permission("Muster Mission", "read", mission):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    limit = min(max(cint(limit), 1), 200)
    return frappe.get_all(
        "Muster Activity",
        filters={"mission": mission, "sequence": [">", cint(after_sequence)]},
        fields=[
            "name", "sequence", "event_type", "state", "summary",
            "actor", "agent", "reference_doctype", "reference_name",
            "payload_json", "creation",
        ],
        order_by="sequence asc",
        limit=limit,
    )


@frappe.whitelist()
def sync(mission: str) -> dict[str, Any]:
    """Fetch and durably project the authenticated gateway mission snapshot."""
    from muster.orchestration.gateway_runtime import poll_and_project

    return poll_and_project(mission)
