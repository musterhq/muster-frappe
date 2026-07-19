from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.file_manager import save_file

from muster.orchestration.development import (
    DevelopmentSecurityError,
    SourceSnapshot,
    apply_reviewed_patch,
    canonical,
    generate_reviewed_patch,
    run_offline_codex,
    sha256_bytes,
    source_snapshot,
    validate_allowed_paths,
)

DEVELOPMENT_ROLES = {"System Manager", "Muster Administrator", "Muster Automation Manager"}
REVIEW_ROLES = {"System Manager", "Muster Administrator"}
# Deployment deliberately remains a different gate. Only these fixed operation
# identifiers may ever be wired to operator-reviewed bench argv templates.
FIXED_DEPLOYMENT_REGISTRY = {
    "migrate": ("bench", "--site", "{registered_site}", "migrate"),
    "build_app": ("bench", "build", "--app", "{registered_app}"),
    "restart": ("bench", "restart"),
}


def _require_post() -> None:
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(_("This endpoint only accepts POST requests"), frappe.PermissionError)


def _roles(user: str | None = None) -> set[str]:
    return set(frappe.get_roles(user or frappe.session.user))


def _require_roles(allowed: set[str]) -> str:
    user = frappe.session.user
    if user == "Guest" or (user != "Administrator" and not (_roles(user) & allowed)):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if not cint(frappe.db.get_value("User", user, "enabled")):
        frappe.throw(_("This user is not active"), frappe.PermissionError)
    return user


def _policy_hash(policy) -> str:
    return hashlib.sha256(canonical({
        "name": policy.name,
        "enabled": bool(policy.enabled),
        "modified": str(policy.modified),
    }).encode()).hexdigest()


def _registered(app_name: str) -> tuple[Any, SourceSnapshot, tuple[str, ...]]:
    app = frappe.get_doc("Muster Development App", app_name)
    if not app.enabled:
        frappe.throw(_("This registered development app is disabled"), frappe.ValidationError)
    root = app.get_password("source_root_secret", raise_exception=False)
    try:
        allowed = validate_allowed_paths(json.loads(app.allowed_paths_json or "[]"))
        snapshot = source_snapshot(app.app_name, root)
    except (TypeError, ValueError, DevelopmentSecurityError) as error:
        frappe.throw(_("The registered development source is unavailable or unsafe"), frappe.ValidationError)
        raise error
    if snapshot.revision != app.registered_revision or snapshot.status_hash != app.registered_status_hash:
        frappe.throw(_("The registered development source changed; an administrator must review it again"), frappe.ValidationError)
    return app, snapshot, allowed


def create_from_ask_turn(turn, app_name: str, policy_name: str, idempotency_key: str) -> dict[str, Any]:
    """Create inert source-bound evidence. No worker is started here."""
    user = _require_roles(DEVELOPMENT_ROLES)
    if turn.requested_by != user:
        frappe.throw(_("Only the Ask requester can create its development proposal"), frappe.PermissionError)
    existing = frappe.db.get_value("Muster Development Proposal", {"request_id": idempotency_key}, "name")
    if existing:
        proposal = frappe.get_doc("Muster Development Proposal", existing)
        if proposal.ask_turn != turn.name or proposal.requested_by != user:
            frappe.throw(_("Development idempotency key was already used"), frappe.ValidationError)
        return {"proposal": proposal.name, "status": proposal.status, "replayed": True, "executed": False}
    app, snapshot, allowed = _registered(app_name)
    if not app.has_permission("read", user=user):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    policy = frappe.get_doc("Muster Policy", policy_name)
    if not policy.enabled or not policy.has_permission("read", user=user):
        frappe.throw(_("Select an enabled policy you can read"), frappe.PermissionError)
    objective = turn.get_password("prompt_secret")
    if hashlib.sha256(objective.encode()).hexdigest() != turn.prompt_hash:
        frappe.throw(_("The Ask objective no longer matches its evidence"), frappe.ValidationError)
    allowed_json = canonical(list(allowed))
    proposal = frappe.get_doc({
        "doctype": "Muster Development Proposal",
        "ask_turn": turn.name,
        "app": app.name,
        "policy": policy.name,
        "status": "Proposed",
        "requested_by": user,
        "requested_at": now_datetime(),
        "request_id": idempotency_key,
        "objective_secret": objective,
        "objective_hash": hashlib.sha256(objective.encode()).hexdigest(),
        "source_revision": snapshot.revision,
        "source_status_hash": snapshot.status_hash,
        "allowed_paths_json": allowed_json,
        "allowed_paths_hash": hashlib.sha256(allowed_json.encode()).hexdigest(),
        "policy_revision_hash": _policy_hash(policy),
        "deployment_status": "Not Requested",
    }).insert()
    return {"proposal": proposal.name, "status": proposal.status, "replayed": False, "executed": False}


