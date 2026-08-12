#!/usr/bin/env python3
"""
Build the TFGBV database schema in Directus.

Idempotent: re-running skips anything that already exists, so this is the source of
truth for the schema rather than the admin UI.

    python3 scripts/seed_schema.py

Reads DIRECTUS_URL / ADMIN_EMAIL / ADMIN_PASSWORD from .env next to this repo root.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    """Read .env if present. Absent in-container, where real env vars are used."""
    env = {}
    try:
        with open(os.path.join(ROOT, ".env")) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


ENV = load_env()
# DIRECTUS_URL wins so the sidecar can reach the container directly, where the
# host's PUBLIC_URL (localhost) would not resolve.
URL = (os.environ.get("DIRECTUS_URL")
       or ENV.get("PUBLIC_URL", "http://localhost:8055")).rstrip("/")
TOKEN = None


def api(method, path, payload=None, quiet404=False):
    req = urllib.request.Request(
        f"{URL}{path}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            return json.loads(body)["data"] if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        if quiet404 and e.code in (403, 404):
            return None
        raise SystemExit(f"\n{method} {path} -> {e.code}\n{detail}\n")


def login():
    global TOKEN
    email = os.environ.get("ADMIN_EMAIL") or ENV["ADMIN_EMAIL"]
    password = os.environ.get("ADMIN_PASSWORD") or ENV["ADMIN_PASSWORD"]
    # Cleared before the call: /auth/login rejects the request outright when the
    # Authorization header carries an expired token, so a stale one here makes
    # re-authentication impossible for any long-running caller.
    TOKEN = None
    data = api("POST", "/auth/login", {"email": email, "password": password})
    TOKEN = data["access_token"]


# --------------------------------------------------------------------------- helpers

def existing_collections():
    return {c["collection"] for c in api("GET", "/collections")}


def existing_fields(collection):
    data = api("GET", f"/fields/{collection}", quiet404=True) or []
    return {f["field"] for f in data}


def pk_string(length=64):
    return {"field": "id", "type": "string",
            "schema": {"is_primary_key": True, "length": length, "is_nullable": False},
            "meta": {"interface": "input", "required": True,
                     "note": "Stable machine key — never change once data exists."}}


def pk_uuid():
    return {"field": "id", "type": "uuid",
            "schema": {"is_primary_key": True, "is_nullable": False},
            "meta": {"special": ["uuid"], "interface": "input", "readonly": True,
                     "hidden": True}}


def pk_auto():
    return {"field": "id", "type": "integer",
            "schema": {"is_primary_key": True, "has_auto_increment": True},
            "meta": {"interface": "input", "readonly": True, "hidden": True}}


def make_collection(name, fields, icon="folder", note="", group=None,
                    display_template=None, sort_field=None, singleton=False):
    if name in COLLECTIONS:
        print(f"  = collection {name}")
        return
    meta = {"icon": icon, "note": note, "singleton": singleton,
            "collection": name, "hidden": False}
    if group:
        meta["group"] = group
    if display_template:
        meta["display_template"] = display_template
    if sort_field:
        meta["sort_field"] = sort_field
    api("POST", "/collections",
        {"collection": name, "schema": {"name": name}, "meta": meta, "fields": fields})
    COLLECTIONS.add(name)
    print(f"  + collection {name}")


def add_field(collection, field, type_, schema=None, meta=None):
    if field in existing_fields(collection):
        return
    api("POST", f"/fields/{collection}",
        {"field": field, "type": type_, "schema": schema if schema is not None else {},
         "meta": meta or {}})
    print(f"  + {collection}.{field}")


def relation_exists(collection, field):
    for r in api("GET", "/relations"):
        if r["collection"] == collection and r["field"] == field:
            return True
    return False


def add_m2o(collection, field, related, required=False, on_delete="SET NULL",
            note=None, width="half"):
    """Many-to-one. Field type must match the related collection's PK type."""
    related_pk = PK_TYPES[related]
    add_field(collection, field, related_pk,
              schema={"is_nullable": not required},
              meta={"interface": "select-dropdown-m2o", "required": required,
                    "note": note, "width": width,
                    "options": {"template": DISPLAY[related]},
                    "display": "related-values",
                    "display_options": {"template": DISPLAY[related]}})
    if not relation_exists(collection, field):
        api("POST", "/relations", {
            "collection": collection, "field": field, "related_collection": related,
            "meta": {"sort_field": None}, "schema": {"on_delete": on_delete}})
        print(f"  + relation {collection}.{field} -> {related}")


