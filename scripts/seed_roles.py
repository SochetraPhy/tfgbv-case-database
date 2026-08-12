#!/usr/bin/env python3
"""
Create the five roles from slide 6 and their access policies.

Idempotent. Run after seed_schema.py:

    python3 scripts/seed_roles.py

Role model
----------
Super Admin  full access (the built-in Administrator policy)
Admin        read all cases, no PII; may request time-boxed PII access
Editor       manage all cases/survivors/perpetrators, never PII
Author       manage only their own organization's cases, never PII
Subscriber   read-only, submitted cases, dashboard fields only

PII lives in survivor_pii / perpetrator_pii. No policy below grants access to
either one; a separate "PII Access (temporary)" policy exists to be attached and
detached by the approval flow.
"""
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import seed_schema as s  # noqa: E402

VOCAB = list(s.LOOKUPS.keys())
JUNCTIONS = ["cases_platforms", "cases_harassment_types", "cases_interventions",
             "survivors_identities", "perpetrators_identities"]

# Fields a Subscriber may read on cases — deliberately excludes every free-text
# field, which is where identifying detail leaks in practice.
DASHBOARD_CASE_FIELDS = [
    "id", "case_code", "case_type", "date_reported", "case_category",
    "case_status", "severity", "perpetrator_relation", "organization",
    "platforms", "harassment_types", "interventions", "survivor", "perpetrator",
    # not sensitive, and a default column in the list view — denying it makes the
    # Content module error out for Subscribers instead of rendering
    "record_status",
]


def find(endpoint, name):
    for item in s.api("GET", f"/{endpoint}?limit=-1"):
        if item.get("name") == name:
            return item
    return None


def ensure_policy(name, description, admin=False, app=True, tfa=False):
    found = find("policies", name)
    if found:
        print(f"  = policy {name}")
        return found["id"]
    p = s.api("POST", "/policies", {
        "name": name, "description": description, "icon": "policy",
        "admin_access": admin, "app_access": app, "enforce_tfa": tfa})
    print(f"  + policy {name}")
    return p["id"]


def ensure_role(name, description, policy_id, icon="badge"):
    found = find("roles", name)
    if found:
        role_id = found["id"]
        print(f"  = role {name}")
    else:
        role_id = s.api("POST", "/roles", {
            "name": name, "description": description, "icon": icon})["id"]
        print(f"  + role {name}")
    links = s.api("GET", "/access?limit=-1")
    if not any(a["role"] == role_id and a["policy"] == policy_id for a in links):
        s.api("POST", "/access", {"role": role_id, "policy": policy_id, "sort": 1})
        print(f"    linked policy -> {name}")
    return role_id


def perm(policy_id, collection, action, fields=None, rules=None, presets=None,
         validation=None):
    existing = s.api(
        "GET", f"/permissions?limit=-1&filter[policy][_eq]={policy_id}"
               f"&filter[collection][_eq]={collection}&filter[action][_eq]={action}")
    payload = {"policy": policy_id, "collection": collection, "action": action,
               "fields": fields or ["*"], "permissions": rules or {},
               "validation": validation or {}, "presets": presets}
    if existing:
        s.api("PATCH", f"/permissions/{existing[0]['id']}", payload)
    else:
        s.api("POST", "/permissions", payload)


def read_vocabulary(policy_id):
    for c in VOCAB + ["organizations"] + JUNCTIONS:
        perm(policy_id, c, "read")


# Without these, a user cannot enrol in 2FA (403 on /users/me/tfa/generate) or change
# their own password. Combined with enforce_tfa that is a hard lockout: the app demands
# 2FA setup at login and the API refuses it. Every non-admin policy needs them.
SELF_SERVICE_FIELDS = [
    "first_name", "last_name", "password", "avatar", "language",
    "appearance", "theme_light", "theme_dark", "tfa_secret",
]


def allow_self_service(policy_id):
    me = {"id": {"_eq": "$CURRENT_USER"}}
    perm(policy_id, "directus_users", "read",
         fields=["id", "first_name", "last_name", "email", "avatar", "language",
                 "appearance", "theme_light", "theme_dark", "tfa_secret", "role",
                 "organization", "last_page", "status"],
         rules=me)
    perm(policy_id, "directus_users", "update", fields=SELF_SERVICE_FIELDS, rules=me)


