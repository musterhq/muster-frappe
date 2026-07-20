from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from muster.api.surface import bootstrap


class TestSurfaceAPI(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.installed_apps = patch(
            "muster.api.surface._installed_apps",
            return_value={
                "frappe", "muster", "erpnext", "hrms", "telephony",
                "crm", "helpdesk", "field_ops", "field_ops_demo",
            },
        )
        self.installed_apps.start()
        self.original_method = getattr(frappe.request, "method", None) if frappe.request else None
        if frappe.request:
            frappe.request.method = "GET"

    def tearDown(self):
        self.installed_apps.stop()
        if frappe.request:
            frappe.request.method = self.original_method
        super().tearDown()

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    @patch("muster.api.surface.get_versions")
    def test_returns_only_bounded_supported_surface(self, versions, _csrf):
        versions.return_value = {
            "crm": {"version": "1.78.2"},
            "helpdesk": {"version": "2.0.0"},
            "secret_customer_app": {"version": "9.9.9"},
        }
        crm = bootstrap("crm")
        self.assertEqual(crm, {
            "schema_version": 1, "adapter_contract": 1, "surface": "crm",
            "supported": True, "installed_version": "1.78.2", "csrf_token": "csrf-test",
        })
        self.assertEqual(bootstrap("helpdesk")["supported"], False)
        unknown = bootstrap("secret_customer_app")
        self.assertIsNone(unknown["surface"])
        self.assertNotIn("installed_version", unknown)

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    @patch("muster.api.surface.get_versions", return_value={"crm": {"version": "1.79.0"}})
    def test_unaudited_minor_version_fails_closed(self, _versions, _csrf):
        result = bootstrap("crm")
        self.assertFalse(result["supported"])
        self.assertNotIn("installed_version", result)

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    @patch("muster.api.surface.get_versions", return_value={"helpdesk": {"version": "1.27.0"}})
    def test_bench_source_without_site_install_fails_closed(self, _versions, _csrf):
        with patch("muster.api.surface._installed_apps", return_value={"frappe", "muster"}):
            result = bootstrap("helpdesk")
        self.assertFalse(result["supported"])
        self.assertNotIn("installed_version", result)

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    @patch("muster.api.surface.get_versions", return_value={"field_ops": {"version": "1.4.0"}})
    def test_custom_site_manifest_is_version_bound_and_non_executable(self, _versions, _csrf):
        configured = [{
            "id": "field-ops", "label": "Field Operations", "app": "field_ops",
            "supported_major": 1, "base": "/operations", "path_prefixes": ["/operations/"],
            "root_markers": ["[data-reactroot]"], "doctypes": ["Service Visit"],
            "operations": ["create", "update"],
            "routes": {"Service Visit": {
                "create": "/visits/new", "record": "/visits/{name}",
                "create_buttons": [], "commit_buttons": {"create": ["Create"], "update": ["Save"]},
                "field_hints": {"customer": ["Customer", "Choose customer"]},
            }},
        }]
        with patch.object(frappe, "conf", {"muster_spa_surfaces": configured}):
            result = bootstrap(route="/operations/visits")
        self.assertTrue(result["supported"])
        self.assertEqual(result["surface"], "custom")
        self.assertEqual(result["installed_version"], "1.4.0")
        self.assertEqual(result["descriptor"]["id"], "muster-config-field-ops")
        self.assertNotIn("app", result["descriptor"])
        self.assertNotIn("javascript", str(result["descriptor"]).lower())

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    @patch("muster.api.surface.get_versions", return_value={"field_ops": {"version": "2.0.0"}})
    def test_custom_manifest_unknown_version_and_ambiguous_route_fail_closed(self, _versions, _csrf):
        row = {
            "id": "field-ops", "app": "field_ops", "supported_major": 1,
            "base": "/operations", "path_prefixes": ["/operations/"],
            "root_markers": ["#app"], "doctypes": ["Service Visit"],
            "operations": ["create"],
            "routes": {"Service Visit": {"create": "/visits/new", "commit_buttons": {"create": ["Create"]}}},
        }
        with patch.object(frappe, "conf", {"muster_spa_surfaces": [row]}):
            self.assertFalse(bootstrap(route="/operations/visits")["supported"])
        duplicate = {**row, "id": "field-ops-copy"}
        with patch("muster.api.surface.get_versions", return_value={"field_ops": {"version": "1.4.0"}}), patch.object(
            frappe, "conf", {"muster_spa_surfaces": [row, duplicate]},
        ):
            self.assertFalse(bootstrap(route="/operations/visits")["supported"])

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    @patch("muster.api.surface.get_versions", return_value={"field_ops_demo": {"version": "1.0.0"}})
    def test_reference_vue_manifest_accepts_only_its_exact_route_family(self, _versions, _csrf):
        row = {
            "id": "field-ops-demo", "app": "field_ops_demo", "supported_major": 1,
            "base": "/operations", "path_prefixes": ["/operations/"],
            "root_markers": ["[data-v-app]"], "doctypes": ["Service Visit"],
            "operations": ["create", "update"],
            "routes": {"Service Visit": {
                "create": "/visits/new", "record": "/visits/{name}",
                "create_buttons": [],
                "commit_buttons": {"create": ["Create"], "update": ["Save"]},
                "field_hints": {"customer": ["Customer", "Choose customer"]},
            }},
        }
        with patch.object(frappe, "conf", {"muster_spa_surfaces": [row]}):
            for route in ("/operations", "/operations/visits", "/operations/visits/SV-2026-00001"):
                with self.subTest(route=route):
                    self.assertTrue(bootstrap(route=route)["supported"])
            for route in ("/operation", "/operations-evil/visits", "/other/operations/visits", "/operations/../desk"):
                with self.subTest(route=route):
                    result = bootstrap(route=route)
                    self.assertFalse(result["supported"])
                    self.assertNotIn("installed_version", result)

    @patch("muster.api.surface.frappe.sessions.get_csrf_token", return_value="csrf-test")
    def test_reference_vue_manifest_rejects_wrong_major_and_bench_only_source(self, _csrf):
        row = {
            "id": "field-ops-demo", "app": "field_ops_demo", "supported_major": 1,
            "base": "/operations", "path_prefixes": ["/operations/"],
            "root_markers": ["[data-v-app]"], "doctypes": ["Service Visit"],
            "operations": ["create"],
            "routes": {"Service Visit": {
                "create": "/visits/new", "create_buttons": [],
                "commit_buttons": {"create": ["Create"]},
            }},
        }
        with patch.object(frappe, "conf", {"muster_spa_surfaces": [row]}), patch(
            "muster.api.surface.get_versions", return_value={"field_ops_demo": {"version": "2.0.0"}},
        ):
            self.assertFalse(bootstrap(route="/operations/visits")["supported"])
        with patch.object(frappe, "conf", {"muster_spa_surfaces": [row]}), patch(
            "muster.api.surface.get_versions", return_value={"field_ops_demo": {"version": "1.0.0"}},
        ), patch("muster.api.surface._installed_apps", return_value={"frappe", "muster"}):
            self.assertFalse(bootstrap(route="/operations/visits")["supported"])
