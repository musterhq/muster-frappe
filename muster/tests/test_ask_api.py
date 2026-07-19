import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

try:
    import frappe
    from frappe.tests.utils import FrappeTestCase

    from muster.api.ask import _prompt_form_doctype, _require_user, accept_handoff, poll, submit
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("Frappe integration tests require an installed test site") from exc


class TestAskAPI(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.original_user = frappe.session.user
        self.user = frappe.get_doc({
            "doctype": "User",
            "email": f"ask-{uuid4().hex[:10]}@example.test",
            "first_name": "Ask Test",
            "enabled": 1,
            "send_welcome_email": 0,
            "roles": [{"role": "Muster Viewer"}],
        }).insert(ignore_permissions=True).name
        self.binding = SimpleNamespace(site_origin="https://erp.example.test", site_id="site-a")

    def tearDown(self):
        frappe.set_user(self.original_user)
        super().tearDown()

    def test_submit_uses_current_user_and_permission_filtered_context(self):
        gateway = Mock()
        acknowledgement = {
            "runId": "msg_11111111-1111-4111-8111-111111111111",
            "pollUrl": "/v1/integrations/frappe/messages/runs/msg_11111111-1111-4111-8111-111111111111",
            "status": "queued",
            "replayed": False,
        }
        frappe.set_user(self.user)
        with (
            patch("muster.api.ask._require_post"),
            patch("muster.api.ask._client_for_user", return_value=(gateway, {"X-Signed": "yes"}, self.binding)) as client_for_user,
            patch("muster.api.ask.build_read_catalog", return_value=[{"doctype": "User", "fields": ["name"]}]),
            patch("muster.api.ask.frappe_identity", return_value={
                "site": "local.invalid", "user": self.user, "roles": ["Muster Viewer"], "authMode": "frappe_session",
            }),
        ):
            def request(method, path, **kwargs):
                if path.endswith("/ask-intents"):
                    request_id = kwargs["payload"]["requestId"]
                    return {
                        "schemaVersion": 1, "requestId": request_id, "status": "classified",
                        "intent": {"schemaVersion": 1, "requestId": request_id, "requestedOutcomes": ["answer"], "requiresClarification": False},
                    }
                if path.endswith("/read-plans"):
                    request_id = kwargs["payload"]["requestId"]
                    return {
                        "schemaVersion": 1, "requestId": request_id, "status": "planned",
                        "plan": {"schemaVersion": 1, "requestId": request_id, "disposition": "unsupported", "reason": "No live records required.", "queries": []},
                    }
                return acknowledgement
            gateway.request.side_effect = request
            result = submit(
                "Explain what I can do on this site.",
                "desk-session-1",
                '{"route":"/app/home","page_type":"Workspace","page_name":"Home"}',
                "ask-request-1",
            )
        self.assertEqual(result["status"], "queued")
        client_for_user.assert_called_once_with(self.user)
        _, kwargs = gateway.request.call_args_list[-1]
        self.assertEqual(kwargs["payload"]["message"]["senderId"], self.user)
        self.assertEqual(kwargs["payload"]["identity"]["site"], "https://erp.example.test")
        self.assertEqual(kwargs["payload"]["context"]["route"], "/app/home")
        self.assertIn("frappe", kwargs["payload"]["context"]["installedApps"])
        self.assertNotIn("bearer", result)
        self.assertNotIn("pollUrl", result)
        self.assertEqual(result["handoffs"], [])
        self.assertTrue(result["turn_id"].startswith("MST-ASK-"))

    def test_builtin_administrator_keeps_exact_local_identity(self):
        frappe.set_user("Administrator")
        self.assertEqual(_require_user(), "Administrator")

    def test_poll_is_bound_to_current_user_and_filters_reasoning_and_untrusted_artifacts(self):
        gateway = Mock()
        run_id = "msg_22222222-2222-4222-8222-222222222222"
        gateway.request.return_value = {
            "runId": run_id,
            "status": "completed",
            "reasoningText": "private provider reasoning",
            "reply": {
                "text": "Here is the permitted answer.",
                "artifacts": [
                    {"name": "safe.pdf", "mime": "application/pdf", "path": f"/v1/integrations/frappe/messages/runs/{run_id}/artifacts/0"},
                    {"name": "remote.txt", "mime": "text/plain", "path": "https://evil.example/steal"},
                ],
            },
        }
        frappe.set_user(self.user)
        with patch("muster.api.ask._client_for_user", return_value=(gateway, {"X-Signed": "yes"}, self.binding)) as client_for_user:
            result = poll(run_id)
        client_for_user.assert_called_once_with(self.user)
        self.assertEqual(result["answer"], "Here is the permitted answer.")
        self.assertNotIn("reasoningText", result)
        self.assertNotIn("reasoning_text", result)
        self.assertEqual([row["name"] for row in result["artifacts"]], ["safe.pdf"])
        self.assertNotIn("path", result["artifacts"][0])

    def test_live_aggregate_gets_fresh_filtered_evidence_before_answer_provider(self):
        gateway = Mock()
        acknowledgement = {
            "runId": "msg_44444444-4444-4444-8444-444444444444",
            "pollUrl": "/v1/integrations/frappe/messages/runs/msg_44444444-4444-4444-8444-444444444444",
            "status": "queued",
            "replayed": False,
        }
        frappe.set_user(self.user)
        with (
            patch("muster.api.ask._require_post"),
            patch("muster.api.ask._client_for_user", return_value=(gateway, {"X-Signed": "yes"}, self.binding)),
            patch("muster.api.ask.build_read_catalog", return_value=[{"doctype": "Sales Invoice", "fields": ["name"]}]),
            patch("muster.api.ask.execute_read_plan", return_value={"kind": "fresh_permission_filtered_frappe_evidence", "permissionFiltered": True}) as execute,
        ):
            # Let the dynamically generated request identity pass exact echo validation.
            def request(method, path, **kwargs):
                if path.endswith("/ask-intents"):
                    request_id = kwargs["payload"]["requestId"]
                    return {
                        "schemaVersion": 1, "requestId": request_id, "status": "classified",
                        "intent": {"schemaVersion": 1, "requestId": request_id, "requestedOutcomes": ["live_read"], "requiresClarification": False},
                    }
                if path.endswith("/read-plans"):
                    request_id = kwargs["payload"]["requestId"]
                    return {
                        "schemaVersion": 1, "requestId": request_id, "status": "planned",
                        "plan": {"schemaVersion": 1, "requestId": request_id, "disposition": "query", "reason": "Fresh evidence", "queries": []},
                    }
                return acknowledgement
            gateway.request.side_effect = request
            result = submit(
                "How many overdue invoices are outstanding?",
                "desk-session-2",
                '{"route":"/app/home"}',
                "ask-request-live-read",
            )
        self.assertEqual(result["status"], "queued")
        execute.assert_called_once()
        self.assertEqual(gateway.request.call_count, 3)
        answer_payload = gateway.request.call_args_list[2].kwargs["payload"]
        self.assertIn("fresh_permission_filtered_frappe_evidence", answer_payload["context"]["summary"])

    def test_effectful_ask_returns_only_an_explicit_inert_handoff(self):
        gateway = Mock()
        acknowledgement = {
            "runId": "msg_55555555-5555-4555-8555-555555555555",
            "pollUrl": "/v1/integrations/frappe/messages/runs/msg_55555555-5555-4555-8555-555555555555",
            "status": "queued", "replayed": False,
        }
        frappe.set_user(self.user)
        with (
            patch("muster.api.ask._require_post"),
            patch("muster.api.ask._client_for_user", return_value=(gateway, {"X-Signed": "yes"}, self.binding)),
        ):
            def request(method, path, **kwargs):
                if path.endswith("/ask-intents"):
                    request_id = kwargs["payload"]["requestId"]
                    return {
                        "schemaVersion": 1, "requestId": request_id, "status": "classified",
                        "intent": {"schemaVersion": 1, "requestId": request_id, "requestedOutcomes": ["governed_change", "attended_browser"], "requiresClarification": False},
                    }
                return acknowledgement
            gateway.request.side_effect = request
            result = submit("Create a customer and show every Desk step.", "desk-session-3", {"route": "/desk"}, "ask-effect-offer")
        self.assertEqual(result["status"], "queued")
        self.assertEqual({row["kind"] for row in result["handoffs"]}, {"governed_change", "attended_browser"})
        self.assertTrue(all(row["requires"] == "explicit_confirmation" for row in result["handoffs"]))
        turn = frappe.get_doc("Muster Ask Turn", result["turn_id"])
        self.assertEqual(turn.status, "Offered")
        self.assertFalse(turn.workflow_proposal)
        self.assertFalse(frappe.db.exists("Muster Mission", {"objective": "Create a customer and show every Desk step."}))

    def test_form_question_identifies_custom_fields_and_property_setters_as_data(self):
        gateway = Mock()
        acknowledgement = {
            "runId": "msg_66666666-6666-4666-8666-666666666666",
            "pollUrl": "/v1/integrations/frappe/messages/runs/msg_66666666-6666-4666-8666-666666666666",
            "status": "queued", "replayed": False,
        }
        frappe.set_user(self.user)
        snapshot = {
            "doctype": "Customer", "authority": {"read": True, "create": False, "write": False},
            "fields": [{
                "fieldname": "custom_tier", "label": "Tier", "fieldtype": "Select", "required": True,
                "read_only": False, "hidden": False, "writable": False,
                "provenance": {"source": "custom_field", "custom_field": "Customer-custom_tier", "property_setters": [{"name": "PS-1", "property": "reqd", "value": "1"}]},
            }],
            "doctype_property_setters": [], "workflow": None,
            "client_scripts": [{"name": "Customer Form", "view": "Form", "modified": "2026-07-19"}],
            "schema_hash": "a" * 64, "revision": "b" * 64,
        }
        with (
            patch("muster.api.ask._require_post"),
            patch("muster.api.ask._client_for_user", return_value=(gateway, {"X-Signed": "yes"}, self.binding)),
            patch("muster.api.ask.effective_form_schema", return_value=snapshot),
        ):
            def request(method, path, **kwargs):
                if path.endswith("/ask-intents"):
                    request_id = kwargs["payload"]["requestId"]
                    return {"schemaVersion": 1, "requestId": request_id, "status": "classified", "intent": {
                        "schemaVersion": 1, "requestId": request_id, "requestedOutcomes": ["live_read"], "requiresClarification": False,
                    }}
                return acknowledgement
            gateway.request.side_effect = request
            result = submit("Which custom fields and property setters changed this form?", "desk-form", {"doctype": "Customer", "scope_mode": "context"}, "ask-form-evidence")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(gateway.request.call_count, 2)
        context = gateway.request.call_args_list[-1].kwargs["payload"]["context"]["summary"]
        self.assertIn("custom_tier", context)
        self.assertIn("property_setters", context)
        self.assertNotIn("script source", context.lower())

    def test_home_prompt_resolves_one_explicit_form_target_and_includes_writable_fields(self):
        readable = SimpleNamespace(get_can_read=lambda: ["Customer", "Custom Field", "Property Setter"])
        with (
            patch.object(frappe, "get_user", return_value=readable),
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe, "has_permission", return_value=True),
        ):
            target = _prompt_form_doctype(
                "What custom fields and property setters currently affect Customer, and which fields can I write?",
                "", self.user,
            )
        self.assertEqual(target, "Customer")

        context = __import__("muster.api.ask", fromlist=["_merge_form_evidence"])._merge_form_evidence({}, {
            "doctype": "Customer", "authority": {"read": True, "write": True},
            "fields": [
                {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "permlevel": 0, "required": True, "writable": True, "provenance": {"source": "doctype_field", "property_setters": []}},
                {"fieldname": "tax_id", "label": "Tax ID", "fieldtype": "Data", "permlevel": 0, "required": False, "writable": False, "provenance": {"source": "doctype_field", "property_setters": []}},
            ],
            "doctype_property_setters": [], "workflow": None, "client_scripts": [], "schema_hash": "a" * 64, "revision": "b" * 64,
        })["summary"]
        self.assertIn('"writable_fields":[{"fieldname":"customer_name"', context)
        self.assertNotIn('"fieldname":"tax_id","fieldtype"', context)

    def test_handoff_requires_confirmation_and_creates_only_a_proposal(self):
        frappe.set_user(self.user)
        turn = frappe.get_doc({
            "doctype": "Muster Ask Turn", "requested_by": self.user,
            "conversation_id": "desk-handoff", "request_id": f"handoff-{uuid4().hex}", "status": "Offered",
            "expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=1),
            "prompt_secret": "Create a customer visibly", "prompt_hash": __import__("hashlib").sha256(b"Create a customer visibly").hexdigest(),
            "scope_json": "{}", "scope_hash": __import__("hashlib").sha256(b"{}").hexdigest(),
            "outcomes_json": '["governed_change"]',
            "handoffs_json": '[{"id":"handoff-test","kind":"governed_change","label":"Prepare","state":"offered","requires":"explicit_confirmation"}]',
        }).insert()
        with patch("muster.api.ask._require_post"):
            with self.assertRaises(frappe.ValidationError):
                accept_handoff(turn.name, "handoff-test", 0, "accept-no")
            with patch("muster.orchestration.workflow_proposal.request_workflow_proposal", return_value={"proposal": "MST-WFP-TEST", "status": "Proposed"}) as request:
                result = accept_handoff(turn.name, "handoff-test", 1, "accept-yes")
        self.assertFalse(result["executed"])
        self.assertEqual(result["proposal"], "MST-WFP-TEST")
        request.assert_called_once()

    def test_development_handoff_creates_only_source_bound_development_proposal(self):
        frappe.set_user(self.user)
        prompt = "Implement a tested custom app report"
        digest = __import__("hashlib").sha256
        turn = frappe.get_doc({
            "doctype": "Muster Ask Turn", "requested_by": self.user,
            "conversation_id": "desk-development", "request_id": f"development-{uuid4().hex}", "status": "Offered",
            "expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=1),
            "prompt_secret": prompt, "prompt_hash": digest(prompt.encode()).hexdigest(),
            "scope_json": "{}", "scope_hash": digest(b"{}").hexdigest(),
            "outcomes_json": '["development_workflow"]',
            "handoffs_json": '[{"id":"handoff-development","kind":"development_workflow","label":"Prepare development","state":"offered","requires":"explicit_confirmation"}]',
        }).insert()
        with (
            patch("muster.api.ask._require_post"),
            patch("muster.api.development.create_from_ask_turn", return_value={
                "proposal": "MST-DEV-TEST", "status": "Proposed", "replayed": False, "executed": False,
            }) as create,
            patch("muster.orchestration.workflow_proposal.request_workflow_proposal") as workflow,
        ):
            result = accept_handoff(
                turn.name, "handoff-development", 1, "accept-development",
                development_app="custom_app", policy="Development Policy",
            )
        self.assertEqual(result["proposal"], "MST-DEV-TEST")
        self.assertEqual(result["proposal_doctype"], "Muster Development Proposal")
        self.assertFalse(result["executed"])
        create.assert_called_once()
        workflow.assert_not_called()

    def test_guest_cannot_submit_or_poll(self):
        frappe.set_user("Guest")
        with patch("muster.api.ask._require_post"):
            with self.assertRaises(frappe.PermissionError):
                submit("Tell me about the site", "guest-session", {}, "guest-key")
        with self.assertRaises(frappe.PermissionError):
            poll("msg_33333333-3333-4333-8333-333333333333")


if __name__ == "__main__":
    unittest.main()
