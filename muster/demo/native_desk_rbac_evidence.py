from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from muster.orchestration.workflow_proposal import (
    WorkflowProposalError,
    _attended_form_catalog,
    _caller_capabilities,
    _canonical_requested_scope,
    _materialize_attended_crud_bundle,
    assert_attended_delete_revision,
    assert_attended_reviewer,
    attended_proposal_preview,
    preflight_attended_proposal_save,
    proposal_attended_operation,
    validate_compiled_graph,
    validate_workflow_descriptor,
)


EVIDENCE_PREFIX = "[Muster Native RBAC]"
EVIDENCE_USERS = {
    "maker": "muster.native.maker@muster.invalid",
    "checker": "muster.native.checker@muster.invalid",
    "denied": "muster.native.denied@muster.invalid",
}
EVIDENCE_ROLES = {
    # The maker must exercise ordinary ERPNext authority. System Manager would
    # bypass the exact role/record boundary this evidence is meant to prove.
    "maker": ("Muster Operator", "Sales Manager"),
    "checker": ("Muster Approver",),
    "denied": ("Muster Auditor",),
}


def _deny_probe(callable_, message: str) -> bool:
    try:
        callable_()
    except (frappe.PermissionError, frappe.ValidationError, WorkflowProposalError):
        return True
    raise WorkflowProposalError(message)