def add_user_organization_field():
    """Author scoping needs an organization on the user record."""
    if "organization" not in s.existing_fields("directus_users"):
        s.api("POST", "/fields/directus_users", {
            "field": "organization", "type": "uuid", "schema": {},
            "meta": {"interface": "select-dropdown-m2o", "width": "half",
                     "note": "Scopes the Author role to this organization's cases.",
                     "options": {"template": "{{name}}"},
                     "display": "related-values",
                     "display_options": {"template": "{{name}}"}}})
        s.api("POST", "/relations", {
            "collection": "directus_users", "field": "organization",
            "related_collection": "organizations", "meta": {},
            "schema": {"on_delete": "SET NULL"}})
        print("  + directus_users.organization")


def main():
    s.login()
    print(f"Connected to {s.URL}\n[roles]")
    add_user_organization_field()

    # ---------------------------------------------------------------- Super Admin
    admin_policy = find("policies", "Administrator")["id"]
    ensure_role("Super Admin", "Full access control, including PII.",
                admin_policy, icon="shield_person")

    # --------------------------------------------------------------------- Admin
    p = ensure_policy("Admin", "Read all cases without PII; may request PII access.",
                      tfa=True)
    read_vocabulary(p)
    allow_self_service(p)
    for c in ("cases", "survivors", "perpetrators"):
        perm(p, c, "read")
    perm(p, "pii_access_requests", "create",
         fields=["reason", "case", "expires_at"],
         presets={"status": "pending", "requested_by": "$CURRENT_USER"})
    perm(p, "pii_access_requests", "read",
         rules={"requested_by": {"_eq": "$CURRENT_USER"}})
    ensure_role("Admin", "Reviews cases; requests time-boxed PII access.", p,
                icon="manage_accounts")

    # -------------------------------------------------------------------- Editor
    p = ensure_policy("Editor", "Manage all case data. Never sees PII.", tfa=True)
    read_vocabulary(p)
    allow_self_service(p)
    for c in ("cases", "survivors", "perpetrators"):
        for a in ("create", "read", "update", "delete"):
            perm(p, c, a)
    for c in JUNCTIONS:
        for a in ("create", "update", "delete"):
            perm(p, c, a)
    ensure_role("Editor", "Manages all case data without access to PII.", p,
                icon="edit_note")

    # -------------------------------------------------------------------- Author
    own_org = {"organization": {"_eq": "$CURRENT_USER.organization"}}
    p = ensure_policy("Author", "Manage own organization's cases. Never sees PII.",
                      tfa=True)
    read_vocabulary(p)
    allow_self_service(p)
    perm(p, "cases", "create", presets={"organization": "$CURRENT_USER.organization"})
    perm(p, "cases", "read", rules=own_org)
    perm(p, "cases", "update", rules=own_org,
         validation={"organization": {"_eq": "$CURRENT_USER.organization"}})
    perm(p, "cases", "delete", rules=own_org)
    # Survivor/perpetrator rows are reachable only through a case the Author can see.
    for c in ("survivors", "perpetrators"):
        for a in ("create", "read", "update"):
            perm(p, c, a)
    for c in JUNCTIONS:
        for a in ("create", "update", "delete"):
            perm(p, c, a)
    ensure_role("Author", "Manages their own organization's cases, without PII.", p,
                icon="drive_file_rename_outline")

    # ---------------------------------------------------------------- Subscriber
    p = ensure_policy("Subscriber", "Read-only dashboard access. No PII, no drafts.")
    read_vocabulary(p)
    allow_self_service(p)
    perm(p, "cases", "read", fields=DASHBOARD_CASE_FIELDS,
         rules={"record_status": {"_eq": "submitted"}})
    # The codes are pseudonymous, not PII, and every list that shows a linked survivor
    # resolves {{survivor_code}} — without them the view 403s instead of rendering.
    perm(p, "survivors", "read", fields=["id", "survivor_code", "gender", "identities"])
    perm(p, "perpetrators", "read",
         fields=["id", "perpetrator_code", "gender", "identities",
                 "identification_status"])
    ensure_role("Subscriber", "Views dashboard summaries only.", p, icon="visibility")

    # ------------------------------------------- temporary PII grant (flow-managed)
    p = ensure_policy(
        "PII Access (temporary)",
        "Attached by the approval flow when a PII access request is granted; "
        "detached at expiry. Do not assign to a role.", tfa=True)
    for c in ("survivor_pii", "perpetrator_pii"):
        perm(p, c, "read")
    print("  + policy PII Access (temporary) — grants read on both PII tables")

    print("\nDone.")


if __name__ == "__main__":
    main()