@frappe.whitelist()
def review(proposal: str, action: str) -> dict[str, Any]:
    """Independent review only; approval does not run Codex."""
    _require_post()
    reviewer = _require_roles(REVIEW_ROLES)
    doc = frappe.get_doc("Muster Development Proposal", proposal)
    if doc.status != "Proposed" or action not in {"approve", "reject"}:
        frappe.throw(_("Only a proposed development change can be approved or rejected"), frappe.ValidationError)
    if reviewer.lower() == doc.requested_by.lower():
        frappe.throw(_("Development review requires a different administrator"), frappe.PermissionError)
    _recheck_proposal(doc, require_policy=True)
    doc.db_set({
        "status": "Approved" if action == "approve" else "Rejected",
        "reviewed_by": reviewer,
        "reviewed_at": now_datetime(),
    }, update_modified=True)
    return {"proposal": doc.name, "status": doc.status, "executed": False}


@frappe.whitelist()
def generate(proposal: str, confirmed: int | str = 0) -> dict[str, Any]:
    """Queue the isolated worker only after an explicit approved-state check."""
    _require_post()
    _require_roles(REVIEW_ROLES)
    if not cint(confirmed):
        frappe.throw(_("Confirm isolated patch generation"), frappe.ValidationError)
    doc = frappe.get_doc("Muster Development Proposal", proposal)
    if doc.status == "Queued":
        return {"proposal": doc.name, "status": doc.status, "replayed": True, "executed": False}
    if doc.status != "Approved" or not doc.reviewed_by or doc.reviewed_by == doc.requested_by:
        frappe.throw(_("This development proposal does not have independent approval"), frappe.ValidationError)
    _recheck_proposal(doc, require_policy=True)
    doc.db_set("status", "Queued", update_modified=True)
    frappe.enqueue(
        "muster.api.development.run_generation",
        queue="long",
        proposal=doc.name,
        job_id=f"muster-development-{doc.name}",
        deduplicate=True,
    )
    return {"proposal": doc.name, "status": "Queued", "replayed": False, "executed": False}


def run_generation(proposal: str) -> None:
    doc = frappe.get_doc("Muster Development Proposal", proposal)
    if doc.status not in {"Queued", "Generating"}:
        return
    doc.db_set("status", "Generating", update_modified=True)
    try:
        _app, snapshot, allowed = _recheck_proposal(doc, require_policy=True)
        objective = doc.get_password("objective_secret")
        generated = generate_reviewed_patch(snapshot, objective, allowed, run_offline_codex)
        patch_file = save_file(
            f"{doc.name}.patch", generated.patch, "Muster Development Proposal", doc.name,
            is_private=1,
        )
        manifest_file = save_file(
            f"{doc.name}-tests.json", generated.test_manifest,
            "Muster Development Proposal", doc.name, is_private=1,
        )
        doc.db_set({
            "status": "Ready",
            "patch_file": patch_file.file_url,
            "patch_hash": generated.patch_hash,
            "test_manifest_file": manifest_file.file_url,
            "changed_files_json": canonical(list(generated.changed_files)),
            "generated_at": now_datetime(),
            "failure_summary": None,
        }, update_modified=True)
    except Exception:
        doc.db_set({
            "status": "Failed",
            "failure_summary": _("Isolated patch generation failed closed. Source was not changed."),
        }, update_modified=True)
        frappe.log_error(title=f"Muster development generation failed: {doc.name}")
        raise