def _proposal_evidence(name: str, expected_operation: str, denied_user: str | None) -> dict[str, Any]:
    proposal = frappe.get_doc("Muster Workflow Proposal", name)
    if proposal.status != "Approved" or proposal_attended_operation(proposal) != expected_operation:
        raise WorkflowProposalError(
            _("{0} must be an approved exact-record {1} proposal").format(name, expected_operation)
        )
    maker = str(proposal.requested_by or "")
    checker = str(proposal.reviewed_by or "")
    if not maker or not checker or maker.lower() == checker.lower():
        raise WorkflowProposalError(_("Live evidence requires different maker and checker users"))
    assert_attended_reviewer(proposal, checker)
    preview = attended_proposal_preview(name, maker)
    if preview.get("operation") != expected_operation or not preview.get("record_name"):
        raise WorkflowProposalError(_("The attended preview lost its exact record identity"))
    checker_preview_denied = _deny_probe(
        lambda: attended_proposal_preview(name, checker),
        _("The checker unexpectedly received the maker's attended preview"),
    )
    maker_self_approval_denied = _deny_probe(
        lambda: assert_attended_reviewer(proposal, maker),
        _("The maker unexpectedly passed the independent-review boundary"),
    )

    if expected_operation == "update":
        current = preflight_attended_proposal_save(
            name, maker, preview["record_name"], preview["record_revision"],
        )
        stale_revision_denied = _deny_probe(
            lambda: preflight_attended_proposal_save(
                name, maker, preview["record_name"], "MUSTER-STALE-REVISION-PROBE",
            ),
            _("A stale update revision unexpectedly passed preflight"),
        )
        field_names = [field["fieldname"] for field in current["fields"]]
        values_hash = sha256(json.dumps(
            [{"fieldname": field["fieldname"], "value": field["value"]} for field in current["fields"]],
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
    else:
        current = assert_attended_delete_revision(
            name, maker, preview["record_name"], preview["record_revision"], preview["approval_proof"],
        )
        stale_revision_denied = _deny_probe(
            lambda: assert_attended_delete_revision(
                name, maker, preview["record_name"], "MUSTER-STALE-REVISION-PROBE", preview["approval_proof"],
            ),
            _("A stale delete revision unexpectedly passed preflight"),
        )
        field_names = []
        values_hash = None

    denied_user_blocked = None
    if denied_user:
        permission = "write" if expected_operation == "update" else "delete"
        denied_user_blocked = not (
            frappe.has_permission(preview["doctype"], "read", doc=preview["record_name"], user=denied_user)
            and frappe.has_permission(preview["doctype"], permission, doc=preview["record_name"], user=denied_user)
        )
        if not denied_user_blocked:
            raise WorkflowProposalError(_("The denied evidence user still has target authority"))

    return {
        "proposal": name,
        "operation": expected_operation,
        "doctype": preview["doctype"],
        "record_name": preview["record_name"],
        "record_revision": preview["record_revision"],
        "maker": maker,
        "checker": checker,
        "maker_checker_distinct": True,
        "maker_self_approval_denied": maker_self_approval_denied,
        "checker_preview_denied": checker_preview_denied,
        "stale_revision_denied": stale_revision_denied,
        "denied_user_blocked": denied_user_blocked,
        "reviewed_field_names": field_names,
        "reviewed_values_sha256": values_hash,
        "descriptor_sha256": proposal.descriptor_hash,
        "compiled_graph_sha256": proposal.compiled_graph_hash,
        "executed": False,
    }


def capture(
    update_proposal: str,
    delete_proposal: str,
    denied_user: str | None = None,
    confirm: bool | int | str = False,
) -> dict[str, Any]:
    """Capture read-only live evidence for exact-record Desk update/delete RBAC."""
    if frappe.session.user != "Administrator":
        frappe.throw(_("Only Administrator can capture native Desk RBAC evidence"), frappe.PermissionError)
    if not cint(confirm):
        frappe.throw(_("Explicit confirmation is required for evidence capture"), frappe.ValidationError)
    cases = [
        _proposal_evidence(update_proposal, "update", denied_user),
        _proposal_evidence(delete_proposal, "delete", denied_user),
    ]
    payload = {
        "schema_version": 1,
        "kind": "muster.native_desk.exact_record_rbac",
        "site": str(getattr(frappe.local, "site", "") or ""),
        "captured_at": str(now_datetime()),
        "read_only": True,
        "cases": cases,
    }
    payload["evidence_sha256"] = sha256(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    return payload


def _ensure_evidence_user(key: str) -> str:
    from frappe.query_builder import Table

    email = EVIDENCE_USERS[key]
    roles = [role for role in EVIDENCE_ROLES[key] if frappe.db.exists("Role", role)]
    if set(roles) != set(EVIDENCE_ROLES[key]):
        raise WorkflowProposalError(_("The native RBAC evidence roles are not installed"))
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User", "email": email,
            "first_name": f"Muster Native {key.title()}",
            "enabled": 0, "send_welcome_email": 0, "user_type": "System User",
            "bio": f"{EVIDENCE_PREFIX} passwordless disposable evidence persona",
            "roles": [{"role": role} for role in roles],
        }).insert(ignore_permissions=True)
    user = frappe.get_doc("User", email)
    actual_roles = {row.role for row in user.roles}
    auth = Table("__Auth")
    has_secret = bool(
        frappe.qb.from_(auth).select(auth.name)
        .where((auth.doctype == "User") & (auth.name == email)).limit(1).run()
    )
    if user.enabled or user.api_key or has_secret:
        raise WorkflowProposalError(
            _("Existing native evidence persona {0} is not disabled and passwordless with its exact roles").format(email)
        )
    if actual_roles != set(roles):
        if not str(user.bio or "").startswith(EVIDENCE_PREFIX):
            raise WorkflowProposalError(
                _("Existing native evidence persona {0} is not managed by the evidence fixture").format(email)
            )
        user.set("roles", [{"role": role} for role in roles])
        user.save(ignore_permissions=True)
    frappe.clear_cache(user=email)
    return email


def _ensure_customer(label: str) -> str:
    customer_name = f"{EVIDENCE_PREFIX} {label}"
    if name := frappe.db.get_value("Customer", {"customer_name": customer_name}, "name"):
        return str(name)
    customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if not customer_group or not territory:
        raise WorkflowProposalError(_("ERPNext Customer masters are incomplete"))
    return frappe.get_doc({
        "doctype": "Customer", "customer_name": customer_name,
        "customer_type": "Company", "customer_group": customer_group,
        "territory": territory, "disabled": 0,
    }).insert(ignore_permissions=True).name


def _ensure_customer_permission(user: str, record_name: str) -> None:
    filters = {"user": user, "allow": "Customer", "for_value": record_name}
    if not frappe.db.exists("User Permission", filters):
        frappe.get_doc({
            "doctype": "User Permission", **filters, "apply_to_all_doctypes": 1,
        }).insert(ignore_permissions=True)
    frappe.clear_cache(user=user)


def _descriptor_and_graph(
    operation: str, record_name: str, planned_customer_name: str | None,
    catalog: dict[str, Any], capabilities: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    capability = f"frappe.record.{operation}"
    operation_value: dict[str, Any] = {
        "kind": "record", "action": operation, "doctype": "Customer", "docname": record_name,
    }
    if operation == "update":
        operation_value["values"] = {"customer_name": planned_customer_name}
    effect = {
        "schemaVersion": 1, "capability": capability, "operation": operation_value,
        "postconditions": [], "approvalClass": "dual_control" if operation == "delete" else "single",
    }
    objective = (
        f"Update exact Customer {record_name} customer name to {planned_customer_name}"
        if operation == "update" else f"Delete exact Customer {record_name}"
    )
    budget = {"runtimeMs": 60_000, "toolCalls": 8, "modelCalls": 0, "tokens": 0, "costMicros": 0, "artifactBytes": 4096}
    limits = {"maxDepth": 3, "maxChildrenPerNode": 4, "maxActiveNodes": 4, "maxRetries": 1, "maxParallelism": 1, "maxPhases": 3, "maxSteps": 4}
    descriptor = {
        "schemaVersion": 1, "id": f"native.rbac.{operation}", "version": "1.0.0",
        "meta": {
            "name": f"Native Desk exact-record {operation}",
            "description": "Deterministic inert proposal for live maker/checker evidence",
            "phases": [{"title": "Review"}, {"title": "Verify"}],
        },
        "goal": objective, "resultSchema": {"type": "object"},
        "budget": budget, "limits": limits,
        "steps": [{
            "kind": "execution", "label": f"Review exact Customer {operation}",
            "capabilities": [capability], "execution": {"surface": "server_effect", "plan": effect},
        }],
    }
    graph = {
        "schemaVersion": 1, "id": descriptor["id"], "version": descriptor["version"],
        "entryNodeId": "change", "nodes": [{
            "id": "change", "kind": "command", "requestedCapabilities": [capability],
            "retryLimit": 0, "executionIntent": {"surface": "server_effect", "plan": effect},
        }], "edges": [], "budget": budget,
        "limits": {key: limits[key] for key in ("maxDepth", "maxChildrenPerNode", "maxActiveNodes", "maxRetries")},
    }
    descriptor, graph = _materialize_attended_crud_bundle(
        descriptor, graph, catalog, capabilities, objective=objective,
    )
    return (
        validate_workflow_descriptor(descriptor, capabilities),
        validate_compiled_graph(graph, descriptor, capabilities),
    )


def _ensure_proposal(operation: str, record_name: str, maker: str) -> str:
    request_id = f"native-desk-rbac-live-v1:{frappe.local.site}:{operation}"
    if name := frappe.db.get_value("Muster Workflow Proposal", {"request_id": request_id}, "name"):
        proposal = frappe.get_doc("Muster Workflow Proposal", name)
        if proposal.requested_by != maker or proposal_attended_operation(proposal) != operation:
            raise WorkflowProposalError(_("Existing native evidence proposal does not match its deterministic binding"))
        return str(name)
    catalog = _attended_form_catalog("Customer", record_name, maker, record_identity_state="unique")
    capabilities = _caller_capabilities(maker, "*")
    planned_name = f"{EVIDENCE_PREFIX} Update Applied" if operation == "update" else None
    descriptor, graph = _descriptor_and_graph(operation, record_name, planned_name, catalog, capabilities)
    scope = _canonical_requested_scope({"doctype": "Customer", "docname": record_name})
    canonical_scope = json.dumps(scope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    canonical_descriptor = json.dumps(descriptor, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    canonical_graph = json.dumps(graph, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    doc = frappe.get_doc({
        "doctype": "Muster Workflow Proposal", "objective": descriptor["goal"],
        "status": "Proposed", "requested_by": maker, "requested_at": now_datetime(),
        "request_id": request_id, "gateway_request_id": f"fixture:{request_id}",
        "requested_scope_json": json.dumps(scope, ensure_ascii=False, indent=2, sort_keys=True),
        "requested_scope_hash": sha256(canonical_scope.encode()).hexdigest(),
        "context_json": json.dumps({"fixture": "native-desk-rbac-live-v1"}, sort_keys=True),
        "capabilities_json": json.dumps(capabilities, ensure_ascii=False, indent=2),
        "descriptor_json": json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True),
        "descriptor_hash": sha256(canonical_descriptor.encode()).hexdigest(),
        "compiled_graph_json": json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True),
        "compiled_graph_hash": sha256(canonical_graph.encode()).hexdigest(),
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _approve_as_checker(proposal_name: str, checker: str) -> None:
    proposal = frappe.get_doc("Muster Workflow Proposal", proposal_name)
    if proposal.status == "Approved":
        if str(proposal.reviewed_by or "") != checker:
            raise WorkflowProposalError(_("The deterministic proposal was approved by an unexpected checker"))
        return
    if proposal.status != "Proposed":
        raise WorkflowProposalError(_("The deterministic proposal is not reviewable"))
    from muster.api.mission import review_proposal

    previous_user = frappe.session.user
    previous_request = getattr(frappe.local, "request", None)
    try:
        frappe.set_user(checker)
        frappe.local.request = frappe._dict(
            method="POST", headers={"Idempotency-Key": f"native-rbac-approve:{proposal_name}"},
        )
        review_proposal(proposal_name, "approve")
    finally:
        frappe.local.request = previous_request
        frappe.set_user(previous_user)


def setup(confirm: bool | int | str = False) -> dict[str, Any]:
    """Create passwordless disposable live-evidence identities, targets and approved proposals."""
    if frappe.session.user != "Administrator":
        frappe.throw(_("Only Administrator can set up native Desk RBAC evidence"), frappe.PermissionError)
    if not cint(confirm):
        frappe.throw(_("Explicit confirmation is required for evidence setup"), frappe.ValidationError)
    installed = set(frappe.get_installed_apps())
    if not {"muster", "erpnext"}.issubset(installed):
        raise WorkflowProposalError(_("Muster and ERPNext are required for native Desk RBAC evidence"))
    users = {key: _ensure_evidence_user(key) for key in EVIDENCE_USERS}
    records = {
        "update": _ensure_customer("Disposable Update Target"),
        "delete": _ensure_customer("Disposable Delete Target"),
    }
    for record_name in records.values():
        _ensure_customer_permission(users["maker"], record_name)
    previous_user = frappe.session.user
    try:
        frappe.set_user(users["maker"])
        proposals = {
            operation: _ensure_proposal(operation, record_name, users["maker"])
            for operation, record_name in records.items()
        }
    finally:
        frappe.set_user(previous_user)
    for proposal_name in proposals.values():
        _approve_as_checker(proposal_name, users["checker"])
    frappe.db.commit()
    return {
        "schema_version": 1, "kind": "muster.native_desk.exact_record_rbac.setup",
        "site": str(frappe.local.site), "users": users, "records": records,
        "proposals": proposals, "personas_enabled": False, "passwords_created": False,
    }


def activate(temporary_password: str, confirm: bool | int | str = False) -> dict[str, Any]:
    """Temporarily enable the exact evidence personas for an attended browser take."""
    from frappe.utils.password import update_password

    if frappe.session.user != "Administrator":
        frappe.throw(_("Only Administrator can activate native evidence personas"), frappe.PermissionError)
    if not cint(confirm):
        frappe.throw(_("Explicit confirmation is required for persona activation"), frappe.ValidationError)
    if not isinstance(temporary_password, str) or len(temporary_password) < 20:
        frappe.throw(_("The runtime-only evidence password must contain at least 20 characters"), frappe.ValidationError)
    users = [_ensure_evidence_user(key) for key in EVIDENCE_USERS]
    for user in users:
        update_password(user, temporary_password, logout_all_sessions=True)
        frappe.db.set_value("User", user, {"enabled": 1, "api_key": None})
        frappe.clear_cache(user=user)
    frappe.db.commit()
    return {"activated": len(users), "users": users, "password_returned": False}


def revoke(confirm: bool | int | str = False) -> dict[str, Any]:
    """Disable evidence personas, remove all credentials, and terminate their sessions."""
    from frappe.sessions import clear_sessions
    from frappe.utils.password import delete_all_passwords_for

    if frappe.session.user != "Administrator":
        frappe.throw(_("Only Administrator can revoke native evidence personas"), frappe.PermissionError)
    if not cint(confirm):
        frappe.throw(_("Explicit confirmation is required for persona revocation"), frappe.ValidationError)
    users = [user for user in EVIDENCE_USERS.values() if frappe.db.exists("User", user)]
    for user in users:
        frappe.db.set_value("User", user, {"enabled": 0, "api_key": None})
        delete_all_passwords_for("User", user)
        clear_sessions(user=user, keep_current=False, force=True)
        frappe.clear_cache(user=user)
    frappe.db.commit()
    return {"revoked": len(users), "users": users, "credentials_removed": True}