def add_m2m(collection, alias, related, junction=None, note=None):
    """Many-to-many via an explicit junction collection."""
    junction = junction or f"{collection}_{related}"
    left, right = f"{collection}_id", f"{related}_id"

    make_collection(junction, [pk_auto()], icon="link",
                    note=f"Junction: {collection} <-> {related}", group="junctions")
    api("PATCH", f"/collections/{junction}", {"meta": {"hidden": True}})
    add_field(junction, left, PK_TYPES[collection], schema={}, meta={"hidden": True})
    add_field(junction, right, PK_TYPES[related], schema={}, meta={"hidden": True})

    # An m2m template is resolved against the JUNCTION row, so it must hop through the
    # junction's foreign key: "{{label}}" alone makes Directus look for `label` on
    # cases_interventions and throw FORBIDDEN when the form loads.
    template = DISPLAY[related].replace("{{", "{{" + right + ".")
    add_field(collection, alias, "alias",
              schema=None,
              meta={"interface": "list-m2m", "special": ["m2m"], "note": note,
                    "options": {"layout": "list", "template": template},
                    "display": "related-values",
                    "display_options": {"template": template}})

    if not relation_exists(junction, left):
        api("POST", "/relations", {
            "collection": junction, "field": left, "related_collection": collection,
            "meta": {"one_field": alias, "junction_field": right, "sort_field": None,
                     "one_deselect_action": "delete"},
            "schema": {"on_delete": "CASCADE"}})
    if not relation_exists(junction, right):
        api("POST", "/relations", {
            "collection": junction, "field": right, "related_collection": related,
            "meta": {"one_field": None, "junction_field": left},
            "schema": {"on_delete": "CASCADE"}})
    print(f"  + m2m {collection}.{alias} <-> {related}")


def add_audit_fields(collection):
    add_field(collection, "date_created", "timestamp",
              meta={"special": ["date-created"], "interface": "datetime",
                    "readonly": True, "hidden": True, "width": "half"})
    add_field(collection, "user_created", "uuid",
              meta={"special": ["user-created"], "interface": "select-dropdown-m2o",
                    "readonly": True, "hidden": True, "width": "half",
                    "display": "user"})
    add_field(collection, "date_updated", "timestamp",
              meta={"special": ["date-updated"], "interface": "datetime",
                    "readonly": True, "hidden": True, "width": "half"})
    add_field(collection, "user_updated", "uuid",
              meta={"special": ["user-updated"], "interface": "select-dropdown-m2o",
                    "readonly": True, "hidden": True, "width": "half",
                    "display": "user"})
    for f, rel in (("user_created", "directus_users"), ("user_updated", "directus_users")):
        if not relation_exists(collection, f):
            api("POST", "/relations", {
                "collection": collection, "field": f,
                "related_collection": rel, "meta": {}, "schema": {}})


def dropdown(choices):
    # display_options needs its own copy of the choices; without it the `labels` display
    # title-cases the raw value ("tfgbv" -> "Tfgbv") instead of showing the label.
    opts = [{"text": t, "value": v} for v, t in choices]
    return {"interface": "select-dropdown",
            "options": {"choices": opts},
            "display": "labels",
            "display_options": {"choices": opts}}


def seed(collection, rows):
    have = {str(i["id"]) for i in
            (api("GET", f"/items/{collection}?limit=-1&fields=id") or [])}
    new = [r for r in rows if str(r["id"]) not in have]
    if new:
        api("POST", f"/items/{collection}", new)
    print(f"  seeded {collection}: +{len(new)} (had {len(have)})")


# --------------------------------------------------------------- controlled vocabulary
# Sourced from Data Modelling.xlsx / Template 1.xlsm, with typos corrected and the
# duplicate survivor/perpetrator identity lists merged into one shared vocabulary.

