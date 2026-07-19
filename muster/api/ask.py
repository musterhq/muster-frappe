from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from muster.adapters.client import GatewayClient, trusted_binding
from muster.adapters.context import permission_filtered_context
from muster.adapters.identity import frappe_identity
from muster.adapters.run_authority import run_authority_headers
from muster.orchestration.read_plan import (
    READ_PLANS_PATH,
    build_read_catalog,
    execute_read_plan,
    merge_read_evidence,
)
from muster.orchestration.form_schema import effective_form_schema

ASYNC_PATH = "/v1/integrations/frappe/messages/async"
ASYNC_RUNS_PATH = "/v1/integrations/frappe/messages/runs"
ASK_INTENTS_PATH = "/v1/integrations/frappe/ask-intents"
RUN_ID = re.compile(r"^msg_[A-Za-z0-9-]+$")
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}$")
MAX_PROMPT_CHARS = 100_000
_LIVE_READ_REQUEST = re.compile(
    r"\b(?:how many|count|total|sum|average|list|show me|find|which|overdue|pending|outstanding|latest|recent|current status)\b",
    re.IGNORECASE,
)
_FORM_SCHEMA_REQUEST = re.compile(
    r"\b(?:form|field|custom field|property setter|customi[sz]|mandatory|required|read[ -]?only|hidden|workflow|client script|layout)\b",
    re.IGNORECASE,
)
_ASK_OUTCOMES = {
    "answer", "live_read", "artifact", "governed_change",
    "durable_workflow", "attended_browser", "development_workflow",
}
_HANDOFF_LABELS = {
    "governed_change": _("Prepare this governed change"),
    "durable_workflow": _("Create a reusable workflow proposal"),
    "attended_browser": _("Prepare an attended browser workflow"),
    "development_workflow": _("Prepare a reviewed development workflow"),
}


def _require_user() -> str:
    user = (frappe.session.user or "").strip().lower()
    if not user or user == "guest":
        frappe.throw(_("Sign in to ask Muster"), frappe.PermissionError)
    if not cint(frappe.db.get_value("User", user, "enabled")):
        frappe.throw(_("This user is not active"), frappe.PermissionError)
    return user


def _require_post() -> None:
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(_("This endpoint only accepts POST requests"), frappe.PermissionError)


def _scope(value: str | dict | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) and value else (value or {})
    except (TypeError, ValueError) as error:
        frappe.throw(_("Ask context must be valid JSON"), frappe.ValidationError)
        raise error
    if not isinstance(parsed, dict):
        frappe.throw(_("Ask context must be a JSON object"), frappe.ValidationError)
    return parsed


def _run_id(value: str) -> str:
    run_id = (value or "").strip()
    if not RUN_ID.fullmatch(run_id):
        frappe.throw(_("Invalid Muster answer identifier"), frappe.ValidationError)
    return run_id


def _conversation(value: str | None) -> str:
    conversation = (value or "").strip()
    if not CONVERSATION_ID.fullmatch(conversation):
        frappe.throw(_("Invalid Muster conversation identifier"), frappe.ValidationError)
    return conversation


def _idempotency_key(value: str | None) -> str:
    request = getattr(frappe.local, "request", None)
    header = frappe.get_request_header("Idempotency-Key") if request is not None else None
    key = (header or value or "").strip()
    if not key or len(key) > 140:
        frappe.throw(_("A valid Idempotency-Key is required"), frappe.ValidationError)
    return key


