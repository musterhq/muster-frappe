from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

import frappe
from frappe.utils import add_days, now_datetime, random_string
from frappe.utils.file_manager import save_file

from muster.api.native_builder import preview
from muster.api.development import create_from_ask_turn
from muster.orchestration.development import generate_reviewed_patch, source_snapshot


POLICY = "[Track3 Live] Native Customization"
MISSION_KEY = "track3-live-customization-v1"
CHECKER = "muster.customization.checker@muster.invalid"
CAPABILITIES = (
    "artifact.custom_field.write",
    "artifact.property_setter.write",
    "artifact.doctype.write",
    "artifact.report.write",
    "artifact.report.script.write",
    "artifact.print_format.write",
    "artifact.page.write",
    "artifact.web_page.write",
    "artifact.client_script.write",
    "artifact.server_script.write",
    "artifact.server_script.doctype.write",
    "artifact.server_script.api.write",
    "artifact.server_script.scheduler.write",
    "artifact.email_template.write",
)
DEVELOPMENT_APP = "field_ops_demo"
DEVELOPMENT_ROOT = "/home/goblin/personal/field_ops_demo-stage"
DEVELOPMENT_ALLOWED = ["field_ops_demo/muster_service_playbook.py"]
DEVELOPMENT_REQUEST = "track3-live-reviewed-code-patch-v1"


def _require(confirm: bool | int | str) -> None:
    if frappe.session.user != "Administrator":
        frappe.throw("Track 3 live setup requires Administrator")
    if str(confirm).lower() not in {"1", "true"}:
        frappe.throw("Track 3 live setup requires explicit confirmation")


def _fixture(name: str) -> Path:
    return Path(__file__).resolve().parent / "fixtures" / name


def _ensure_mission() -> str:
    existing = frappe.db.get_value("Muster Mission", {"idempotency_key": MISSION_KEY}, "name")
    if existing:
        return str(existing)
    return frappe.get_doc({
        "doctype": "Muster Mission",
        "objective": "Live source-bound native customization evidence for Frappeverse",
        "status": "Draft",
        "requested_by": "Administrator",
        "requested_at": now_datetime(),
        "idempotency_key": MISSION_KEY,
        "scope_json": json.dumps({"site": frappe.local.site, "disposable": True}, sort_keys=True),
        "budget_json": "{}",
        "usage_json": "{}",
    }).insert().name


def _ensure_source_file() -> str:
    source = _fixture("frappeverse_service_intake_prd.md")
    filename = "track3-live-frappeverse-service-intake-prd.md"
    existing = frappe.db.get_value("File", {"file_name": filename, "is_private": 1}, "name")
    if existing:
        return str(existing)
    return save_file(filename, source.read_bytes(), None, None, is_private=1).name


def _ensure_policy() -> str:
    existing = frappe.db.exists("Muster Policy", POLICY)
    if existing:
        policy = frappe.get_doc("Muster Policy", existing)
        actual = {(row.effect, row.capability, row.action, row.resource_type,
                   row.resource_pattern, row.approval_class) for row in policy.rules}
        expected = {("Allow", capability, "*", "Site", frappe.local.site, "None")
                    for capability in CAPABILITIES}
        if not policy.enabled:
            frappe.throw("Existing Track 3 policy is disabled")
        if actual != expected:
            policy.set("rules", [{
                "effect": "Allow", "capability": capability, "action": "*",
                "resource_type": "Site", "resource_pattern": frappe.local.site,
                "approval_class": "None",
            } for capability in CAPABILITIES])
            policy.save()
        return policy.name
    return frappe.get_doc({
        "doctype": "Muster Policy", "policy_name": POLICY, "enabled": 1,
        "priority": 40, "description": "Disposable Frappeverse native customization evidence only.",
        "rules": [{
            "effect": "Allow", "capability": capability, "action": "*",
            "resource_type": "Site", "resource_pattern": frappe.local.site,
            "approval_class": "None",
        } for capability in CAPABILITIES],
    }).insert().name