LOOKUPS = {
    "case_categories": ("How the case entered the system", "category", [
        ("victim_reported", "Victim Reported"),
        ("operator_collected", "Operator Collected")]),
    "genders": ("Gender identity of survivor or perpetrator", "wc", [
        ("female", "Female"), ("male", "Male"),
        ("gender_diverse_group", "Gender Diverse Group"),
        ("lgbtqia_community", "LGBTQIA+ Community"),
        ("lesbian", "Lesbian"), ("gay", "Gay"), ("bisexual", "Bisexual"),
        ("transgender", "Transgender"), ("queer_questioning", "Queer/Questioning"),
        ("intersex", "Intersex"), ("asexual", "Asexual"),
        ("prefer_not_to_say", "Prefer not to say")]),
    "identities": ("Shared identity vocabulary for survivors and perpetrators", "badge", [
        ("gender_advocate", "Gender Advocate"), ("cso_member", "CSO Member"),
        ("human_rights_defender", "Human Rights Defender"),
        ("media_worker", "Media Worker"),
        ("political_activist", "Political Activist"),
        ("indigenous_peoples", "Indigenous Peoples"),
        ("people_with_disabilities", "People with disabilities"),
        ("minority", "Minority"), ("journalist", "Journalist"),
        ("public_figure", "Artist/Celebrity/Public Figure"),
        ("critic", "Civilian criticizing the development project"),
        ("public_user", "Public/Social Media User"),
        ("educator", "Educator"), ("unknown", "Unknown")]),
    "platforms": ("Platform where the incident occurred", "public", [
        ("facebook", "Facebook"), ("messenger", "Messenger"),
        ("instagram", "Instagram"), ("telegram", "Telegram"),
        ("youtube", "YouTube"), ("tiktok", "Tik Tok"), ("x", "X"),
        ("whatsapp", "WhatsApp"), ("signal", "Signal"), ("email", "Email"),
        ("cool_app", "Cool App"), ("website", "Website"), ("other", "Other")]),
    "harassment_types": ("Form of harassment (multi-select on a case)", "report", [
        ("misogynistic_hate_speech", "Misogynistic Hate-Speech"),
        ("cyber_bullying", "Cyber-bullying"),
        ("image_video_based_abuse", "Image/Video based abuse"),
        ("discrimination", "Discrimination"),
        ("sexual_harassment_exploitation", "Sexual Harassment/Exploitation"),
        ("cyber_stalking", "Cyber-Stalking"), ("doxxing", "Doxxing"),
        ("impersonation", "Impersonation"),
        ("mass_reporting_to_silence_victims", "Mass-reporting to silence victims"),
        ("controlling_social_media_accounts", "Controlling one's social media accounts"),
        ("sextortion_blackmail", "Sextortion (blackmail, ...)"),
        ("ai_generated_sexual_content", "AI-generated sexual content"),
        ("physical_safety_threats", "Physical Safety Threats")]),
    "case_statuses": ("Where the case stands", "flag", [
        ("investigating", "Under Investigation"), ("ongoing", "On-going"),
        ("resolved", "Resolved"), ("cannot_resolve", "Cannot be resolved"),
        ("referred", "Referred"), ("rejected", "Rejected")]),
    "interventions": ("Action taken or recommended (multi-select on a case)", "healing", [
        ("continue_monitoring_the_case", "Continue monitoring the case"),
        ("technical_support", "Technical Support"),
        ("documented", "Documented"), ("password_reset", "Password reset"),
        ("2fa_setup", "2FA Setup"),
        ("page_account_recovery", "Page/Account Recovery"),
        ("secured_digital_device", "Secured Digital Device"),
        ("submitted_report_to_platforms", "Submitted Report to Platforms"),
        ("provided_digital_security_training", "Provided Digital Security Training"),
        ("provided_safety_planning_to_victim", "Provided Safety Planning to Victim"),
        ("provided_safety_guideline", "Provided Safety Guideline"),
        ("mental_health_referrals", "Mental Health Referrals"),
        ("legal_referrals", "Legal Referrals"),
        ("not_applicable", "Not Applicable")]),
    "perpetrator_relations": ("Perpetrator's relationship to the survivor", "group", [
        ("stranger", "Stranger"), ("current_partner", "Current Partner"),
        ("ex_partner", "Ex-Partner"), ("colleague", "Colleague"),
        ("manager_supervisor", "Manager/Supervisor"),
        ("political_actor", "Political Actor"),
        ("authorities_government_officials", "Authorities/Government Officials"),
        ("not_applicable", "Not Applicable")]),
    "severity_levels": ("Impact severity, 1 (minimal) to 5 (severe)", "priority_high", [
        ("minimal", "1. Minimal Impact"), ("low", "2. Low Impact"),
        ("moderate", "3. Moderate Impact"), ("high", "4. High Impact"),
        ("severe", "5. Severe Impact")]),
    # PROVISIONAL — the source files define `reporter` as a lookup but never populate it.
    "reporters": ("Who reported the case (PROVISIONAL — confirm with project owner)",
                  "record_voice_over", [
        ("survivor_self", "Survivor (self-reported)"),
        ("family_or_friend", "Family member or friend"),
        ("partner_organization", "Partner organization"),
        ("operator_outreach", "Operator outreach/monitoring"),
        ("hotline", "Hotline"), ("anonymous", "Anonymous"),
        ("unknown", "Unknown")]),
}