def _client_for_user(user: str) -> tuple[GatewayClient, dict[str, str], Any]:
    binding = trusted_binding()
    headers, _csrf = run_authority_headers(binding, user)
    return GatewayClient(binding), headers, binding


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_text(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _validated_intent(response: dict[str, Any], request_id: str) -> dict[str, Any]:
    if set(response) != {"schemaVersion", "requestId", "status", "intent"}:
        frappe.throw(_("The gateway returned an invalid Ask routing result"), frappe.ValidationError)
    intent = response.get("intent")
    if response.get("schemaVersion") != 1 or response.get("requestId") != request_id or response.get("status") != "classified" or not isinstance(intent, dict):
        frappe.throw(_("The gateway returned an invalid Ask routing result"), frappe.ValidationError)
    allowed = {"schemaVersion", "requestId", "requestedOutcomes", "requiresClarification", "clarification"}
    if set(intent) - allowed or intent.get("schemaVersion") != 1 or intent.get("requestId") != request_id:
        frappe.throw(_("The gateway returned an invalid Ask routing result"), frappe.ValidationError)
    outcomes = intent.get("requestedOutcomes")
    if not isinstance(outcomes, list) or not outcomes or len(outcomes) > len(_ASK_OUTCOMES) or any(item not in _ASK_OUTCOMES for item in outcomes) or len(set(outcomes)) != len(outcomes):
        frappe.throw(_("The gateway returned an invalid Ask routing result"), frappe.ValidationError)
    requires = intent.get("requiresClarification")
    clarification = intent.get("clarification")
    if not isinstance(requires, bool) or requires != bool(isinstance(clarification, str) and clarification.strip()):
        frappe.throw(_("The gateway returned an invalid Ask routing result"), frappe.ValidationError)
    return {"outcomes": outcomes, "clarification": clarification.strip() if requires else None}


def _handoffs(outcomes: list[str], request_id: str) -> list[dict[str, str]]:
    handoffs = []
    for outcome in outcomes:
        label = _HANDOFF_LABELS.get(outcome)
        if not label:
            continue
        kind = "workflow_proposal" if outcome in {"artifact", "durable_workflow"} else outcome
        handoffs.append({
            "id": f"handoff-{sha256(f'{request_id}:{kind}'.encode()).hexdigest()[:20]}",
            "kind": kind,
            "label": label,
            "state": "offered",
            "requires": "explicit_confirmation",
        })
    return handoffs


def _ask_turn(user: str, conversation: str, key: str, prompt: str, scope: dict[str, Any], outcomes: list[str], handoffs: list[dict[str, str]], clarification: str | None = None):
    prompt_hash = _hash_text(prompt)
    scope_json = _canonical(scope)
    scope_hash = _hash_text(scope_json)
    existing_name = frappe.db.get_value("Muster Ask Turn", {"request_id": key}, "name")
    if existing_name:
        existing = frappe.get_doc("Muster Ask Turn", existing_name)
        if (
            existing.requested_by != user or existing.conversation_id != conversation
            or existing.prompt_hash != prompt_hash or existing.scope_hash != scope_hash
            or json.loads(existing.outcomes_json or "[]") != outcomes
            or json.loads(existing.handoffs_json or "[]") != handoffs
            or (existing.clarification or None) != clarification
        ):
            frappe.throw(_("This Ask idempotency key was already used for another request"), frappe.ValidationError)
        return existing
    doc = frappe.get_doc({
        "doctype": "Muster Ask Turn",
        "requested_by": user,
        "conversation_id": conversation,
        "request_id": key,
        "status": "Offered",
        "expires_at": add_to_date(now_datetime(), hours=24),
        "prompt_secret": prompt,
        "prompt_hash": prompt_hash,
        "scope_json": scope_json,
        "scope_hash": scope_hash,
        "outcomes_json": _canonical(outcomes),
        "handoffs_json": _canonical(handoffs),
        "clarification": clarification,
    })
    doc.insert()
    return doc


def _merge_form_evidence(context: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    customized = []
    for field in snapshot.get("fields") or []:
        provenance = field.get("provenance") if isinstance(field, dict) else None
        if not isinstance(provenance, dict) or (provenance.get("source") != "custom_field" and not provenance.get("property_setters")):
            continue
        customized.append({key: field.get(key) for key in (
            "fieldname", "label", "fieldtype", "required", "read_only", "hidden", "writable", "provenance"
        )})
    evidence = {
        "kind": "fresh_permission_filtered_effective_form_schema",
        "doctype": snapshot.get("doctype"),
        "authority": snapshot.get("authority"),
        "customized_fields": customized[:100],
        "doctype_property_setters": (snapshot.get("doctype_property_setters") or [])[:100],
        "workflow": snapshot.get("workflow"),
        "client_scripts": (snapshot.get("client_scripts") or [])[:100],
        "schema_hash": snapshot.get("schema_hash"),
        "revision": snapshot.get("revision"),
        "permissionFiltered": True,
    }
    existing = {}
    if context.get("summary"):
        try:
            parsed = json.loads(context["summary"])
            if isinstance(parsed, dict):
                existing = parsed
        except (TypeError, ValueError):
            pass
    summary = _canonical({**existing, "effectiveFormSchema": evidence})
    if len(summary.encode()) > 32_000:
        frappe.throw(_("The permission-filtered form evidence exceeded its safe size limit"), frappe.ValidationError)
    return {**context, "summary": summary}


@frappe.whitelist()
def submit(
    prompt: str,
    conversation_id: str,
    scope: str | dict | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue a universal, read-safe Muster turn under the live Frappe user."""
    _require_post()
    user = _require_user()
    text = (prompt or "").strip()
    if not text or len(text) > MAX_PROMPT_CHARS:
        frappe.throw(_("Ask Muster requires a prompt of at most {0} characters").format(MAX_PROMPT_CHARS), frappe.ValidationError)
    conversation = _conversation(conversation_id)
    key = _idempotency_key(idempotency_key)
    client, headers, binding = _client_for_user(user)
    requested_scope = _scope(scope)
    context = permission_filtered_context(requested_scope, user)
    intent_request_id = f"intent-{sha256(f'{user}:{conversation}:{key}'.encode()).hexdigest()[:32]}"
    prior_name = frappe.db.get_value("Muster Ask Turn", {"request_id": key}, "name")
    if prior_name:
        turn = frappe.get_doc("Muster Ask Turn", prior_name)
        prompt_hash = _hash_text(text)
        scope_hash = _hash_text(_canonical(requested_scope))
        if turn.requested_by != user or turn.conversation_id != conversation or turn.prompt_hash != prompt_hash or turn.scope_hash != scope_hash:
            frappe.throw(_("This Ask idempotency key was already used for another request"), frappe.ValidationError)
        outcomes = json.loads(turn.outcomes_json or "[]")
        handoffs = json.loads(turn.handoffs_json or "[]")
        if (
            not isinstance(outcomes, list) or not outcomes or len(outcomes) > len(_ASK_OUTCOMES)
            or any(item not in _ASK_OUTCOMES for item in outcomes) or len(set(outcomes)) != len(outcomes)
            or handoffs != _handoffs(outcomes, intent_request_id)
            or (turn.clarification and len(turn.clarification) > 500)
        ):
            frappe.throw(_("Stored Ask routing evidence is invalid"), frappe.ValidationError)
        intent = {"outcomes": outcomes, "clarification": turn.clarification or None}
    else:
        classified = client.request(
            "POST",
            ASK_INTENTS_PATH,
            payload={
                "schemaVersion": 1,
                "requestId": intent_request_id,
                "prompt": text,
                "context": {key: value for key, value in context.items() if key != "summary"},
            },
            idempotency_key=f"{intent_request_id}-route",
            headers=headers,
        )
        intent = _validated_intent(classified, intent_request_id)
        handoffs = _handoffs(intent["outcomes"], intent_request_id)
        turn = _ask_turn(user, conversation, key, text, requested_scope, intent["outcomes"], handoffs, intent["clarification"])
    if intent["clarification"]:
        return {
            "status": "clarification",
            "reason": intent["clarification"],
            "turn_id": turn.name,
            "handoffs": [],
        }
    form_evidence = False
    selected_doctype = str(requested_scope.get("doctype") or "").strip()
    if "live_read" in intent["outcomes"] and selected_doctype and _FORM_SCHEMA_REQUEST.search(text):
        context = _merge_form_evidence(context, effective_form_schema(selected_doctype, user=user))
        form_evidence = True
    # Broad live-data questions use two separate trust stages: the gateway may
    # propose only a bounded data IR, then Frappe independently revalidates and
    # executes it as this session user. The provider never receives credentials
    # and never gets a SQL/method/URL escape hatch.
    if not form_evidence and ("live_read" in intent["outcomes"] or _LIVE_READ_REQUEST.search(text)):
        read_request_id = f"read-{sha256(f'{user}:{conversation}:{key}'.encode()).hexdigest()[:32]}"
        catalog = build_read_catalog(text, requested_scope, user)
        if not catalog:
            return {
                "status": "needs_read_plan",
                "reason": _("No permission-filtered business records are available to answer this question. No live value was guessed."),
            }
        planned = client.request(
            "POST",
            READ_PLANS_PATH,
            payload={
                "schemaVersion": 1,
                "requestId": read_request_id,
                "question": text,
                "catalog": catalog,
                "context": {key: value for key, value in context.items() if key != "summary"},
            },
            idempotency_key=f"{read_request_id}-plan",
            headers=headers,
        )
        if (
            planned.get("schemaVersion") != 1
            or planned.get("requestId") != read_request_id
            or planned.get("status") != "planned"
        ):
            frappe.throw(_("The gateway returned an invalid permission-filtered read plan"), frappe.ValidationError)
        read_plan = planned.get("plan")
        if not isinstance(read_plan, dict):
            frappe.throw(_("The gateway returned an invalid permission-filtered read plan"), frappe.ValidationError)
        disposition = read_plan.get("disposition")
        if disposition == "query":
            evidence = execute_read_plan(read_plan, read_request_id, user)
            context = merge_read_evidence(context, evidence)
        elif disposition not in {"unsupported", "action_needed"} or read_plan.get("queries") != []:
            frappe.throw(_("The gateway returned an invalid permission-filtered read disposition"), frappe.ValidationError)
    identity = frappe_identity(user)
    # The public HTTPS origin is the gateway's reciprocal identity. Local bench
    # site names are deliberately never sent as an authority claim.
    identity["site"] = binding.site_origin
    context = {**context, "ask": {
        "schemaVersion": 1,
        "requestId": intent_request_id,
        "requestedOutcomes": intent["outcomes"],
    }}
    response = client.request(
        "POST",
        ASYNC_PATH,
        payload={
            "message": {
                "surfaceId": f"frappe:{binding.site_id}",
                "conversationId": conversation,
                "senderId": user,
                "text": text,
            },
            "identity": identity,
            "context": context,
        },
        idempotency_key=key,
        headers=headers,
    )
    run_id = _run_id(str(response.get("runId") or ""))
    expected_poll = f"{ASYNC_RUNS_PATH}/{run_id}"
    if response.get("pollUrl") != expected_poll or response.get("status") not in {"queued", "running", "completed"}:
        frappe.throw(_("The gateway returned an invalid Ask Muster acknowledgement"), frappe.ValidationError)
    return {
        "run_id": run_id,
        "status": response["status"],
        "replayed": bool(response.get("replayed")),
        "turn_id": turn.name,
        "handoffs": handoffs,
    }


@frappe.whitelist()
def accept_handoff(
    turn_id: str,
    handoff_id: str,
    confirmed: int | str = 0,
    idempotency_key: str | None = None,
    development_app: str | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    """Accept one inert next step; acceptance never publishes, starts, or executes it."""
    _require_post()
    user = _require_user()
    if not cint(confirmed):
        frappe.throw(_("Confirm this reviewed next step before creating it"), frappe.ValidationError)
    key = _idempotency_key(idempotency_key)
    if not turn_id or len(turn_id) > 140 or not handoff_id or len(handoff_id) > 140:
        frappe.throw(_("Invalid Ask handoff"), frappe.ValidationError)
    if frappe.db.db_type == "sqlite":
        frappe.db.sql("select name from `tabMuster Ask Turn` where name=%s", turn_id)
    else:
        frappe.db.sql("select name from `tabMuster Ask Turn` where name=%s for update", turn_id)
    turn = frappe.get_doc("Muster Ask Turn", turn_id)
    if turn.requested_by != user or not turn.has_permission("read"):
        frappe.throw(_("This Ask handoff is unavailable"), frappe.PermissionError)
    handoffs = json.loads(turn.handoffs_json or "[]")
    selected = next((item for item in handoffs if isinstance(item, dict) and item.get("id") == handoff_id), None)
    if not selected or selected.get("state") != "offered" or selected.get("requires") != "explicit_confirmation":
        frappe.throw(_("This Ask handoff is unavailable"), frappe.PermissionError)
    if turn.status == "Accepted":
        linked = turn.development_proposal if selected.get("kind") == "development_workflow" else turn.workflow_proposal
        if turn.accepted_handoff_id != handoff_id or not linked:
            frappe.throw(_("Another handoff from this Ask turn was already accepted"), frappe.ValidationError)
        return {
            "turn_id": turn.name, "handoff_id": handoff_id, "proposal": linked,
            "proposal_doctype": "Muster Development Proposal" if selected.get("kind") == "development_workflow" else "Muster Workflow Proposal",
            "status": "Proposed", "replayed": True, "executed": False,
        }
    if turn.status != "Offered" or now_datetime() >= get_datetime(turn.expires_at):
        if turn.status == "Offered":
            turn.db_set("status", "Expired", update_modified=False)
        frappe.throw(_("This Ask handoff has expired"), frappe.ValidationError)
    prompt = turn.get_password("prompt_secret")
    if _hash_text(prompt) != turn.prompt_hash:
        frappe.throw(_("The stored Ask request no longer matches its evidence"), frappe.ValidationError)
    requested_scope = json.loads(turn.scope_json or "{}")
    if _hash_text(_canonical(requested_scope)) != turn.scope_hash:
        frappe.throw(_("The stored Ask context no longer matches its evidence"), frappe.ValidationError)
    proposal_key = f"ask-{sha256(f'{turn.name}:{handoff_id}:{key}'.encode()).hexdigest()[:56]}"
    if selected.get("kind") == "development_workflow":
        if not development_app or not policy:
            frappe.throw(_("Select a registered app and enabled policy for this development proposal"), frappe.ValidationError)
        from muster.api.development import create_from_ask_turn
        proposal = create_from_ask_turn(turn, development_app, policy, proposal_key)
        link_field = "development_proposal"
        proposal_doctype = "Muster Development Proposal"
    else:
        from muster.orchestration.workflow_proposal import request_workflow_proposal
        proposal = request_workflow_proposal(
            prompt, requested_scope, proposal_key,
            preferred_handoff_kind=selected.get("kind"),
        )
        link_field = "workflow_proposal"
        proposal_doctype = "Muster Workflow Proposal"
    turn.db_set({
        "status": "Accepted", "accepted_handoff_id": handoff_id,
        "accepted_at": now_datetime(), link_field: proposal["proposal"],
    }, update_modified=False)
    return {
        "turn_id": turn.name, "handoff_id": handoff_id, "proposal": proposal["proposal"],
        "proposal_doctype": proposal_doctype, "status": proposal["status"],
        "replayed": False, "executed": False,
    }


@frappe.whitelist()
def poll(run_id: str, wait_ms: int | str = 0) -> dict[str, Any]:
    """Poll only a run owned by this site's current authenticated user."""
    user = _require_user()
    run_id = _run_id(run_id)
    wait = min(max(cint(wait_ms), 0), 20_000)
    client, headers, _binding = _client_for_user(user)
    response = client.request(
        "GET",
        f"{ASYNC_RUNS_PATH}/{run_id}",
        params={"waitMs": wait},
        headers=headers,
    )
    status = response.get("status")
    if status not in {"queued", "running", "completed", "failed"} or response.get("runId") != run_id:
        frappe.throw(_("The gateway returned an invalid Ask Muster status"), frappe.ValidationError)
    result: dict[str, Any] = {"run_id": run_id, "status": status}
    partial = response.get("partialText")
    if isinstance(partial, str):
        result["partial_text"] = partial[:64_000]
    if status == "completed":
        reply = response.get("reply")
        if not isinstance(reply, dict) or not isinstance(reply.get("text"), str):
            frappe.throw(_("The gateway returned an invalid Ask Muster answer"), frappe.ValidationError)
        result["answer"] = reply["text"][:64_000]
        artifacts = []
        for index, artifact in enumerate(reply.get("artifacts") or []):
            if not isinstance(artifact, dict):
                continue
            expected = f"{ASYNC_RUNS_PATH}/{run_id}/artifacts/{index}"
            if artifact.get("path") != expected:
                continue
            artifacts.append({
                "name": str(artifact.get("name") or _("Artifact"))[:255],
                "mime": str(artifact.get("mime") or "application/octet-stream")[:255],
                "download_url": (
                    "/api/method/muster.api.ask.artifact"
                    f"?run_id={quote(run_id)}&index={index}"
                ),
            })
        if artifacts:
            result["artifacts"] = artifacts
    elif status == "failed":
        # Provider/runtime diagnostics stay server-side; the deployment bearer,
        # paths, and provider topology must never reach a Desk user.
        result["error"] = _("Muster could not complete this answer. You can retry safely.")
    return result


@frappe.whitelist()
def artifact(run_id: str, index: int | str) -> None:
    """Proxy a bounded run artifact through Frappe's authenticated session."""
    user = _require_user()
    run_id = _run_id(run_id)
    artifact_index = cint(index)
    if artifact_index < 0 or artifact_index > 100:
        frappe.throw(_("Invalid Muster artifact"), frappe.ValidationError)
    client, headers, _binding = _client_for_user(user)
    value = client.request_bytes(
        f"{ASYNC_RUNS_PATH}/{run_id}/artifacts/{artifact_index}",
        headers=headers,
    )
    frappe.response["type"] = "binary"
    frappe.response["filecontent"] = value.content
    frappe.response["filename"] = f"muster-artifact-{artifact_index}"
    frappe.response["content_type"] = value.content_type
    frappe.response["display_content_as"] = "attachment"