def _ensure_binding() -> str:
    filters = {
        "subject_type": "User", "subject": "Administrator", "status": "Active",
        "scope_type": "Site", "scope_value": frappe.local.site,
    }
    existing = frappe.db.get_value("Muster Role Binding", filters, "name")
    capabilities = "\n".join(CAPABILITIES)
    if existing:
        binding = frappe.get_doc("Muster Role Binding", existing)
        if set((binding.capabilities or "").splitlines()) != set(CAPABILITIES):
            binding.capabilities = capabilities
            binding.save()
        return binding.name
    return frappe.get_doc({"doctype": "Muster Role Binding", **filters,
                           "capabilities": capabilities}).insert().name


def _ensure_checker() -> dict[str, Any]:
    """Enable one narrowly scoped approver with a server-only runtime credential."""
    if not frappe.db.exists("User", CHECKER):
        user = frappe.get_doc({
            "doctype": "User", "email": CHECKER,
            "first_name": "Muster Customization", "last_name": "Checker",
            "send_welcome_email": 0, "enabled": 0,
        }).insert(ignore_permissions=True)
        user.add_roles("Muster Approver")
    user = frappe.get_doc("User", CHECKER)
    explicit_roles = {row.role for row in user.roles}
    if explicit_roles != {"Muster Approver"}:
        frappe.throw("The customization checker must have only the Muster Approver role")
    user.enabled = 1
    user.api_key = random_string(20)
    user.api_secret = random_string(40)
    user.save(ignore_permissions=True)
    return {
        "checker": CHECKER,
        "explicit_roles": sorted(explicit_roles),
        "effective_roles": sorted(frappe.get_roles(CHECKER)),
        "has_system_manager": "System Manager" in frappe.get_roles(CHECKER),
        "runtime_credential_issued": True,
        "credential_exposed": False,
    }


def revoke_checker(*, confirm: bool | int | str = False) -> dict[str, Any]:
    """Revoke the evidence user's API material and return it to disabled state."""
    _require(confirm)
    user = frappe.get_doc("User", CHECKER)
    user.enabled = 0
    user.api_key = None
    user.api_secret = None
    user.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "checker": CHECKER, "enabled": False,
        "api_key_present": bool(user.api_key), "api_secret_present": bool(user.api_secret),
        "explicit_roles": sorted(row.role for row in user.roles),
    }


def _ensure_development_app() -> str:
    snapshot = source_snapshot(DEVELOPMENT_APP, DEVELOPMENT_ROOT)
    allowed = json.dumps(DEVELOPMENT_ALLOWED, separators=(",", ":"))
    if frappe.db.exists("Muster Development App", DEVELOPMENT_APP):
        app = frappe.get_doc("Muster Development App", DEVELOPMENT_APP)
        if (not app.enabled or app.registered_revision != snapshot.revision
                or app.registered_status_hash != snapshot.status_hash
                or json.loads(app.allowed_paths_json) != DEVELOPMENT_ALLOWED):
            frappe.throw("The registered Field Ops Demo source boundary changed")
        return app.name
    return frappe.get_doc({
        "doctype": "Muster Development App", "app_name": DEVELOPMENT_APP,
        "enabled": 1, "source_root_secret": DEVELOPMENT_ROOT,
        "allowed_paths_json": allowed,
    }).insert().name


def _development_runner(workspace: Path, _prompt: str) -> None:
    target = workspace / DEVELOPMENT_ALLOWED[0]
    target.write_text(
        '"""Reviewed service-intake rule generated from the cited Track 3 PRD."""\n\n'
        "SERVICE_REGIONS = (\"North\", \"South\", \"East\", \"West\")\n\n"
        "def normalize_service_region(value: str) -> str:\n"
        "    normalized = (value or \"\").strip().title()\n"
        "    if normalized not in SERVICE_REGIONS:\n"
        "        raise ValueError(\"Unsupported service region\")\n"
        "    return normalized\n",
        encoding="utf-8",
    )


