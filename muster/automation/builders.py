from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from muster.automation.models import ArtifactManifest, AutomationValidationError


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FIELDNAME = re.compile(r"^[a-z][a-z0-9_]{0,139}$")
_DOCTYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,139}$")
_SCRIPTISH = re.compile(
    r"(?is)<\s*script\b|javascript\s*:|\bon[a-z]+\s*=|<\s*(?:iframe|object|embed|link|meta)\b"
)
_JINJA = re.compile(r"{{\s*doc\.([a-z][a-z0-9_]*)\s*}}")
_ANY_TEMPLATE = re.compile(r"({[{%#].*?[}%#]})", re.S)


@dataclass(frozen=True)
class ArtifactDefinition:
    doctype: str
    name: str
    values: dict[str, Any]
    capability: str
    approval_class: str
    governed_permissions: tuple[tuple[str, str], ...] = ()
    verify_only: bool = False


class TrustedResolver(Protocol):
    def resolve_trusted_artifact(self, kind: str, key: str) -> Mapping[str, Any]: ...


def _only(values: Mapping[str, Any], allowed: set[str], kind: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise AutomationValidationError(
            f"{kind} contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    if any(str(key).startswith("_") for key in values):
        raise AutomationValidationError(f"{kind} contains a reserved field")


def _required(values: Mapping[str, Any], *names: str) -> None:
    missing = [name for name in names if values.get(name) in (None, "", [])]
    if missing:
        raise AutomationValidationError(f"required values are missing: {', '.join(missing)}")


def _flag(value: Any, label: str) -> int:
    if value in (0, 1, False, True):
        return int(bool(value))
    raise AutomationValidationError(f"{label} must be a boolean")


def _safe_static_html(value: str, *, allow_doc_fields: bool = False) -> str:
    if len(value.encode("utf-8")) > 128_000:
        raise AutomationValidationError("HTML exceeds 128 KB")
    if _SCRIPTISH.search(value):
        raise AutomationValidationError("active HTML content is not permitted")
    if allow_doc_fields:
        without_safe_fields = _JINJA.sub("", value)
        if _ANY_TEMPLATE.search(without_safe_fields):
            raise AutomationValidationError("only simple {{ doc.fieldname }} placeholders are permitted")
    elif _ANY_TEMPLATE.search(value):
        raise AutomationValidationError("template expressions are not permitted on this surface")
    return value


def _route(value: Any) -> str:
    route = str(value)
    if (len(route) > 140 or not re.fullmatch(r"[a-z0-9][a-z0-9_/-]*", route)
            or "//" in route or ".." in route):
        raise AutomationValidationError("route is invalid")
    return route


def _roles(value: Any) -> list[dict[str, str]]:
    roles = value or []
    if (not isinstance(roles, list) or len(roles) > 50 or
            any(not isinstance(row, dict) or set(row) != {"role"} or
                not isinstance(row["role"], str) or not row["role"] for row in roles)):
        raise AutomationValidationError("roles must be [{\"role\": \"Role Name\"}]")
    return [dict(row) for row in roles]


def _custom_field(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    if not m.target_doctype:
        raise AutomationValidationError("custom_field requires target_doctype")
    values = dict(m.values)
    _only(values, {
        "label", "fieldtype", "options", "insert_after", "description", "default",
        "reqd", "unique", "read_only", "hidden", "in_list_view", "in_standard_filter",
        "allow_on_submit", "depends_on", "mandatory_depends_on", "read_only_depends_on",
        "collapsible", "collapsible_depends_on", "width", "precision", "length",
        "fetch_from", "fetch_if_empty", "translatable", "no_copy",
    }, m.kind)
    if not _FIELDNAME.fullmatch(m.target_name):
        raise AutomationValidationError("custom field target_name must be a snake_case fieldname")
    _required(values, "label", "fieldtype")
    fieldtype = str(values["fieldtype"])
    if fieldtype in {"Password", "Code"}:
        raise AutomationValidationError("Password and executable Code custom fields require app code review")
    for name in {"reqd", "unique", "read_only", "hidden", "in_list_view", "in_standard_filter",
                 "allow_on_submit", "fetch_if_empty", "translatable", "no_copy"} & set(values):
        values[name] = _flag(values[name], name)
    for dependency_name in {"depends_on", "mandatory_depends_on", "read_only_depends_on"} & set(values):
        dependency = str(values[dependency_name])
        if dependency.startswith("eval:") or not _FIELDNAME.fullmatch(dependency):
            raise AutomationValidationError("executable custom field dependencies are not permitted")
    values.update({"dt": m.target_doctype, "fieldname": m.target_name})
    return ArtifactDefinition("Custom Field", f"{m.target_doctype}-{m.target_name}", values,
                              "artifact.custom_field.write", "Standard", ((m.target_doctype, "write"),))


def _property_setter(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    if not m.target_doctype:
        raise AutomationValidationError("property_setter requires target_doctype")
    values = dict(m.values)
    _only(values, {"field_name", "property", "value", "property_type", "doctype_or_field"}, m.kind)
    _required(values, "property", "value", "property_type")
    if isinstance(values["value"], (dict, list)):
        raise AutomationValidationError("Property Setter value must be a scalar")
    if values["property_type"] not in {"Data", "Check", "Int", "Float", "Text", "Select"}:
        raise AutomationValidationError("Property Setter property_type is not allowed")
    field_name = str(values.get("field_name") or "")
    if field_name and not _FIELDNAME.fullmatch(field_name):
        raise AutomationValidationError("field_name is invalid")
    property_name = str(values["property"])
    if not _FIELDNAME.fullmatch(property_name):
        raise AutomationValidationError("property is invalid")
    if property_name in {"depends_on", "mandatory_depends_on", "read_only_depends_on"}:
        dependency = str(values["value"])
        if dependency.startswith("eval:") or not re.fullmatch(r"[a-z][a-z0-9_]*", dependency):
            raise AutomationValidationError("executable property dependencies are not permitted")
    values.update({
        "doc_type": m.target_doctype, "field_name": field_name,
        "doctype_or_field": values.get("doctype_or_field") or ("DocField" if field_name else "DocType"),
    })
    name = "-".join(part for part in (m.target_doctype, field_name, property_name) if part)
    return ArtifactDefinition("Property Setter", name, values, "artifact.property_setter.write",
                              "Sensitive", ((m.target_doctype, "write"),))


_DOCFIELD_KEYS = {
    "fieldname", "label", "fieldtype", "options", "reqd", "unique", "read_only", "hidden",
    "in_list_view", "in_standard_filter", "description", "default", "insert_after", "depends_on",
    "mandatory_depends_on", "read_only_depends_on", "allow_on_submit", "no_copy", "precision",
    "length", "width", "collapsible", "collapsible_depends_on", "fetch_from", "fetch_if_empty",
}


def _doctype(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"fields", "permissions", "istable", "issingle", "is_submittable", "track_changes",
                   "autoname", "title_field", "search_fields", "sort_field", "sort_order", "description"}, m.kind)
    if not m.module:
        raise AutomationValidationError("doctype requires module")
    fields = values.get("fields") or []
    if not isinstance(fields, list) or not fields or len(fields) > 200:
        raise AutomationValidationError("doctype fields must contain 1-200 field definitions")
    seen: set[str] = set()
    clean_fields = []
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            raise AutomationValidationError(f"fields[{index}] must be an object")
        _only(field, _DOCFIELD_KEYS, f"fields[{index}]")
        _required(field, "fieldname", "label", "fieldtype")
        fieldname = str(field["fieldname"])
        if not _FIELDNAME.fullmatch(fieldname) or fieldname in seen:
            raise AutomationValidationError(f"fields[{index}].fieldname is invalid or duplicated")
        if field["fieldtype"] in {"Code", "Password"}:
            raise AutomationValidationError("Code and Password fields require app-level review")
        seen.add(fieldname)
        for dependency_name in {"depends_on", "mandatory_depends_on", "read_only_depends_on"}:
            if dependency_name in field:
                dependency = str(field[dependency_name])
                if dependency.startswith("eval:") or not re.fullmatch(r"[a-z][a-z0-9_]*", dependency):
                    raise AutomationValidationError("executable field dependencies are not permitted")
        clean_fields.append(dict(field))
    permissions = values.get("permissions") or []
    if not isinstance(permissions, list) or len(permissions) > 50:
        raise AutomationValidationError("permissions must be a list with at most 50 rows")
    allowed_perm = {"role", "read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "print", "email", "share", "if_owner", "permlevel"}
    clean_permissions = []
    for row in permissions:
        if not isinstance(row, dict):
            raise AutomationValidationError("permission rows must be objects")
        _only(row, allowed_perm, "doctype permission")
        _required(row, "role")
        clean_permissions.append(dict(row))
    for flag in {"istable", "issingle", "is_submittable", "track_changes"} & set(values):
        values[flag] = _flag(values[flag], flag)
    values.update({"name": m.target_name, "module": m.module, "custom": 1,
                   "fields": clean_fields, "permissions": clean_permissions})
    return ArtifactDefinition("DocType", m.target_name, values, "artifact.doctype.write", "Sensitive")


def _page(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"title", "icon", "restrict_to_domain", "roles"}, m.kind)
    if not m.module:
        raise AutomationValidationError("page requires module")
    roles = _roles(values.get("roles"))
    values.update({"page_name": m.target_name, "name": m.target_name, "module": m.module,
                   "title": values.get("title") or m.target_name, "standard": "No", "roles": roles})
    return ArtifactDefinition("Page", m.target_name, values, "artifact.page.write", "Sensitive")


def _query_report(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"source_doctype", "fields", "filters", "order_by", "limit", "roles", "disabled"}, m.kind)
    source = str(values.get("source_doctype") or m.target_doctype or "")
    if not source or not _DOCTYPE_NAME.fullmatch(source):
        raise AutomationValidationError("query_report requires a safe source_doctype")
    if not m.module:
        raise AutomationValidationError("query_report requires module")
    fields = values.get("fields") or []
    if not isinstance(fields, list) or not fields or len(fields) > 50:
        raise AutomationValidationError("query_report fields must contain 1-50 fieldnames")
    if any(not isinstance(item, str) or not _FIELDNAME.fullmatch(item) for item in fields):
        raise AutomationValidationError("query_report fields must be safe fieldnames")
    filters = values.get("filters") or []
    if not isinstance(filters, list) or len(filters) > 20:
        raise AutomationValidationError("query_report filters must be a list with at most 20 entries")
    where = []
    for row in filters:
        if not isinstance(row, dict) or set(row) - {"fieldname", "operator", "parameter"}:
            raise AutomationValidationError("query_report filter rows are invalid")
        _required(row, "fieldname", "operator", "parameter")
        if not _FIELDNAME.fullmatch(str(row["fieldname"])) or not _IDENTIFIER.fullmatch(str(row["parameter"])):
            raise AutomationValidationError("query_report filter identifier is invalid")
        operator = str(row["operator"]).upper()
        if operator not in {"=", "!=", ">", ">=", "<", "<=", "LIKE"}:
            raise AutomationValidationError("query_report filter operator is not allowed")
        where.append(f"`{row['fieldname']}` {operator} %({row['parameter']})s")
    order_by = values.get("order_by") or []
    if not isinstance(order_by, list) or len(order_by) > 5:
        raise AutomationValidationError("order_by must be a list with at most 5 rows")
    order = []
    for row in order_by:
        if not isinstance(row, dict) or set(row) != {"fieldname", "direction"}:
            raise AutomationValidationError("order_by rows require fieldname and direction")
        if not _FIELDNAME.fullmatch(str(row["fieldname"])) or str(row["direction"]).upper() not in {"ASC", "DESC"}:
            raise AutomationValidationError("order_by row is invalid")
        order.append(f"`{row['fieldname']}` {str(row['direction']).upper()}")
    limit = int(values.get("limit") or 500)
    if not 1 <= limit <= 5000:
        raise AutomationValidationError("query_report limit must be between 1 and 5000")
    query = f"SELECT {', '.join(f'`{item}`' for item in fields)} FROM `tab{source}`"
    if where:
        query += " WHERE " + " AND ".join(where)
    if order:
        query += " ORDER BY " + ", ".join(order)
    query += f" LIMIT {limit}"
    payload = {"report_name": m.target_name, "name": m.target_name, "ref_doctype": source,
               "report_type": "Query Report", "is_standard": "No", "query": query,
               "module": m.module, "roles": _roles(values.get("roles")),
               "disabled": _flag(values.get("disabled", 0), "disabled")}
    return ArtifactDefinition("Report", m.target_name, payload, "artifact.report.write",
                              "Sensitive", ((source, "report"),))


def _script_report(m: ArtifactManifest, resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"implementation_key", "ref_doctype", "roles", "disabled"}, m.kind)
    _required(values, "implementation_key", "ref_doctype")
    trusted = dict(resolver.resolve_trusted_artifact("script_report", str(values["implementation_key"])))
    if set(trusted) - {"report_script", "module"} or not trusted.get("report_script"):
        raise AutomationValidationError("trusted Script Report resolver returned an invalid definition")
    payload = {"report_name": m.target_name, "name": m.target_name,
               "ref_doctype": values["ref_doctype"], "report_type": "Script Report",
               "is_standard": "No", "report_script": trusted["report_script"],
               "module": trusted.get("module") or m.module, "roles": _roles(values.get("roles")),
               "disabled": _flag(values.get("disabled", 0), "disabled")}
    return ArtifactDefinition("Report", m.target_name, payload, "artifact.report.script.write",
                              "Privileged Code", ((str(values["ref_doctype"]), "report"),))


def _print_format(m: ArtifactManifest, resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"doc_type", "html", "trusted_template_key", "disabled", "print_format_type", "raw_printing"}, m.kind)
    _required(values, "doc_type")
    if not m.module:
        raise AutomationValidationError("print_format requires module")
    if bool(values.get("html")) == bool(values.get("trusted_template_key")):
        raise AutomationValidationError("provide exactly one of html or trusted_template_key")
    approval = "Sensitive"
    if values.get("trusted_template_key"):
        trusted = dict(resolver.resolve_trusted_artifact("print_format", str(values["trusted_template_key"])))
        content = str(trusted.get("html") or "")
        if not content:
            raise AutomationValidationError("trusted Print Format resolver returned no HTML")
        approval = "Privileged Code"
    else:
        content = _safe_static_html(str(values["html"]), allow_doc_fields=True)
    payload = {"name": m.target_name, "doc_type": values["doc_type"], "module": m.module,
               "standard": "No", "custom_format": 1, "html": content,
               "disabled": _flag(values.get("disabled", 0), "disabled"),
               "print_format_type": values.get("print_format_type") or "Jinja",
               "raw_printing": _flag(values.get("raw_printing", 0), "raw_printing")}
    return ArtifactDefinition("Print Format", m.target_name, payload, "artifact.print_format.write",
                              approval, ((str(values["doc_type"]), "print"),))