@frappe.whitelist()
def apply(proposal: str, confirmed: int | str = 0) -> dict[str, Any]:
    """Separate privileged source-apply gate. This never deploys the result."""
    _require_post()
    actor = _require_roles(REVIEW_ROLES)
    if not cint(confirmed):
        frappe.throw(_("Confirm application of the exact reviewed patch"), frappe.ValidationError)
    if frappe.db.db_type == "sqlite":
        frappe.db.sql("select name from `tabMuster Development Proposal` where name=%s", proposal)
    else:
        frappe.db.sql("select name from `tabMuster Development Proposal` where name=%s for update", proposal)
    doc = frappe.get_doc("Muster Development Proposal", proposal)
    if doc.status == "Applied":
        return {"proposal": doc.name, "status": "Applied", "replayed": True, "deployed": False}
    if doc.status != "Ready" or not doc.patch_file or not doc.patch_hash:
        frappe.throw(_("Only a ready reviewed patch can be applied"), frappe.ValidationError)
    _app, snapshot, allowed = _recheck_proposal(doc, require_policy=True)
    patch = _private_file_content(doc.patch_file, doc.name)
    if sha256_bytes(patch) != doc.patch_hash:
        frappe.throw(_("The stored reviewed patch no longer matches its checksum"), frappe.ValidationError)
    lock_path = frappe.get_site_path("private", "locks", f"muster-development-{doc.app}.lock")
    try:
        evidence_hash = apply_reviewed_patch(snapshot, patch, doc.patch_hash, allowed, lock_path)
    except DevelopmentSecurityError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise error
    doc.db_set({
        "status": "Applied",
        "applied_by": actor,
        "applied_at": now_datetime(),
        "apply_evidence_hash": evidence_hash,
        "deployment_status": "Ready for Separate Gate",
    }, update_modified=True)
    return {"proposal": doc.name, "status": "Applied", "replayed": False, "deployed": False, "evidence_hash": evidence_hash}


@frappe.whitelist()
def request_deployment(proposal: str, operation: str, confirmed: int | str = 0) -> dict[str, Any]:
    """Fail closed until a site-specific fixed bench registry and rollback runner is approved."""
    _require_post()
    _require_roles(REVIEW_ROLES)
    if operation not in FIXED_DEPLOYMENT_REGISTRY or not cint(confirmed):
        frappe.throw(_("A valid separately confirmed deployment operation is required"), frappe.ValidationError)
    doc = frappe.get_doc("Muster Development Proposal", proposal)
    if doc.status != "Applied":
        frappe.throw(_("Only an applied reviewed patch can enter deployment review"), frappe.ValidationError)
    doc.db_set("deployment_status", "Blocked - No Approved Registry", update_modified=True)
    frappe.throw(_("Deployment is disabled until this site has an administrator-reviewed bench command registry and rollback target. No command ran."), frappe.ValidationError)


def _recheck_proposal(doc, *, require_policy: bool) -> tuple[Any, SourceSnapshot, tuple[str, ...]]:
    app, snapshot, current_allowed = _registered(doc.app)
    reviewed_allowed = validate_allowed_paths(json.loads(doc.allowed_paths_json or "[]"))
    if (
        snapshot.revision != doc.source_revision
        or snapshot.status_hash != doc.source_status_hash
        or current_allowed != reviewed_allowed
        or hashlib.sha256(canonical(list(reviewed_allowed)).encode()).hexdigest() != doc.allowed_paths_hash
    ):
        frappe.throw(_("The registered source or reviewed paths changed; create a new development proposal"), frappe.ValidationError)
    if require_policy:
        policy = frappe.get_doc("Muster Policy", doc.policy)
        if not policy.enabled or _policy_hash(policy) != doc.policy_revision_hash:
            frappe.throw(_("The development policy changed; create a new proposal"), frappe.ValidationError)
    return app, snapshot, reviewed_allowed


def _private_file_content(file_url: str, proposal: str) -> bytes:
    name = frappe.db.get_value(
        "File",
        {"file_url": file_url, "attached_to_doctype": "Muster Development Proposal", "attached_to_name": proposal, "is_private": 1},
        "name",
    )
    if not name:
        frappe.throw(_("The reviewed patch artifact is unavailable"), frappe.ValidationError)
    content = frappe.get_doc("File", name).get_content()
    return content if isinstance(content, bytes) else content.encode()