def prepare_development_case(*, confirm: bool | int | str = False) -> dict[str, Any]:
    """Create a real reviewed patch for a clean registered app; never apply it here."""
    _require(confirm)
    checker = _ensure_checker()
    app_name = _ensure_development_app()
    existing = frappe.db.get_value(
        "Muster Development Proposal", {"request_id": DEVELOPMENT_REQUEST}, "name"
    )
    if existing:
        proposal = frappe.get_doc("Muster Development Proposal", existing)
        return {
            "proposal": proposal.name, "status": proposal.status,
            "app": app_name, **checker, "effects_executed": proposal.status in {"Applied", "Rolled Back"},
        }
    objective = (
        "Implement a small, testable service-region normalization module in the registered "
        "Field Ops Demo app, constrained to the reviewed path and cited Track 3 PRD."
    )
    prompt_hash = hashlib.sha256(objective.encode()).hexdigest()
    ask = frappe.get_doc({
        "doctype": "Muster Ask Turn", "requested_by": "Administrator",
        "conversation_id": "track3-live-development", "request_id": DEVELOPMENT_REQUEST,
        "status": "Accepted", "expires_at": add_days(now_datetime(), 1),
        "prompt_secret": objective, "prompt_hash": prompt_hash,
        "scope_json": json.dumps({"site": frappe.local.site, "app": DEVELOPMENT_APP}, sort_keys=True),
        "scope_hash": hashlib.sha256(json.dumps({"site": frappe.local.site, "app": DEVELOPMENT_APP}, sort_keys=True).encode()).hexdigest(),
        "outcomes_json": "[]", "handoffs_json": "[]",
    }).insert()
    created = create_from_ask_turn(
        ask, app_name, POLICY, DEVELOPMENT_REQUEST, source_file=_ensure_source_file(),
    )
    proposal = frappe.get_doc("Muster Development Proposal", created["proposal"])
    snapshot = source_snapshot(DEVELOPMENT_APP, DEVELOPMENT_ROOT)
    generated = generate_reviewed_patch(
        snapshot, objective, DEVELOPMENT_ALLOWED, _development_runner,
    )
    patch_file = save_file(
        f"{proposal.name}.patch", generated.patch,
        "Muster Development Proposal", proposal.name, is_private=1,
    )
    manifest_file = save_file(
        f"{proposal.name}-tests.json", generated.test_manifest,
        "Muster Development Proposal", proposal.name, is_private=1,
    )
    proposal.db_set({
        "status": "Ready", "reviewed_by": CHECKER, "reviewed_at": now_datetime(),
        "patch_file": patch_file.file_url, "patch_hash": generated.patch_hash,
        "test_manifest_file": manifest_file.file_url,
        "changed_files_json": json.dumps(list(generated.changed_files), separators=(",", ":")),
        "generated_at": now_datetime(), "deployment_status": "Not Requested",
        "rollback_status": "Not Requested",
    }, update_modified=True)
    frappe.db.commit()
    return {
        "proposal": proposal.name, "status": "Ready", "app": app_name,
        "patch_hash": generated.patch_hash, "changed_files": list(generated.changed_files),
        "reviewed_by": CHECKER, **checker, "effects_executed": False,
    }


def prepare(*, confirm: bool | int | str = False) -> dict[str, Any]:
    """Create deterministic inputs and authority, never an artifact outcome."""
    _require(confirm)
    checker = _ensure_checker()
    result = {
        "mission": _ensure_mission(), "source_file": _ensure_source_file(),
        "policy": _ensure_policy(), "binding": _ensure_binding(),
        **checker, "effects_executed": False,
    }
    frappe.db.commit()
    return result


