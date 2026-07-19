from __future__ import annotations

import json
import re
from collections import Counter
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import now_datetime

from muster.adapters.client import GatewayBinding, GatewayClient, trusted_binding
from muster.adapters.context import permission_filtered_context
from muster.adapters.run_authority import run_authority_headers
from muster.orchestration.gateway_runtime import _caller_capabilities
from muster.orchestration.gateway_runtime import _capability_authority
from muster.orchestration.form_schema import MusterFormSchemaError, effective_form_schema
from muster.orchestration.studio import publish_workflow
from muster.orchestration.workflow_graph import (
    browser_action_plan,
    compile_legacy_snapshot,
    effect_intent,
)

WORKFLOW_PROPOSALS_PATH = "/v1/integrations/frappe/workflow-proposals"
MAX_DESCRIPTOR_BYTES = 1_000_000
MAX_SCOPE_BYTES = 30_000
MAX_SCOPE_DOCUMENTS = 20
MAX_STEPS = 64
MAX_DEPTH = 8
WORKFLOW_BUDGET_CEILINGS = {"runtimeMs": 900_000, "toolCalls": 100, "modelCalls": 32, "tokens": 200_000, "costMicros": 5_000_000, "artifactBytes": 100_000_000}
WORKFLOW_LIMIT_CEILINGS = {"maxDepth": 8, "maxChildrenPerNode": 8, "maxActiveNodes": 64, "maxRetries": 3, "maxParallelism": 8, "maxPhases": 16, "maxSteps": 64}
CAPABILITY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,255}$")
GRAPH_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,255}$")
GRAPH_NODE_KINDS = {
    "plan", "agent", "subworkflow", "command", "transform", "condition",
    "parallel_map", "approval", "wait", "artifact", "verification",
    "compensation", "loop",
}
SCHEMA_KEYS = {
    "type", "title", "description", "default", "enum", "const", "properties", "required",
    "additionalProperties", "items", "minItems", "maxItems", "minimum", "maximum",
    "minLength", "maxLength", "pattern", "format", "oneOf", "anyOf", "allOf",
}


class WorkflowProposalError(frappe.ValidationError):
    pass