PK_TYPES = {"directus_users": "uuid"}
DISPLAY = {"directus_users": "{{first_name}} {{last_name}}"}
for _n in LOOKUPS:
    PK_TYPES[_n], DISPLAY[_n] = "string", "{{label}}"
for _n in ("organizations", "survivors", "perpetrators", "cases",
           "survivor_pii", "perpetrator_pii", "pii_access_requests"):
    PK_TYPES[_n] = "uuid"
DISPLAY.update({
    "organizations": "{{name}}",
    "survivors": "{{survivor_code}}",
    "perpetrators": "{{perpetrator_code}}",
    "cases": "{{case_code}}",
})

COLLECTIONS = set()


# ------------------------------------------------------------------------------ build

def make_folder(name, icon="folder", note="", hidden=False):
    """A collection folder — a directus_collections row with no table behind it."""
    if name in COLLECTIONS:
        return
    api("POST", "/collections", {"collection": name, "schema": None,
                                 "meta": {"icon": icon, "note": note,
                                          "hidden": hidden, "collection": name}})
    COLLECTIONS.add(name)
    print(f"  + folder {name}")


def build_lookups():
    print("\n[1/5] Lookup collections")
    make_folder("vocabulary", icon="list_alt", note="Controlled vocabularies")
    # hidden: junction tables are plumbing, not something an operator should browse
    make_folder("junctions", icon="link", note="Many-to-many junction tables",
                hidden=True)
    for name, (note, icon, rows) in LOOKUPS.items():
        make_collection(name, [
            pk_string(),
            {"field": "label", "type": "string",
             "schema": {"is_nullable": False},
             "meta": {"interface": "input", "required": True, "width": "half"}},
            {"field": "description", "type": "text",
             "meta": {"interface": "input-multiline", "width": "full"}},
            {"field": "sort", "type": "integer", "meta": {"interface": "input",
                                                          "hidden": True}},
            {"field": "active", "type": "boolean",
             "schema": {"default_value": True},
             "meta": {"interface": "boolean", "width": "half",
                      "note": "Uncheck to retire a value without deleting history."}},
        ], icon=icon, note=note, group="vocabulary",
            display_template="{{label}}", sort_field="sort")
        seed(name, [{"id": k, "label": v, "sort": i}
                    for i, (k, v) in enumerate(rows, 1)])


def build_organizations():
    print("\n[2/5] Organizations")
    make_collection("organizations", [
        pk_uuid(),
        {"field": "name", "type": "string", "schema": {"is_nullable": False},
         "meta": {"interface": "input", "required": True, "width": "half"}},
        {"field": "code", "type": "string",
         "schema": {"is_unique": True, "length": 32},
         "meta": {"interface": "input", "width": "half"}},
        {"field": "active", "type": "boolean", "schema": {"default_value": True},
         "meta": {"interface": "boolean", "width": "half"}},
    ], icon="corporate_fare", display_template="{{name}}",
        note="Owning organization — drives the Author role's row-level scope.")
    seed("organizations", [])