def _web_page(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"title", "route", "main_section", "published", "dynamic_route", "insert_style"}, m.kind)
    _required(values, "title", "route", "main_section")
    payload = {"name": m.target_name, "title": values["title"], "route": _route(values["route"]),
               "main_section": _safe_static_html(str(values["main_section"])),
               "published": _flag(values.get("published", 0), "published"),
               "dynamic_route": _flag(values.get("dynamic_route", 0), "dynamic_route")}
    if values.get("insert_style"):
        style = str(values["insert_style"])
        if any(token in style.lower() for token in ("@import", "url(", "expression(", "behavior:")):
            raise AutomationValidationError("external CSS imports and URLs are not permitted")
        payload["insert_style"] = style
    return ArtifactDefinition("Web Page", m.target_name, payload, "artifact.web_page.write", "Sensitive")


def _web_form(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"doc_type", "title", "route", "web_form_fields", "published", "login_required",
                   "allow_edit", "allow_multiple", "show_attachments", "success_message"}, m.kind)
    _required(values, "doc_type", "title", "route", "web_form_fields")
    rows = values["web_form_fields"]
    allowed = {"fieldname", "fieldtype", "label", "reqd", "options", "read_only", "show_in_filter",
               "max_length", "default"}
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise AutomationValidationError("web_form_fields must contain 1-100 rows")
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            raise AutomationValidationError("web form field rows must be objects")
        _only(row, allowed, "web form field")
        _required(row, "fieldname")
        if not _FIELDNAME.fullmatch(str(row["fieldname"])):
            raise AutomationValidationError("web form fieldname is invalid")
        cleaned.append(dict(row))
    payload = {"name": m.target_name, "doc_type": values["doc_type"], "title": values["title"],
               "route": _route(values["route"]), "web_form_fields": cleaned,
               "published": _flag(values.get("published", 0), "published"),
               "login_required": _flag(values.get("login_required", 1), "login_required"),
               "allow_edit": _flag(values.get("allow_edit", 0), "allow_edit"),
               "allow_multiple": _flag(values.get("allow_multiple", 0), "allow_multiple"),
               "show_attachments": _flag(values.get("show_attachments", 0), "show_attachments"),
               "success_message": html.escape(str(values.get("success_message") or "Submitted"))}
    return ArtifactDefinition("Web Form", m.target_name, payload, "artifact.web_form.write",
                              "Sensitive", ((str(values["doc_type"]), "create"),))