def prepare_case(case_id: str, *, confirm: bool | int | str = False) -> dict[str, Any]:
    """Preview and independently approve one case; applying remains a browser action."""
    inputs = prepare(confirm=confirm)
    matrix = json.loads(_fixture("attended_native_customization_matrix.json").read_text())
    case = next((row for row in matrix["cases"] if row["id"] == case_id), None)
    if not case:
        frappe.throw("Unknown Track 3 live case")
    artifact = case["artifact"]
    target = {
        "property_setter": ("Property Setter", None),
        "doctype": ("DocType", artifact["target_name"]),
        "query_report": ("Report", artifact["target_name"]),
        "script_report": ("Report", artifact["target_name"]),
        "print_format": ("Print Format", artifact["target_name"]),
        "page": ("Page", artifact["target_name"]),
        "web_page": ("Web Page", artifact["target_name"]),
        "client_script": ("Client Script", artifact["target_name"]),
        "server_script": ("Server Script", artifact["target_name"]),
        "email_template": ("Email Template", artifact["target_name"]),
    }.get(artifact["kind"])
    if artifact["kind"] == "custom_field":
        target = ("Custom Field", f"{artifact['target_doctype']}-{artifact['target_name']}")
    if target is None:
        frappe.throw("Unsupported Track 3 live artifact kind")
    if target[1] and frappe.db.exists(target[0], target[1]):
        frappe.throw(f"Disposable target already exists: {target[0]} {target[1]}")
    result = preview({
        "schema_version": "1.0", "mission": inputs["mission"],
        "source_file": inputs["source_file"], "artifacts": [artifact],
    })
    approval = frappe.get_doc({
        "doctype": "Muster Approval", "mission": inputs["mission"],
        "change_set": result["change_set"], "status": "Pending",
        "approval_class": result["approval_class"], "requested_by": "Administrator",
        "requested_from": CHECKER, "expires_at": add_days(now_datetime(), 1),
        "action_hash": result["plan_hash"],
        "diff_json": json.dumps(result["changes"], sort_keys=True, default=str),
    }).insert()
    frappe.set_user(CHECKER)
    approval.status = "Approved"
    approval.decision_note = "Independent Track 3 disposable live-evidence approval"
    # Bench evidence setup has no HTTP permission resolver context. The
    # document controller still enforces that the session is the assigned,
    # different approver; ignore_permissions only bypasses the transport layer.
    approval.save(ignore_permissions=True)
    frappe.set_user("Administrator")
    frappe.db.commit()
    return {**result, "case_id": case_id, "approval": approval.name,
            "approved_by": CHECKER, "effects_executed": False}


def approve_rollback(change_set: str, *, confirm: bool | int | str = False) -> dict[str, Any]:
    """Create an independent destructive approval; rollback remains a browser action."""
    _require(confirm)
    checker = _ensure_checker()
    doc = frappe.get_doc("Muster Change Set", change_set)
    if doc.actor != "Administrator" or doc.status != "Verified":
        frappe.throw("Only a verified Track 3 Administrator Change Set can request rollback")
    existing = frappe.db.get_value("Muster Approval", {
        "change_set": doc.name, "approval_class": "Destructive", "status": "Approved",
        "action_hash": doc.plan_hash,
    }, "name")
    if existing:
        return {"change_set": doc.name, "approval": existing,
                **checker, "effects_executed": False}
    approval = frappe.get_doc({
        "doctype": "Muster Approval", "mission": doc.mission,
        "change_set": doc.name, "status": "Pending", "approval_class": "Destructive",
        "requested_by": "Administrator", "requested_from": CHECKER,
        "expires_at": add_days(now_datetime(), 1), "action_hash": doc.plan_hash,
        "diff_json": doc.evidence_json,
    }).insert()
    frappe.set_user(CHECKER)
    approval.status = "Approved"
    approval.decision_note = "Independent destructive approval for disposable Track 3 evidence"
    approval.save(ignore_permissions=True)
    frappe.set_user("Administrator")
    frappe.db.commit()
    return {"change_set": doc.name, "approval": approval.name,
            "approved_by": CHECKER, **checker, "effects_executed": False}
