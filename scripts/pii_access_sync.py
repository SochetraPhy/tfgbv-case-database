#!/usr/bin/env python3
"""
Reconcile temporary PII access against approved, unexpired requests.

Run on a schedule. Each pass:

  1. marks approved requests whose `expires_at` has passed as `expired`
  2. computes the set of users holding a valid approval right now
  3. makes the "PII Access (temporary)" policy's user grants match that set exactly —
     adding what is missing, removing everything else

Written as a reconciler rather than a grant-on-approve hook so it is self-healing: a
missed event, a failed webhook, or a grant somebody added by hand all converge back to
"access equals valid approvals" on the next pass. Nothing accumulates silently, which is
the failure mode that matters when the thing being granted is access to survivor PII.

    python3 scripts/pii_access_sync.py [--once]

SECURITY — read before deploying
--------------------------------
This process runs with administrator credentials, and that is unavoidable: in Directus,
writing `directus_access` is how policies are granted, so anything able to grant PII
access can also grant itself the Administrator policy. A "scoped" service account for
this job was tested and rejected — it could self-grant admin, so it offered no real
containment, only the appearance of it. Treat this process as admin-equivalent and
protect its credentials accordingly.

The alternative — evaluating approvals at query time via a permission rule — does not
work on Directus 11.17.4: `_some` on a one-to-many produces invalid SQL inside a
permission filter (`CASE WHEN 1 THEN 1 END`). Worth retrying on a later version, since
it would remove the need for this process entirely.
"""
import datetime as dt
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import seed_schema as s  # noqa: E402

POLICY_NAME = "PII Access (temporary)"
INTERVAL_SECONDS = 300
# Directus access tokens last 15 minutes by default. Refresh on a timer rather than
# waiting to discover expiry through a failed pass, which would skip that pass.
TOKEN_REFRESH_SECONDS = 600


def now_iso():
    # Z-suffixed, not +00:00 — a literal "+" in a query string decodes as a space.
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def policy_id():
    for p in s.api("GET", "/policies?limit=-1"):
        if p["name"] == POLICY_NAME:
            return p["id"]
    raise SystemExit(f"policy {POLICY_NAME!r} not found — run seed_roles.py first")


def reconcile(verbose=True):
    pid = policy_id()
    changes = []

    # 1. expire anything past its window
    stale = s.api("GET", "/items/pii_access_requests"
                         "?filter[status][_eq]=approved"
                         f"&filter[expires_at][_lt]={now_iso()}"
                         "&fields=id&limit=-1") or []
    for req in stale:
        s.api("PATCH", f"/items/pii_access_requests/{req['id']}",
              {"status": "expired"})
        changes.append(f"expired request {req['id'][:8]}")

    # 2. who should hold access right now
    valid = s.api("GET", "/items/pii_access_requests"
                         "?filter[status][_eq]=approved"
                         f"&filter[expires_at][_gt]={now_iso()}"
                         "&fields=requested_by&limit=-1") or []
    should_have = {r["requested_by"] for r in valid if r.get("requested_by")}

    # 3. make grants match, in both directions
    granted = s.api("GET", f"/access?filter[policy][_eq]={pid}"
                           "&fields=id,user,role&limit=-1") or []
    has_now = {g["user"]: g["id"] for g in granted if g.get("user")}

    for user in should_have - set(has_now):
        s.api("POST", "/access", {"user": user, "policy": pid, "sort": 1})
        changes.append(f"granted PII access to {user[:8]}")

    for user, access_id in has_now.items():
        if user not in should_have:
            s.api("DELETE", f"/access/{access_id}")
            changes.append(f"revoked PII access from {user[:8]}")

    if verbose:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        if changes:
            for c in changes:
                print(f"[{stamp}] {c}", flush=True)
        else:
            print(f"[{stamp}] no change ({len(should_have)} active grant(s))",
                  flush=True)
    return changes


def main():
    once = "--once" in sys.argv
    s.login()
    if once:
        reconcile()
        return
    print(f"reconciling every {INTERVAL_SECONDS}s against {s.URL}", flush=True)
    last_login = time.monotonic()
    while True:
        try:
            if time.monotonic() - last_login >= TOKEN_REFRESH_SECONDS:
                s.login()
                last_login = time.monotonic()
            reconcile()
        except (SystemExit, Exception) as e:   # keep the loop alive through API blips
            print(f"error: {str(e)[:200]}", flush=True)
            try:
                s.login()
                last_login = time.monotonic()
            except (SystemExit, Exception) as relogin_error:
                # Nothing left to do but wait for the next pass; exiting here would
                # hand recovery to the container restart policy instead.
                print(f"re-login failed: {str(relogin_error)[:200]}", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