def request_workflow_proposal(
    objective: str,
    scope: dict[str, Any],
    idempotency_key: str,
    *,
    client: GatewayClient | None = None,
    binding: GatewayBinding | None = None,
    preferred_handoff_kind: str | None = None,
) -> dict[str, Any]:
    objective = _bounded_text(objective, "objective", 10_000)
    if not isinstance(scope, dict):
        raise WorkflowProposalError(_("Planning scope must be a JSON object"))
    user = frappe.session.user
    if user == "Guest" or not frappe.has_permission("Muster Workflow Proposal", "create"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    prior = frappe.db.get_value(
        "Muster Workflow Proposal", {"request_id": idempotency_key},
        ["name", "objective", "status"], as_dict=True,
    )
    if prior:
        if prior.objective != objective:
            raise WorkflowProposalError(_("Idempotency key was already used for another goal"))
        return {"proposal": prior.name, "status": prior.status, "replayed": True}

    binding = binding or trusted_binding()
    client = client or GatewayClient(binding)
    reviewed_scope = _canonical_requested_scope(scope)
    context = permission_filtered_context(reviewed_scope, user)
    attended_catalogs = _attended_form_catalogs(reviewed_scope, user, objective)
    if attended_catalogs:
        context = {**context, "attended_form_catalog": [{
            "doctype": catalog["doctype"], "actions": catalog["actions"],
            "record_name": catalog["record_name"], "fields": catalog["fields"],
            "authority": catalog["authority"],
        } for catalog in attended_catalogs]}
    allowed_capabilities = _caller_capabilities(user, "*")
    request_id = _stable_request_id(idempotency_key, user)
    headers, _csrf_token = run_authority_headers(binding, user)
    response = client.request(
        "POST",
        WORKFLOW_PROPOSALS_PATH,
        payload={
            "schemaVersion": 1,
            "requestId": request_id,
            "objective": objective,
            "context": context,
            "allowedCapabilities": allowed_capabilities,
        },
        idempotency_key=idempotency_key,
        headers=headers,
    )
    if response.get("schemaVersion") != 1 or response.get("requestId") != request_id or response.get("status") != "proposed":
        raise WorkflowProposalError(_("The gateway returned an invalid planning acknowledgement"))
    raw_descriptor, raw_graph = response.get("proposal"), response.get("graph")
    if preferred_handoff_kind in {"governed_change", "attended_browser"}:
        raw_descriptor, raw_graph = _materialize_attended_crud_bundle(
            raw_descriptor, raw_graph, attended_catalogs, allowed_capabilities,
            requested_kind=preferred_handoff_kind,
        )
    descriptor = validate_workflow_descriptor(raw_descriptor, allowed_capabilities)
    graph = validate_compiled_graph(raw_graph, descriptor, allowed_capabilities)
    run_metadata = validate_run_metadata(response.get("run"))
    canonical = json.dumps(descriptor, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    canonical_graph = json.dumps(graph, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    canonical_scope = json.dumps(reviewed_scope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    doc = frappe.get_doc({
        "doctype": "Muster Workflow Proposal",
        "objective": objective,
        "status": "Proposed",
        "requested_by": user,
        "requested_at": now_datetime(),
        "request_id": idempotency_key,
        "gateway_request_id": request_id,
        "context_json": json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True),
        "requested_scope_json": json.dumps(reviewed_scope, ensure_ascii=False, indent=2, sort_keys=True),
        "requested_scope_hash": sha256(canonical_scope.encode()).hexdigest(),
        "descriptor_json": json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True),
        "descriptor_hash": sha256(canonical.encode()).hexdigest(),
        "compiled_graph_json": json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True),
        "compiled_graph_hash": sha256(canonical_graph.encode()).hexdigest(),
        "capabilities_json": json.dumps(allowed_capabilities, ensure_ascii=False, indent=2),
        "run_metadata_json": json.dumps(run_metadata, ensure_ascii=False, indent=2, sort_keys=True) if run_metadata else None,
    })
    doc.insert()
    return {"proposal": doc.name, "status": doc.status, "replayed": False}


def publish_approved_proposal(
    proposal_name: str, root_agent: str, policy: str, idempotency_key: str
) -> dict[str, Any]:
    """Materialize reviewed IR into a native draft and immutable publication.

    This boundary performs no model call. It rechecks the requester's live
    authority and converts only the already admitted portable graph.
    """
    _bounded_text(proposal_name, "proposal", 140)
    _bounded_text(root_agent, "root agent", 140)
    _bounded_text(policy, "policy", 140)
    _bounded_text(idempotency_key, "idempotency key", 140)
    if frappe.db.db_type == "sqlite":
        frappe.db.sql("select name from `tabMuster Workflow Proposal` where name=%s", proposal_name)
    else:
        frappe.db.sql("select name from `tabMuster Workflow Proposal` where name=%s for update", proposal_name)
    proposal = frappe.get_doc("Muster Workflow Proposal", proposal_name)
    if not proposal.has_permission("write") or not frappe.has_permission("Muster Workflow", "create"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if proposal.status == "Published":
        return {
            "proposal": proposal.name,
            "workflow": proposal.published_workflow,
            "version": proposal.published_version,
            "status": proposal.status,
            "replayed": True,
            "executed": False,
        }
    if proposal.status != "Approved":
        raise WorkflowProposalError(_("Only an approved workflow proposal can be published"))
    agent = frappe.get_doc("Muster Agent", root_agent)
    if not agent.has_permission("read") or agent.status != "Active":
        raise WorkflowProposalError(_("Select an active root agent you can read"))
    policy_doc = frappe.get_doc("Muster Policy", policy)
    if not policy_doc.has_permission("read") or not policy_doc.enabled:
        raise WorkflowProposalError(_("Select an enabled policy you can read"))

    descriptor = json.loads(proposal.descriptor_json)
    # Stored maximum authority is evidence, not a permanent grant. The
    # original requester's current roles/bindings must still allow the plan.
    live_authority = _caller_capabilities(proposal.requested_by, "*")
    descriptor = validate_workflow_descriptor(descriptor, live_authority)
    graph = validate_compiled_graph(
        json.loads(proposal.compiled_graph_json), descriptor, live_authority
    )
    nodes, edges = _native_rows(graph, root_agent)
    workflow_name = _unique_workflow_name(descriptor["meta"]["name"], proposal.name)
    budget = descriptor["budget"]
    workflow = frappe.get_doc({
        "doctype": "Muster Workflow",
        "workflow_name": workflow_name,
        "status": "Draft",
        "version": 1,
        "description": descriptor["meta"]["description"],
        "root_agent": root_agent,
        "policy": policy,
        "max_duration_minutes": max(1, (int(budget["runtimeMs"]) + 59_999) // 60_000),
        "max_tool_calls": int(budget["toolCalls"]),
        "max_model_calls": int(budget["modelCalls"]),
        "max_tokens": int(budget["tokens"]),
        "max_cost": float(budget["costMicros"]) / 1_000_000,
        "max_artifact_bytes": int(budget["artifactBytes"]),
        "nodes": nodes,
        "edges": edges,
    }).insert()
    publication = publish_workflow(
        workflow.name, str(workflow.modified), f"proposal:{proposal.name}:{idempotency_key}"
    )
    proposal.db_set({
        "status": "Published",
        "published_workflow": workflow.name,
        "published_version": publication["version"],
    }, update_modified=True)
    return {
        "proposal": proposal.name,
        "workflow": workflow.name,
        "version": publication["version"],
        "snapshot_hash": publication["snapshot_hash"],
        "status": "Published",
        "replayed": False,
        "executed": False,
    }


def start_published_proposal_mission(
    proposal_name: str,
    idempotency_key: str,
    *,
    confirmed: bool | int | str,
) -> dict[str, Any]:
    """Queue one explicitly confirmed Mission from an immutable publication.

    Publication and execution intentionally remain separate boundaries. Only
    the original requester may cross this boundary, and every authority input
    is recomputed from current Frappe state before a Mission is inserted.
    """
    _bounded_text(proposal_name, "proposal", 140)
    _bounded_text(idempotency_key, "idempotency key", 140)
    if confirmed not in {True, 1, "1"}:
        raise WorkflowProposalError(_("Explicit Start confirmation is required"))
    actor = frappe.session.user
    if actor == "Guest":
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    if frappe.db.db_type == "sqlite":
        frappe.db.sql("select name from `tabMuster Workflow Proposal` where name=%s", proposal_name)
    else:
        frappe.db.sql("select name from `tabMuster Workflow Proposal` where name=%s for update", proposal_name)
    proposal = frappe.get_doc("Muster Workflow Proposal", proposal_name)
    if actor != proposal.requested_by or not proposal.has_permission("read"):
        frappe.throw(_("Only the original requester can start this workflow"), frappe.PermissionError)
    user = frappe.get_cached_doc("User", actor)
    if not user.enabled or not frappe.has_permission("Muster Mission", "create"):
        frappe.throw(_("The original requester cannot create missions"), frappe.PermissionError)
    if proposal.status != "Published" or not proposal.published_workflow or not proposal.published_version:
        raise WorkflowProposalError(_("Publish this proposal before starting a mission"))

    descriptor = _verified_proposal_snapshot(proposal, actor)
    requested_scope = _verified_requested_scope(proposal)
    workflow = frappe.get_doc("Muster Workflow", proposal.published_workflow)
    if workflow.status != "Published" or workflow.published_version != proposal.published_version:
        raise WorkflowProposalError(_("The proposal publication is no longer active"))
    if not workflow.has_permission("read", user=actor):
        frappe.throw(_("The original requester cannot read the published workflow"), frappe.PermissionError)
    policy = frappe.get_doc("Muster Policy", workflow.policy)
    if not policy.enabled:
        raise WorkflowProposalError(_("The published workflow policy is not currently active"))
    agent = frappe.get_doc("Muster Agent", workflow.root_agent)
    if agent.status != "Active" or not agent.has_permission("read", user=actor):
        raise WorkflowProposalError(_("The published workflow root agent is not currently available"))

    version = frappe.get_doc("Muster Workflow Version", proposal.published_version)
    if version.docstatus != 1 or version.workflow != workflow.name:
        raise WorkflowProposalError(_("The proposal does not reference a valid published version"))
    if not version.has_permission("read", user=actor):
        frappe.throw(_("The original requester cannot read the published version"), frappe.PermissionError)
    if (
        not isinstance(version.graph_json, str)
        or not version.snapshot_hash
        or sha256(version.graph_json.encode()).hexdigest() != version.snapshot_hash
    ):
        raise WorkflowProposalError(_("The published workflow evidence hash does not match"))

    # Validate the exact portable publication against current policy, role
    # bindings, agent declarations, and requested scope before queueing work.
    published_graph = compile_legacy_snapshot(version.graph_json)
    mission_shape = frappe._dict({
        "requested_by": actor,
        "scope_json": json.dumps(requested_scope, ensure_ascii=False, sort_keys=True),
    })
    _capability_authority(mission_shape, workflow, published_graph)

    existing = frappe.db.get_value(
        "Muster Mission", {"idempotency_key": idempotency_key},
        ["name", "status", "requested_by", "source_proposal", "workflow", "workflow_version"],
        as_dict=True,
    )
    if existing:
        if (
            existing.requested_by != actor
            or existing.source_proposal != proposal.name
            or existing.workflow != workflow.name
            or existing.workflow_version != version.name
        ):
            raise WorkflowProposalError(_("Idempotency key is already bound to another mission"))
        return {"mission": existing.name, "status": existing.status, "replayed": True}

    budget = descriptor["budget"]
    mission = frappe.get_doc({
        "doctype": "Muster Mission",
        "objective": proposal.objective,
        "workflow": workflow.name,
        "workflow_version": version.name,
        "root_agent": workflow.root_agent,
        "source_proposal": proposal.name,
        "scope_json": json.dumps(requested_scope, ensure_ascii=False, sort_keys=True),
        "requested_by": actor,
        "status": "Queued",
        "idempotency_key": idempotency_key,
        "requested_at": now_datetime(),
        "budget_json": json.dumps(budget, ensure_ascii=False, sort_keys=True),
    }).insert()
    frappe.enqueue(
        "muster.orchestration.worker.dispatch_mission",
        queue="long",
        enqueue_after_commit=True,
        mission=mission.name,
        job_id=f"muster-mission-{mission.name}",
    )
    return {"mission": mission.name, "status": mission.status, "replayed": False}


def _verified_proposal_snapshot(proposal, actor: str) -> dict[str, Any]:
    try:
        descriptor = json.loads(proposal.descriptor_json)
        compiled_graph = json.loads(proposal.compiled_graph_json)
    except (TypeError, ValueError) as error:
        raise WorkflowProposalError(_("Stored workflow proposal evidence is invalid")) from error
    canonical_descriptor = json.dumps(
        descriptor, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    canonical_graph = json.dumps(
        compiled_graph, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if sha256(canonical_descriptor.encode()).hexdigest() != proposal.descriptor_hash:
        raise WorkflowProposalError(_("Stored workflow descriptor hash does not match"))
    if sha256(canonical_graph.encode()).hexdigest() != proposal.compiled_graph_hash:
        raise WorkflowProposalError(_("Stored compiled graph hash does not match"))
    live_authority = _caller_capabilities(actor, "*")
    descriptor = validate_workflow_descriptor(descriptor, live_authority)
    validate_compiled_graph(compiled_graph, descriptor, live_authority)
    return descriptor


def _verified_requested_scope(proposal) -> dict[str, Any]:
    """Return only the immutable, reviewed resource scope attached at planning."""
    raw = proposal.requested_scope_json or "{}"
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise WorkflowProposalError(_("Stored workflow scope evidence is invalid")) from error
    normalized = _canonical_requested_scope(value)
    canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    expected = proposal.requested_scope_hash
    if not expected or sha256(canonical.encode()).hexdigest() != expected:
        raise WorkflowProposalError(_("Stored workflow scope hash does not match"))
    return normalized


def _attended_form_catalogs(scope: dict[str, Any], user: str, objective: str) -> list[dict[str, Any]]:
    """Build a bounded permission-filtered candidate catalog from page hint + prompt terms."""
    selected = scope.get("doctype")
    tokens = {
        token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", objective)
        if token.lower() not in {"the", "and", "for", "with", "from", "into", "this", "that", "create", "update", "change", "edit", "read", "show", "open", "record", "document", "new"}
    }
    names: set[str] = {selected} if selected else set()
    if tokens:
        rows = frappe.get_all(
            "DocType", filters={"istable": 0},
            or_filters=[["name", "like", f"%{token}%"] for token in sorted(tokens)],
            fields=["name"], order_by="name asc", limit_page_length=50,
        )
        names.update(str(row.name if hasattr(row, "name") else row.get("name")) for row in rows if (row.name if hasattr(row, "name") else row.get("name")))
    normalized_objective = re.sub(r"[^a-z0-9]+", " ", objective.lower()).strip()
    ranked = sorted(names, key=lambda name: (
        0 if name == selected else 1,
        -(20 if re.sub(r"[^a-z0-9]+", " ", name.lower()).strip() in normalized_objective else 0)
        - len(set(re.findall(r"[a-z0-9]+", name.lower())) & tokens),
        name.lower(),
    ))[:6]
    catalogs = []
    for doctype in ranked:
        try:
            catalogs.append(_attended_form_catalog(doctype, scope.get("docname") if doctype == selected else None, user))
        except (frappe.PermissionError, MusterFormSchemaError):
            continue
    return catalogs


def _attended_form_catalog(doctype: str, record_name: str | None, user: str) -> dict[str, Any]:
    snapshot = effective_form_schema(doctype, user=user)
    fields = [
        {
            "fieldname": field["fieldname"], "label": field["label"],
            "fieldtype": field["fieldtype"], "required": field["required"],
            "has_default": field["has_default"], "writable": field["writable"],
        }
        for field in snapshot["fields"][:120]
    ]
    actions = ["read"]
    if snapshot["authority"]["create"]:
        actions.append("create")
    if record_name and snapshot["authority"]["write"]:
        actions.append("update")
    return {
        "doctype": doctype, "record_name": record_name, "actions": actions,
        "authority": snapshot["authority"], "fields": fields,
        "schema_hash": snapshot["schema_hash"], "revision": snapshot["revision"],
    }


def _materialize_attended_crud_bundle(
    descriptor_value: Any, graph_value: Any, catalogs: list[dict[str, Any]] | dict[str, Any] | None,
    allowed_capabilities: list[str], *, requested_kind: str = "governed_change",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace a model-selected record intent with a host-authored Desk plan.

    The provider may choose create/update and scalar values only from the
    supplied catalog. Routes, labels, semantic actions, schema hashes and
    revision evidence are authored here and never accepted from model output.
    """
    if isinstance(catalogs, dict):
        catalogs = [catalogs]
    if not catalogs:
        raise WorkflowProposalError(_("Name a live-readable DocType before preparing this attended change"))
    try:
        descriptor = json.loads(json.dumps(descriptor_value, ensure_ascii=False, allow_nan=False))
        graph = json.loads(json.dumps(graph_value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise WorkflowProposalError(_("The attended workflow proposal is not valid JSON")) from error
    available = set(allowed_capabilities)
    converted = 0

    def convert(execution: Any, capabilities: Any) -> tuple[dict[str, Any], list[str], bool]:
        if not isinstance(execution, dict) or not isinstance(execution.get("plan"), dict):
            return execution, capabilities, False
        if execution.get("surface") == "browser":
            if requested_kind != "attended_browser":
                raise WorkflowProposalError(_("A governed change must select values through the host form catalog"))
            raw_actions = execution["plan"].get("actions")
            if not isinstance(raw_actions, list) or any(not isinstance(item, dict) or item.get("kind") not in {"navigate", "read_visible"} for item in raw_actions):
                raise WorkflowProposalError(_("An attended read may not smuggle model-authored form mutations"))
            model_doctypes = {item.get("doctype") for item in raw_actions if isinstance(item.get("doctype"), str)}
            candidates = [catalog for catalog in catalogs if catalog["doctype"] in model_doctypes] if model_doctypes else catalogs
            if len(candidates) != 1:
                raise WorkflowProposalError(_("The attended read target is ambiguous; name one permitted DocType"))
            plan = _host_attended_read_plan(candidates[0])
            required = _browser_plan_capabilities(plan)
            if "*" not in available and not required.issubset(available):
                raise WorkflowProposalError(_("The live actor lacks an attended browser capability"))
            return {"surface": "browser", "plan": plan}, sorted(required), True
        if execution.get("surface") != "server_effect":
            return execution, capabilities, False
        intent = execution["plan"]
        operation = intent.get("operation")
        if intent.get("capability") not in {"frappe.record.create", "frappe.record.update"} or not isinstance(operation, dict):
            return execution, capabilities, False
        action = operation.get("action")
        catalog = next((item for item in catalogs if item["doctype"] == operation.get("doctype")), None)
        if not catalog or operation.get("kind") != "record" or action not in {"create", "update"}:
            raise WorkflowProposalError(_("The change selected a DocType or action outside the live form catalog"))
        if action not in catalog["actions"]:
            raise WorkflowProposalError(_("The live actor cannot perform this form action"))
        values = operation.get("values")
        if not isinstance(values, dict) or not values or len(values) > 100:
            raise WorkflowProposalError(_("The attended change requires bounded form values"))
        plan = _host_attended_browser_plan(action, values, catalog)
        required = _browser_plan_capabilities(plan)
        if "*" not in available and not required.issubset(available):
            raise WorkflowProposalError(_("The live actor lacks an attended browser capability"))
        return {"surface": "browser", "plan": plan}, sorted(required), True

    def walk_steps(steps: Any) -> None:
        nonlocal converted
        if not isinstance(steps, list):
            return
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("kind") == "execution":
                execution, capabilities, changed = convert(step.get("execution"), step.get("capabilities"))
                if changed:
                    step["execution"], step["capabilities"] = execution, capabilities
                    converted += 1
            for key in ("steps", "branches", "subagents"):
                walk_steps(step.get(key))

    walk_steps(descriptor.get("steps") if isinstance(descriptor, dict) else None)
    graph_converted = 0
    for node in graph.get("nodes", []) if isinstance(graph, dict) else []:
        execution, capabilities, changed = convert(node.get("executionIntent"), node.get("requestedCapabilities"))
        if changed:
            node["executionIntent"], node["requestedCapabilities"] = execution, capabilities
            graph_converted += 1
    if converted < 1 or converted != graph_converted:
        raise WorkflowProposalError(_("The governed change did not compile to one matching attended CRUD plan"))
    return descriptor, graph


def _browser_plan_capabilities(plan: dict[str, Any]) -> set[str]:
    mapping = {
        "navigate": "frappe.browser.navigate", "click": "frappe.browser.click",
        "fill": "frappe.browser.fill", "select": "frappe.browser.select",
        "read_visible": "frappe.browser.read_visible",
    }
    return {mapping[action["kind"]] for action in plan["actions"]}


def _host_attended_browser_plan(action: str, values: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    fields = {field["fieldname"]: field for field in catalog["fields"]}
    labels = [field["label"] for field in catalog["fields"]]
    for fieldname, value in values.items():
        field = fields.get(fieldname)
        if not field or not field["writable"] or labels.count(field["label"]) != 1:
            raise WorkflowProposalError(_("A selected form field is unavailable, ambiguous, hidden, read-only, or denied by permlevel"))
        if isinstance(value, (dict, list)) or value is None:
            raise WorkflowProposalError(_("Attended CRUD v1 accepts scalar visible form values only"))
        if field["fieldtype"] not in {"Data", "Small Text", "Text", "Long Text", "Link", "Dynamic Link", "Date", "Datetime", "Time", "Int", "Float", "Currency", "Percent", "Phone", "Select", "Autocomplete"}:
            raise WorkflowProposalError(_("A selected field type is not safely supported by attended CRUD v1"))
    if action == "create":
        missing = [field["label"] for field in catalog["fields"] if field["required"] and field["writable"] and not field["has_default"] and field["fieldname"] not in values]
        if missing:
            raise WorkflowProposalError(_("The attended create is missing required live form fields: {0}").format(", ".join(missing[:10])))
    record_name = catalog["record_name"] if action == "update" else None
    if action == "update" and not record_name:
        raise WorkflowProposalError(_("Select an exact permitted record before preparing an attended update"))
    doctype_slug = frappe.scrub(catalog["doctype"]).replace("_", "-")
    list_route = f"/desk/{doctype_slug}"
    form_route = f"{list_route}/{quote(record_name, safe='')}" if record_name else "@attended-form"
    actions: list[dict[str, Any]] = []
    if action == "create":
        actions.extend([
            {"kind": "navigate", "route": list_route, "doctype": catalog["doctype"]},
            {"kind": "click", "route": list_route, "doctype": catalog["doctype"], "target": {"kind": "role", "role": "button", "name": "New"}, "postcondition": {"kind": "bind_route", "token": "attended_form", "doctype": catalog["doctype"]}},
        ])
    else:
        actions.append({"kind": "navigate", "route": form_route, "doctype": catalog["doctype"], "recordName": record_name})
    for fieldname in sorted(values):
        field = fields[fieldname]
        target = {"kind": "label", "name": field["label"]}
        common = {"route": form_route, "doctype": catalog["doctype"], **({"recordName": record_name} if record_name else {}), "target": target, "field": fieldname, "postcondition": {"kind": "target", "target": target, "state": "visible"}}
        if field["fieldtype"] == "Select":
            actions.append({"kind": "select", **common, "option": str(values[fieldname])})
        else:
            actions.append({"kind": "fill", **common, "value": str(values[fieldname])})
    actions.append({
        "kind": "click", "route": form_route, "doctype": catalog["doctype"],
        **({"recordName": record_name} if record_name else {}),
        "target": {"kind": "role", "role": "button", "name": "Save"},
        "postcondition": {"kind": "record_saved", "doctype": catalog["doctype"], "recordName": record_name},
    })
    return browser_action_plan({
        "schemaVersion": 1, "actionBudget": len(actions), "actions": actions,
        "attendedCrud": {
            "operation": action, "doctype": catalog["doctype"], "record_name": record_name,
            "fields": sorted(values), "schema_hash": catalog["schema_hash"], "revision": catalog["revision"],
        },
    })


def _host_attended_read_plan(catalog: dict[str, Any]) -> dict[str, Any]:
    encoded_doctype = frappe.scrub(catalog["doctype"]).replace("_", "-")
    record_name = catalog.get("record_name")
    route = f"/desk/{encoded_doctype}" + (f"/{quote(record_name, safe='')}" if record_name else "")
    return browser_action_plan({
        "schemaVersion": 1, "actionBudget": 2,
        "actions": [
            {"kind": "navigate", "route": route, "doctype": catalog["doctype"], **({"recordName": record_name} if record_name else {})},
            {"kind": "read_visible", "route": route, "doctype": catalog["doctype"], **({"recordName": record_name} if record_name else {}), "maxChars": 10_000},
        ],
        "attendedCrud": {"operation": "read", "doctype": catalog["doctype"], "record_name": record_name, "fields": [], "schema_hash": catalog["schema_hash"], "revision": catalog["revision"]},
    })


def _canonical_requested_scope(value: Any) -> dict[str, Any]:
    """Admit a small resource selector; never persist arbitrary prompt payloads.

    Permission-filtered record contents belong in ``context_json``. This value
    contains only route/resource identities used to re-check policy at Start
    and dispatch boundaries.
    """
    if not isinstance(value, dict):
        raise WorkflowProposalError(_("Planning scope must be a JSON object"))
    scalar_fields = {
        "source": 80, "route": 500, "page_type": 140, "page_name": 140,
        "doctype": 140, "docname": 500, "locale": 40, "timezone": 80,
        "scope_mode": 20,
    }
    allowed = {*scalar_fields, "documents", "fields"}
    if set(value) - allowed:
        raise WorkflowProposalError(_("Planning scope contains unsupported fields"))
    normalized: dict[str, Any] = {}
    for field, maximum in scalar_fields.items():
        if value.get(field) is not None:
            normalized[field] = _bounded_text(value[field], field, maximum)
    if normalized.get("scope_mode") not in {None, "context", "site", "doctype", "record"}:
        raise WorkflowProposalError(_("Planning scope mode is invalid"))
    if "docname" in normalized and "doctype" not in normalized:
        raise WorkflowProposalError(_("Planning scope document name requires a DocType"))
    fields = value.get("fields") or []
    if not isinstance(fields, list) or len(fields) > 256:
        raise WorkflowProposalError(_("Planning scope fields are invalid or excessive"))
    admitted_fields: list[str] = []
    for item in fields:
        field = _bounded_text(item, "field", 140)
        if field not in admitted_fields:
            admitted_fields.append(field)
    if admitted_fields:
        normalized["fields"] = admitted_fields
    documents = value.get("documents") or []
    if not isinstance(documents, list) or len(documents) > MAX_SCOPE_DOCUMENTS:
        raise WorkflowProposalError(_("Planning scope documents are invalid or excessive"))
    admitted_documents: list[dict[str, str]] = []
    for row in documents:
        if not isinstance(row, dict) or set(row) - {"doctype", "name", "docname", "fields"}:
            raise WorkflowProposalError(_("Each planning scope document must be an exact resource reference"))
        doctype = _bounded_text(row.get("doctype"), "DocType", 140)
        name = _bounded_text(row.get("name") or row.get("docname"), "document name", 500)
        row_fields = row.get("fields") or []
        if not isinstance(row_fields, list) or len(row_fields) > 256:
            raise WorkflowProposalError(_("Planning scope document fields are invalid or excessive"))
        normalized_row_fields: list[str] = []
        for item in row_fields:
            field = _bounded_text(item, "document field", 140)
            if field not in normalized_row_fields:
                normalized_row_fields.append(field)
        reference = {
            "doctype": doctype,
            "name": name,
            **({"fields": normalized_row_fields} if normalized_row_fields else {}),
        }
        if reference not in admitted_documents:
            admitted_documents.append(reference)
    if admitted_documents:
        normalized["documents"] = admitted_documents
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode()) > MAX_SCOPE_BYTES:
        raise WorkflowProposalError(_("Planning scope exceeds the safe size limit"))
    return normalized


def _native_rows(graph: dict[str, Any], root_agent: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    node_type = {
        "agent": "Agent", "approval": "Approval", "parallel_map": "Parallel",
        "transform": "Join", "condition": "Condition", "loop": "Bounded Loop",
        "artifact": "Artifact",
    }
    nodes = []
    for raw in graph["nodes"]:
        assigned_agent = raw.get("agentId") if raw["kind"] == "agent" else None
        if assigned_agent:
            if not frappe.db.exists("Muster Agent", assigned_agent) or frappe.db.get_value("Muster Agent", assigned_agent, "status") != "Active":
                raise WorkflowProposalError(_("The proposal references an unavailable agent: {0}").format(assigned_agent))
        elif raw["kind"] == "agent":
            assigned_agent = root_agent
        configuration = {
            "core_kind": raw["kind"],
            "requested_capabilities": raw.get("requestedCapabilities") or [],
        }
        if raw.get("compensationNodeId"):
            configuration["compensation_node_id"] = raw["compensationNodeId"]
        if raw.get("loop"):
            configuration.update({
                "max_iterations": raw["loop"]["maxIterations"],
                "progress_predicate": raw["loop"]["progressPredicate"],
                "budget": raw["loop"]["budget"],
            })
        execution = raw.get("executionIntent")
        if execution:
            if not isinstance(execution, dict) or set(execution) != {"surface", "plan"}:
                raise WorkflowProposalError(_("The proposal execution intent is invalid"))
            if execution["surface"] == "server_effect":
                configuration["effect_intent"] = effect_intent(execution["plan"])
            elif execution["surface"] == "browser":
                configuration["browser_action_plan"] = browser_action_plan(execution["plan"])
            else:
                raise WorkflowProposalError(_("The proposal execution surface is unsupported"))
        nodes.append({
            "node_id": raw["id"],
            "label": _graph_node_label(raw["id"]),
            "node_type": node_type.get(raw["kind"], "Tool"),
            "agent": assigned_agent,
            "configuration_json": json.dumps(configuration, ensure_ascii=False, sort_keys=True),
            "approval_class": "Standard",
            "timeout_seconds": 600,
            "retry_limit": int(raw.get("retryLimit") or 0),
        })
    edges = [
        {
            "source_node": edge["from"], "target_node": edge["to"],
            "condition_expression": edge.get("when"), "priority": 100,
        }
        for edge in graph["edges"]
    ]
    return nodes, edges


def _graph_node_label(node_id: str) -> str:
    stem = node_id.split("-", 1)[1] if "-" in node_id else node_id
    return re.sub(r"[-_]+", " ", stem).strip().title()[:140] or node_id


def _unique_workflow_name(label: str, proposal_name: str) -> str:
    base = re.sub(r"\s+", " ", label).strip()[:100] or "Muster workflow"
    if not frappe.db.exists("Muster Workflow", base):
        return base
    suffix = re.sub(r"[^A-Za-z0-9-]+", "-", proposal_name).strip("-")[-24:]
    candidate = f"{base[:110]} · {suffix}"
    if frappe.db.exists("Muster Workflow", candidate):
        raise WorkflowProposalError(_("This proposal already conflicts with an existing workflow name"))
    return candidate


def validate_workflow_descriptor(value: Any, allowed_capabilities: list[str]) -> dict[str, Any]:
    """Independent Frappe-side admission gate for untrusted planner output.

    The TypeScript gateway is the canonical compiler. This second boundary is
    intentionally redundant so a compromised/misconfigured gateway cannot
    persist source code or expand the caller's live Frappe authority.
    """
    if isinstance(value, str):
        raise WorkflowProposalError(_("Workflow source is forbidden; the proposal must be strict JSON data"))
    if not isinstance(value, dict):
        raise WorkflowProposalError(_("Workflow proposal must be a JSON object"))
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise WorkflowProposalError(_("Workflow proposal contains a non-JSON value")) from error
    if len(encoded.encode()) > MAX_DESCRIPTOR_BYTES:
        raise WorkflowProposalError(_("Workflow proposal exceeds the safe size limit"))
    _exact_keys(value, {"schemaVersion", "id", "version", "meta", "goal", "inputSchema", "resultSchema", "budget", "limits", "steps"}, "proposal")
    if value.get("schemaVersion") != 1:
        raise WorkflowProposalError(_("Workflow proposal schemaVersion must be 1"))
    for field in ("id", "version", "goal"):
        _bounded_text(value.get(field), field, 10_000)
    meta = value.get("meta")
    if not isinstance(meta, dict):
        raise WorkflowProposalError(_("Workflow proposal meta must be an object"))
    _exact_keys(meta, {"name", "description", "phases"}, "meta")
    _bounded_text(meta.get("name"), "meta.name", 500)
    _bounded_text(meta.get("description"), "meta.description", 4_000)
    if not isinstance(meta.get("phases"), list) or len(meta["phases"]) > 16:
        raise WorkflowProposalError(_("Workflow proposal phases are invalid"))
    for phase in meta["phases"]:
        if not isinstance(phase, dict):
            raise WorkflowProposalError(_("Workflow proposal phase must be an object"))
        _exact_keys(phase, {"title", "detail"}, "meta.phases")
        _bounded_text(phase.get("title"), "phase title", 500)
        if phase.get("detail") is not None:
            _bounded_text(phase.get("detail"), "phase detail", 4_000)
    _validate_budget(value.get("budget"), WORKFLOW_BUDGET_CEILINGS)
    limits = value.get("limits")
    if not isinstance(limits, dict):
        raise WorkflowProposalError(_("Workflow proposal limits must be an object"))
    _exact_keys(limits, {"maxDepth", "maxChildrenPerNode", "maxActiveNodes", "maxRetries", "maxParallelism", "maxPhases", "maxSteps"}, "limits")
    for key, ceiling in limits.items():
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1 or ceiling > WORKFLOW_LIMIT_CEILINGS[key]:
            raise WorkflowProposalError(_("Workflow proposal limit {0} is invalid").format(key))
    authority = set(_validate_capabilities(allowed_capabilities))
    state = {"count": 0}
    _validate_steps(value.get("steps"), authority, state, 1, value["budget"])
    if state["count"] > min(MAX_STEPS, int(limits.get("maxSteps", MAX_STEPS))):
        raise WorkflowProposalError(_("Workflow proposal contains too many steps"))
    if value.get("inputSchema") is not None:
        _validate_schema(value["inputSchema"], "inputSchema")
    _validate_schema(value.get("resultSchema"), "resultSchema")
    return json.loads(encoded)


def validate_compiled_graph(
    value: Any, descriptor: dict[str, Any], allowed_capabilities: list[str]
) -> dict[str, Any]:
    """Admit the gateway compiler output as bounded data, independently in Frappe."""
    if not isinstance(value, dict):
        raise WorkflowProposalError(_("Compiled workflow graph must be a JSON object"))
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise WorkflowProposalError(_("Compiled workflow graph contains a non-JSON value")) from error
    if len(encoded.encode()) > MAX_DESCRIPTOR_BYTES:
        raise WorkflowProposalError(_("Compiled workflow graph exceeds the safe size limit"))
    _exact_keys(value, {"schemaVersion", "id", "version", "entryNodeId", "nodes", "edges", "budget", "limits"}, "compiled graph")
    if value.get("schemaVersion") != 1 or value.get("id") != descriptor.get("id") or value.get("version") != descriptor.get("version"):
        raise WorkflowProposalError(_("Compiled workflow graph identity does not match its proposal"))
    _validate_budget(value.get("budget"), descriptor.get("budget"))
    limits = value.get("limits")
    if not isinstance(limits, dict):
        raise WorkflowProposalError(_("Compiled workflow graph limits are invalid"))
    _exact_keys(limits, {"maxDepth", "maxChildrenPerNode", "maxActiveNodes", "maxRetries"}, "compiled graph limits")
    for key, graph_limit in limits.items():
        descriptor_limit = descriptor.get("limits", {}).get(key)
        if not isinstance(graph_limit, int) or isinstance(graph_limit, bool) or graph_limit < 1 or graph_limit != descriptor_limit:
            raise WorkflowProposalError(_("Compiled workflow graph limits do not match its proposal"))

    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not nodes or len(nodes) > min(MAX_STEPS * 2, limits.get("maxActiveNodes", MAX_STEPS * 2)):
        raise WorkflowProposalError(_("Compiled workflow graph nodes are invalid"))
    if not isinstance(edges, list) or len(edges) > MAX_STEPS * MAX_STEPS:
        raise WorkflowProposalError(_("Compiled workflow graph edges are invalid"))
    authority = set(_validate_capabilities(allowed_capabilities))
    node_ids: set[str] = set()
    adjacency: dict[str, list[str]] = {}
    graph_capabilities: Counter[str] = Counter()
    graph_executions: Counter[str] = Counter()
    for node in nodes:
        if not isinstance(node, dict):
            raise WorkflowProposalError(_("Compiled workflow graph node must be an object"))
        _exact_keys(node, {"id", "kind", "agentId", "requestedCapabilities", "retryLimit", "compensationNodeId", "loop", "executionIntent"}, "compiled graph node")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not GRAPH_ID_PATTERN.fullmatch(node_id) or node_id in node_ids:
            raise WorkflowProposalError(_("Compiled workflow graph node id is invalid"))
        node_ids.add(node_id)
        adjacency[node_id] = []
        if node.get("kind") not in GRAPH_NODE_KINDS:
            raise WorkflowProposalError(_("Compiled workflow graph node kind is invalid"))
        if node.get("agentId") is not None and (
            not isinstance(node["agentId"], str) or not GRAPH_ID_PATTERN.fullmatch(node["agentId"])
        ):
            raise WorkflowProposalError(_("Compiled workflow graph agent id is invalid"))
        retry = node.get("retryLimit", 0)
        if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0 or retry > limits["maxRetries"]:
            raise WorkflowProposalError(_("Compiled workflow graph retry limit is invalid"))
        requested = _validate_capabilities(node.get("requestedCapabilities") or [])
        if any("*" not in authority and capability not in authority for capability in requested):
            raise WorkflowProposalError(_("Compiled workflow graph exceeds caller capability authority"))
        graph_capabilities.update(requested)
        execution = node.get("executionIntent")
        if execution is not None:
            if node.get("kind") != "command" or not isinstance(execution, dict) or set(execution) != {"surface", "plan"}:
                raise WorkflowProposalError(_("Compiled workflow execution intent is invalid"))
            if execution.get("surface") == "server_effect":
                admitted = effect_intent(execution.get("plan"), "compiled graph execution intent")
                if requested != [admitted["capability"]]:
                    raise WorkflowProposalError(_("Compiled effect capability does not match its authority"))
            elif execution.get("surface") == "browser":
                plan = browser_action_plan(execution.get("plan"), "compiled graph browser plan")
                required = {
                    {
                        "navigate": "frappe.browser.navigate", "click": "frappe.browser.click",
                        "fill": "frappe.browser.fill", "select": "frappe.browser.select",
                        "upload": "frappe.browser.upload", "screenshot": "frappe.browser.screenshot",
                        "read_visible": "frappe.browser.read_visible",
                    }[action["kind"]]
                    for action in plan["actions"]
                }
                if not required.issubset(set(requested)):
                    raise WorkflowProposalError(_("Compiled browser plan exceeds its capability authority"))
            else:
                raise WorkflowProposalError(_("Compiled workflow execution surface is unsupported"))
            graph_executions.update([json.dumps(execution, ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
        loop = node.get("loop")
        if node.get("kind") == "loop":
            if not isinstance(loop, dict):
                raise WorkflowProposalError(_("Compiled workflow loop controls are missing"))
            _exact_keys(loop, {"maxIterations", "progressPredicate", "cancellationCheckpoint", "budget"}, "compiled graph loop")
            if not isinstance(loop.get("maxIterations"), int) or not 1 <= loop["maxIterations"] <= 100 or loop.get("cancellationCheckpoint") is not True:
                raise WorkflowProposalError(_("Compiled workflow loop is not safely bounded"))
            _bounded_text(loop.get("progressPredicate"), "compiled loop progress predicate", 10_000)
            _validate_budget(loop.get("budget"), descriptor.get("budget"))
        elif loop is not None:
            raise WorkflowProposalError(_("Only compiled loop nodes may declare loop controls"))

    entry = value.get("entryNodeId")
    if entry not in node_ids:
        raise WorkflowProposalError(_("Compiled workflow graph entry node is invalid"))
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise WorkflowProposalError(_("Compiled workflow graph edge must be an object"))
        _exact_keys(edge, {"from", "to", "when"}, "compiled graph edge")
        source, target = edge.get("from"), edge.get("to")
        if source not in node_ids or target not in node_ids or source == target:
            raise WorkflowProposalError(_("Compiled workflow graph edge is invalid"))
        when = edge.get("when") or ""
        if not isinstance(when, str) or len(when) > 10_000 or (source, target, when) in seen_edges:
            raise WorkflowProposalError(_("Compiled workflow graph edge is invalid"))
        seen_edges.add((source, target, when))
        adjacency[source].append(target)
    if any(len(children) > limits["maxChildrenPerNode"] for children in adjacency.values()):
        raise WorkflowProposalError(_("Compiled workflow graph fan-out exceeds its limit"))
    visiting: set[str] = set()
    visited: set[str] = set()
    def walk(node_id: str) -> None:
        if node_id in visiting:
            raise WorkflowProposalError(_("Compiled workflow graph contains a raw cycle"))
        if node_id in visited:
            return
        visiting.add(node_id)
        for child in adjacency[node_id]:
            walk(child)
        visiting.remove(node_id)
        visited.add(node_id)
    walk(entry)
    if visited != node_ids:
        raise WorkflowProposalError(_("Compiled workflow graph contains an unreachable node"))

    descriptor_capabilities: Counter[str] = Counter()
    descriptor_executions: Counter[str] = Counter()
    def collect(steps: list[dict[str, Any]]) -> None:
        for step in steps:
            descriptor_capabilities.update(step.get("capabilities") or [])
            if step.get("kind") == "execution":
                descriptor_executions.update([json.dumps(step.get("execution"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))])
            for key in ("steps", "branches", "subagents"):
                if isinstance(step.get(key), list):
                    collect(step[key])
    collect(descriptor["steps"])
    if graph_capabilities != descriptor_capabilities:
        raise WorkflowProposalError(_("Compiled workflow graph capability evidence does not match its proposal"))
    if graph_executions != descriptor_executions:
        raise WorkflowProposalError(_("Compiled workflow execution evidence does not match its proposal"))
    return json.loads(encoded)


def _validate_steps(steps: Any, authority: set[str], state: dict[str, int], depth: int, workflow_budget: dict[str, Any]) -> None:
    if depth > MAX_DEPTH or not isinstance(steps, list) or not steps:
        raise WorkflowProposalError(_("Workflow proposal step structure is invalid"))
    for step in steps:
        state["count"] += 1
        if not isinstance(step, dict):
            raise WorkflowProposalError(_("Workflow proposal step must be an object"))
        kind = step.get("kind")
        if kind not in {"agent", "subworkflow", "phase", "parallel", "approval", "verification", "compensation", "repeat", "execution"}:
            raise WorkflowProposalError(_("Workflow proposal contains an unsupported step"))
        allowed = {"kind", "label", "description", "capabilities", "retryLimit", "resultSchema", "compensation"}
        allowed.update({
            "agent": {"prompt", "agentId", "agentType", "subagents"},
            "subworkflow": {"workflowId", "goal", "steps"},
            "phase": {"detail", "steps"},
            "parallel": {"maxConcurrency", "branches"},
            "approval": {"prompt", "requiredRoles"},
            "verification": {"criteria"},
            "compensation": {"action"},
            "repeat": {"maxIterations", "progressPredicate", "cancellationCheckpoint", "budget", "steps"},
            "execution": {"execution"},
        }[kind])
        _exact_keys(step, allowed, "step")
        _bounded_text(step.get("label"), "step label", 500)
        if step.get("description") is not None:
            _bounded_text(step["description"], "step description", 10_000)
        if step.get("retryLimit") is not None and (
            not isinstance(step["retryLimit"], int) or isinstance(step["retryLimit"], bool) or step["retryLimit"] < 0
        ):
            raise WorkflowProposalError(_("Workflow proposal retry limit is invalid"))
        if step.get("resultSchema") is not None:
            _validate_schema(step["resultSchema"], "step.resultSchema")
        requested = _validate_capabilities(step.get("capabilities") or [])
        escalated = [capability for capability in requested if "*" not in authority and capability not in authority]
        if escalated:
            raise WorkflowProposalError(_("Workflow proposal exceeds caller capability authority"))
        if kind == "repeat":
            if not isinstance(step.get("maxIterations"), int) or step["maxIterations"] < 1 or step.get("cancellationCheckpoint") is not True:
                raise WorkflowProposalError(_("Workflow repeat step is not safely bounded"))
            _bounded_text(step.get("progressPredicate"), "repeat progress predicate", 10_000)
            _validate_budget(step.get("budget"), workflow_budget)
            _validate_steps(step.get("steps"), authority, state, depth + 1, workflow_budget)
        elif kind == "phase":
            _validate_steps(step.get("steps"), authority, state, depth + 1, workflow_budget)
        elif kind == "parallel":
            if not isinstance(step.get("maxConcurrency"), int) or step["maxConcurrency"] < 1:
                raise WorkflowProposalError(_("Parallel step concurrency is invalid"))
            _validate_steps(step.get("branches"), authority, state, depth + 1, workflow_budget)
        elif kind == "agent":
            _bounded_text(step.get("prompt"), "agent prompt", 50_000)
            if step.get("subagents") is not None:
                _validate_steps(step["subagents"], authority, state, depth + 1, workflow_budget)
        elif kind == "subworkflow":
            _bounded_text(step.get("workflowId"), "workflow id", 500)
            _bounded_text(step.get("goal"), "subworkflow goal", 10_000)
            if step.get("steps") is not None:
                _validate_steps(step["steps"], authority, state, depth + 1, workflow_budget)
        elif kind == "approval":
            _bounded_text(step.get("prompt"), "approval prompt", 10_000)
            roles = step.get("requiredRoles")
            if roles is not None and (not isinstance(roles, list) or any(not isinstance(role, str) or not role.strip() for role in roles)):
                raise WorkflowProposalError(_("Workflow approval roles are invalid"))
        elif kind == "verification":
            _bounded_text(step.get("criteria"), "verification criteria", 10_000)
        elif kind == "compensation":
            _bounded_text(step.get("action"), "compensation action", 10_000)
        elif kind == "execution":
            execution = step.get("execution")
            if not isinstance(execution, dict) or set(execution) != {"surface", "plan"}:
                raise WorkflowProposalError(_("Workflow execution step is invalid"))
            if execution.get("surface") == "server_effect":
                admitted = effect_intent(execution.get("plan"), "step.execution.plan")
                if requested != [admitted["capability"]]:
                    raise WorkflowProposalError(_("Workflow effect capability does not exactly match authority"))
            elif execution.get("surface") == "browser":
                plan = browser_action_plan(execution.get("plan"), "step.execution.plan")
                required = {
                    {
                        "navigate": "frappe.browser.navigate", "click": "frappe.browser.click",
                        "fill": "frappe.browser.fill", "select": "frappe.browser.select",
                        "upload": "frappe.browser.upload", "screenshot": "frappe.browser.screenshot",
                        "read_visible": "frappe.browser.read_visible",
                    }[action["kind"]]
                    for action in plan["actions"]
                }
                if not required.issubset(set(requested)):
                    raise WorkflowProposalError(_("Workflow browser plan exceeds capability authority"))
            else:
                raise WorkflowProposalError(_("Workflow execution surface is unsupported"))


def _validate_budget(value: Any, value_ceiling: dict[str, int] | None = WORKFLOW_BUDGET_CEILINGS) -> None:
    fields = {"runtimeMs", "toolCalls", "modelCalls", "tokens", "costMicros", "artifactBytes"}
    if not isinstance(value, dict):
        raise WorkflowProposalError(_("Workflow proposal budget must be an object"))
    _exact_keys(value, fields, "budget")
    if set(value) != fields or any(not isinstance(item, int | float) or isinstance(item, bool) or item < 0 for item in value.values()):
        raise WorkflowProposalError(_("Workflow proposal budget is invalid"))
    if value_ceiling and any(value[field] > value_ceiling[field] for field in fields):
        raise WorkflowProposalError(_("Workflow proposal budget exceeds the safe planning ceiling"))


def _validate_capabilities(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise WorkflowProposalError(_("Workflow capabilities are invalid"))
    result = []
    for capability in value:
        if not isinstance(capability, str) or not CAPABILITY_PATTERN.fullmatch(capability):
            raise WorkflowProposalError(_("Workflow capabilities are invalid"))
        if capability not in result:
            result.append(capability)
    return result


def validate_run_metadata(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkflowProposalError(_("Workflow planner run metadata is invalid"))
    _exact_keys(value, {"runId", "providerId", "model", "runtimeId", "durationMs", "inputTokens", "outputTokens", "executionBoundary"}, "run metadata")
    for field in ("runId", "providerId", "model", "runtimeId"):
        _bounded_text(value.get(field), field, 500)
    if value.get("executionBoundary") != "read-only-offline-provider":
        raise WorkflowProposalError(_("Workflow planner execution boundary is invalid"))
    for field in ("durationMs", "inputTokens", "outputTokens"):
        item = value.get(field)
        if item is not None and (not isinstance(item, int | float) or isinstance(item, bool) or item < 0):
            raise WorkflowProposalError(_("Workflow planner run metadata is invalid"))
    return value


def _validate_schema(value: Any, label: str, depth: int = 0) -> None:
    if depth > 12 or not isinstance(value, dict):
        raise WorkflowProposalError(_("Workflow {0} is invalid").format(label))
    _exact_keys(value, SCHEMA_KEYS, label)
    properties = value.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or len(properties) > 256:
            raise WorkflowProposalError(_("Workflow {0} properties are invalid").format(label))
        for name, schema in properties.items():
            if not isinstance(name, str) or not name or len(name) > 500:
                raise WorkflowProposalError(_("Workflow {0} property name is invalid").format(label))
            _validate_schema(schema, f"{label}.properties", depth + 1)
    items = value.get("items")
    if items is not None:
        _validate_schema(items, f"{label}.items", depth + 1)
    additional = value.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        _validate_schema(additional, f"{label}.additionalProperties", depth + 1)
    for composition in ("oneOf", "anyOf", "allOf"):
        schemas = value.get(composition)
        if schemas is not None:
            if not isinstance(schemas, list) or not schemas or len(schemas) > 32:
                raise WorkflowProposalError(_("Workflow {0}.{1} is invalid").format(label, composition))
            for schema in schemas:
                _validate_schema(schema, f"{label}.{composition}", depth + 1)


def _exact_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    if set(value) - allowed:
        raise WorkflowProposalError(_("{0} contains an unknown field").format(label))


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise WorkflowProposalError(_("{0} is invalid").format(label))
    return value.strip()


def _stable_request_id(idempotency_key: str, user: str) -> str:
    digest = sha256(f"{frappe.local.site}\0{user.lower()}\0{idempotency_key}".encode()).hexdigest()
    return f"frappe-plan-{digest[:40]}"