def build_people():
    print("\n[3/5] Survivors and perpetrators (non-identifying) + PII vault tables")

    make_collection("survivors", [
        pk_uuid(),
        {"field": "survivor_code", "type": "string",
         "schema": {"is_unique": True, "length": 32},
         "meta": {"interface": "input", "readonly": True, "width": "half",
                  "note": "Assigned automatically on save (SUR-0001). "
                          "Pseudonymous — never a real name."}},
    ], icon="person", display_template="{{survivor_code}}",
        note="Non-identifying survivor attributes. Names/contacts live in survivor_pii.")
    add_m2o("survivors", "gender", "genders")
    add_m2m("survivors", "identities", "identities",
            junction="survivors_identities",
            note="A survivor may hold several identities at once.")
    add_audit_fields("survivors")

    make_collection("perpetrators", [
        pk_uuid(),
        {"field": "perpetrator_code", "type": "string",
         "schema": {"is_unique": True, "length": 32},
         "meta": {"interface": "input", "readonly": True, "width": "half",
                  "note": "Assigned automatically on save (PER-0001)."}},
        {"field": "identification_status", "type": "string",
         "schema": {"default_value": "suspected"},
         "meta": dict(dropdown([("known", "Known"), ("suspected", "Suspected"),
                                ("unknown", "Unknown")]), width="half",
                      note="From the Perpetrator sheet's status column.")},
    ], icon="person_alert", display_template="{{perpetrator_code}}",
        note="Non-identifying perpetrator attributes. Details live in perpetrator_pii.")
    add_m2o("perpetrators", "gender", "genders")
    add_m2m("perpetrators", "identities", "identities",
            junction="perpetrators_identities")
    add_audit_fields("perpetrators")

    # ---- restricted PII tables: isolated so they can be swapped for a vault later
    make_collection("survivor_pii", [
        pk_uuid(),
        {"field": "full_name", "type": "string",
         "meta": {"interface": "input", "width": "half"}},
        {"field": "contact_phone", "type": "string",
         "meta": {"interface": "input", "width": "half"}},
        {"field": "contact_email", "type": "string",
         "meta": {"interface": "input", "width": "half"}},
        {"field": "address", "type": "text",
         "meta": {"interface": "input-multiline"}},
        {"field": "notes", "type": "text", "meta": {"interface": "input-multiline"}},
    ], icon="lock", note="RESTRICTED — Super Admin only. One row per survivor.")
    add_m2o("survivor_pii", "survivor", "survivors", required=True, on_delete="CASCADE")
    add_audit_fields("survivor_pii")

    make_collection("perpetrator_pii", [
        pk_uuid(),
        {"field": "full_name", "type": "string",
         "meta": {"interface": "input", "width": "half"}},
        {"field": "known_information", "type": "text",
         "meta": {"interface": "input-multiline",
                  "note": "Known Perpetrator Information (legacy column N)."}},
        {"field": "suspected_information", "type": "text",
         "meta": {"interface": "input-multiline",
                  "note": "Suspected Perpetrator Information (legacy column O)."}},
        {"field": "profile_urls", "type": "json",
         "meta": {"interface": "list", "options": {"template": "{{url}}", "fields": [
             {"field": "url", "type": "string", "name": "URL",
              "meta": {"interface": "input"}}]}}},
    ], icon="lock", note="RESTRICTED — Super Admin only. One row per perpetrator.")
    add_m2o("perpetrator_pii", "perpetrator", "perpetrators", required=True,
            on_delete="CASCADE")
    add_audit_fields("perpetrator_pii")