_OFFICE_MIMES = {
    "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
}


def _office_artifact(m: ArtifactManifest, _resolver: TrustedResolver) -> ArtifactDefinition:
    values = dict(m.values)
    _only(values, {"mission", "work_unit", "file_url", "mime_type", "size_bytes", "checksum",
                   "title", "visibility"}, m.kind)
    _required(values, "mission", "file_url", "mime_type", "size_bytes", "checksum")
    if not str(values["file_url"]).startswith("/private/files/"):
        raise AutomationValidationError("office artifacts must reference an existing private Frappe File")
    if values["mime_type"] not in _OFFICE_MIMES:
        raise AutomationValidationError("office artifact MIME type is not allowed")
    size = int(values["size_bytes"])
    if not 0 < size <= 100 * 1024 * 1024:
        raise AutomationValidationError("office artifact size must be between 1 byte and 100 MB")
    checksum = str(values["checksum"])
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise AutomationValidationError("office artifact checksum must be lowercase SHA-256")
    if values.get("visibility") not in {None, "Private", "Participants", "Auditors"}:
        raise AutomationValidationError("office artifact visibility is invalid")
    kind = {
        "application/pdf": "PDF",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Spreadsheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "Presentation",
    }.get(str(values["mime_type"]), "Other")
    payload = {"name": m.target_name, "title": values.get("title") or m.target_name,
               "mission": values["mission"], "work_unit": values.get("work_unit"), "kind": kind,
               "visibility": values.get("visibility") or "Private", "is_public": 0,
               "file": values["file_url"], "mime_type": values["mime_type"], "size_bytes": size,
               "checksum": checksum, "verification_status": "Pending"}
    return ArtifactDefinition("Muster Artifact", m.target_name, payload,
                              "artifact.office.write", "Standard",
                              (("Muster Mission", "read"), ("File", "read")))


BUILDERS: dict[str, Callable[[ArtifactManifest, TrustedResolver], ArtifactDefinition]] = {
    "custom_field": _custom_field, "property_setter": _property_setter, "doctype": _doctype,
    "page": _page, "query_report": _query_report, "script_report": _script_report,
    "print_format": _print_format, "web_page": _web_page, "web_form": _web_form,
    "office_artifact": _office_artifact,
}


def build(manifest: ArtifactManifest, resolver: TrustedResolver) -> ArtifactDefinition:
    return BUILDERS[manifest.kind](manifest, resolver)