def build_cases():
    print("\n[4/5] Cases")
    make_collection("cases", [
        pk_uuid(),
        {"field": "case_code", "type": "string",
         "schema": {"is_unique": True, "length": 32},
         "meta": {"interface": "input", "readonly": True, "width": "half",
                  "note": "Assigned automatically on save (TFGBV0001 / TS0001) by a "
                          "database trigger — see scripts/migrations.sql."}},
        {"field": "case_type", "type": "string",
         "schema": {"default_value": "tfgbv", "is_nullable": False},
         "meta": dict(dropdown([("tfgbv", "TFGBV Case"),
                                ("technical_support", "Technical Support Case")]),
                      width="half", required=True,
                      note="Unifies both sheets of the legacy workbook.")},
        {"field": "record_status", "type": "string",
         "schema": {"default_value": "draft", "is_nullable": False},
         "meta": dict(dropdown([("draft", "Draft"), ("submitted", "Submitted"),
                                ("archived", "Archived")]),
                      width="half", required=True,
                      note="Workflow state — supports save-as-draft.")},
        {"field": "date_reported", "type": "date",
         "schema": {"is_nullable": False},
         "meta": {"interface": "datetime", "required": True, "width": "half"}},
        {"field": "caption_main_content", "type": "text",
         "meta": {"interface": "input-multiline",
                  "note": "Caption written on the main content."}},
        {"field": "incident_summary", "type": "text",
         "meta": {"interface": "input-rich-text-md"}},
        {"field": "impact_on_survivor", "type": "text",
         "meta": {"interface": "input-multiline",
                  "note": "Impact on the survivor (if victim-reported)."}},
        {"field": "evidence_links", "type": "json",
         "meta": {"interface": "list", "note": "Links to online content.",
                  "options": {"template": "{{url}}", "fields": [
                      {"field": "url", "type": "string", "name": "URL",
                       "meta": {"interface": "input"}},
                      {"field": "captured_at", "type": "string", "name": "Captured",
                       "meta": {"interface": "input"}}]}}},
    ], icon="gavel", display_template="{{case_code}} — {{date_reported}}",
        note="One row per reported case, TFGBV or technical support.")

    add_m2o("cases", "case_category", "case_categories", required=False)
    add_m2o("cases", "organization", "organizations",
            note="Row-level scope for the Author role.")
    add_m2o("cases", "reported_by", "reporters")
    add_m2o("cases", "survivor", "survivors")
    add_m2o("cases", "perpetrator", "perpetrators")
    add_m2o("cases", "perpetrator_relation", "perpetrator_relations")
    add_m2o("cases", "case_status", "case_statuses")
    add_m2o("cases", "severity", "severity_levels",
            note="Used by technical support cases; optional for TFGBV cases.")
    add_m2m("cases", "platforms", "platforms", junction="cases_platforms",
            note="An incident can span several platforms.")
    add_m2m("cases", "harassment_types", "harassment_types",
            junction="cases_harassment_types",
            note="An incident can involve several harassment types.")
    add_m2m("cases", "interventions", "interventions", junction="cases_interventions",
            note="Actions taken / recommended. Also covers technical support types.")
    add_audit_fields("cases")


def build_access_requests():
    print("\n[5/5] PII access request workflow")
    make_collection("pii_access_requests", [
        pk_uuid(),
        {"field": "reason", "type": "text", "schema": {"is_nullable": False},
         "meta": {"interface": "input-multiline", "required": True,
                  "note": "Why this record's PII needs to be seen."}},
        {"field": "status", "type": "string",
         "schema": {"default_value": "pending", "is_nullable": False},
         "meta": dict(dropdown([("pending", "Pending"), ("approved", "Approved"),
                                ("denied", "Denied"), ("expired", "Expired")]),
                      width="half", readonly=False)},
        {"field": "expires_at", "type": "timestamp",
         "meta": {"interface": "datetime", "width": "half",
                  "note": "Access auto-revokes at this time."}},
        {"field": "decided_at", "type": "timestamp",
         "meta": {"interface": "datetime", "width": "half", "readonly": True}},
    ], icon="key", note="Admin role requests time-boxed access to PII on a case.")
    add_m2o("pii_access_requests", "case", "cases", required=True, on_delete="CASCADE")
    add_m2o("pii_access_requests", "requested_by", "directus_users")
    add_m2o("pii_access_requests", "decided_by", "directus_users")
    add_audit_fields("pii_access_requests")


def main():
    login()
    COLLECTIONS.update(existing_collections())
    print(f"Connected to {URL}")
    build_lookups()
    build_organizations()
    build_people()
    build_cases()
    build_access_requests()
    print("\nDone.")


if __name__ == "__main__":
    main()
