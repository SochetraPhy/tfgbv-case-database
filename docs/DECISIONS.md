# Design decisions

Why the system is built the way it is. The README covers how to run it; this covers the
reasoning behind the parts that are not obvious, and the approaches that were tried and
rejected — so they are not attempted again.

---

## 1. Where the schema departs from the original ERD

The data model in `Data Modelling.xlsx` and the schema slide could not be implemented as
drawn. Six changes were necessary.

**Multi-value fields became many-to-many relations.** The ERD gave each case a single
`platform_id` and `harrassment_type_id`, but the source workbook stores
`"Facebook, Instagram, Tik Tok"` and `"Misogynistic Hate-Speech, Cyber-bullying"` in one
cell. Real incidents span several platforms and several forms of harassment.
Single foreign keys would have forced a choice of one and discarded the rest, making the
data unanalysable for exactly the questions the dashboard asks.

**Survivor and perpetrator identity lists were merged.** The two vocabularies were
identical, entry for entry. Keeping them separate guaranteed they would drift apart.

**`case_category` was added to the case.** The lookup existed and the legacy column was
populated, but the ERD had no foreign key for it — despite it being a dashboard
dimension.

**`severity_levels` was added.** Defined in the workbook with five values, used by the
technical-support sheet, absent from the ERD entirely.

**TFGBV and technical-support cases share one table**, separated by `case_type`. The
legacy workbook kept them on separate sheets, but they share most fields, and one table
means reporting spans both without a union. Relatedly, `technical_Support` was removed
from the harassment-type vocabulary: it is an intervention, not a form of harassment.

**`organizations` was added.** The Author role is defined as managing "his/her
organizational data", which is unenforceable without an organisation on the case and on
the user.

Smaller corrections: `gender` became a controlled vocabulary rather than free text;
evidence links were added; typos were fixed before they became permanent column names
(`harrassment`, `disailities`).

### Still provisional

- The `reporters` vocabulary is invented. The source files define the lookup but never
  populate it; the seven values are placeholders awaiting confirmation.
- `Male` was added to `genders`. The workbook omitted it, but perpetrator gender is a
  dashboard chart. Confirm whether the omission was deliberate.

---

## 2. PII protection: three designs, two rejected

**Rejected — evaluating approval at query time.** The ideal design: a permission rule on
`survivor_pii` that walks to the case's access requests and checks for an approved,
unexpired one. No grants to manage, expiry automatic. **It does not work on Directus
11.17.4** — `_some` on a one-to-many inside a *permission* filter generates invalid SQL
(`CASE WHEN 1 THEN 1 END`, which PostgreSQL rejects). The same filter works correctly in
an ordinary query; only permission rules break. Worth retrying on a later version, as it
would remove the reconciler entirely.

**Rejected — a scoped service account holding grant rights.** Tested and confirmed to be
false security: an account that can write `directus_access` can attach the *Administrator*
policy to itself. Directus does not restrict which policy may be granted. Any account
able to grant PII access is administrator-equivalent, so a "limited" one only disguises
that.

**In use — a reconciler with administrator credentials.** Every five minutes it expires
overdue requests and makes grants match currently-valid approvals exactly, adding and
removing. Self-healing: a grant added by hand is removed on the next pass. It holds
administrator credentials by necessity, which is documented rather than hidden.

### What this design does not protect against

PII is stored **unencrypted** in PostgreSQL. The protection is Directus's permission
layer, which means direct database access, a `pg_dump`, or a stolen backup exposes every
name in the clear. It is an access-control boundary, not a cryptographic one.

PII is isolated in two tables referenced by pseudonymous codes specifically so this can
change later: adopting a vault means altering how those two tables store and resolve
values, not remodelling anything else.

---

## 3. Deferred: Keycloak and Databunker

Both appeared in the original technology stack. Neither is in use.

**Databunker** would store PII in a separate encrypted service and keep only opaque
tokens in PostgreSQL, so a database dump yields tokens rather than names, and deleting a
vault record renders every token pointing at it permanently useless. It requires a custom
Directus extension to tokenise on write and resolve on read, plus a second stateful
service with its own key management. It is the largest remaining item, and it closes the
gap described above.

**Keycloak** would move authentication to an external identity provider via OpenID
Connect, giving central account lifecycle, enforced password policy, and — importantly —
MFA that holds at the API layer. It earns its place if SSO across other systems is
needed, or if the API-layer gap below must be closed. Otherwise it is another service to
patch on a machine holding case data.

**Before either:** encrypted backups with a tested restore, and TLS. Both are cheaper and
close larger holes.

---

## 4. Rules enforced in PostgreSQL rather than the application

Reference codes, draft/submitted completeness, and one-PII-record-per-person are
implemented as sequences, triggers, and CHECK constraints rather than Directus Flows.

A Flow runs in the API layer: it can be skipped by a direct SQL write and can race when
two operators save simultaneously. A trigger cannot. For identifiers and integrity rules
that is the correct trade, and the cost is only that constraint violations surface as
database errors rather than friendly messages.

**Case status is deliberately not required on submitted TFGBV cases.** The legacy
masterfile has no status column — only the technical-support sheet records one. Requiring
it would block migration of every historical record, or force someone to invent a status
for each. Migrated cases arrive without a status; assigning them is a triage pass, not
part of the import.

---

## 5. Directus behaviours worth knowing

**`enforce_tfa` is applied by the application, not the API.** A user with 2FA enforced
still authenticates over `/auth/login` with a password alone. If MFA must hold at the API
layer, that requires an identity provider in front.

**Users need update rights on their own record to enrol in 2FA.** Without them,
`/users/me/tfa/generate` returns 403 while the app still demands enrolment — a hard
lockout. Every non-administrator policy grants a narrow self-service permission for this
reason.

**Many-to-many display templates resolve against the junction row**, so they must hop
through the junction's foreign key (`{{interventions_id.label}}`, not `{{label}}`). The
short form throws FORBIDDEN when the form loads.

**The `labels` display needs its own copy of the choices** in `display_options`, or it
title-cases raw values ("tfgbv" → "Tfgbv").

**Insights does not support cross-filtering** — clicking a slice to filter other panels.
Dashboard-level filters are supported. If true cross-filtering is required, that implies
a separate frontend or a BI tool over a sanitised view. **This is still an open
question.**

---

## 6. Environment: macOS Docker bind mounts

Docker Desktop bind mounts under `~/Documents` (TCC-protected) fail reads with
`errno 35, Resource deadlock would occur`. Two consequences, both handled:

- The reconciler is built into an image rather than mounting `./scripts`. Python read the
  mounted source as **0 bytes and exited cleanly**, doing nothing — a silent failure.
  Editing those scripts therefore requires `docker compose build pii-access-sync`.
- The healthcheck uses `/server/ping`, not `/server/health`, whose storage probe fails on
  the same mount even though uploads work.

Granting Docker Desktop Full Disk Access, or moving the project outside `~/Documents`,
would fix this at the source.

---

## 7. Defects found by testing each role

Verified by logging in as every role, through the UI and through the calls the app makes
on load. API-level permission checks alone did not catch the first two — both appear only
when the application loads.

1. **Admin, Editor, and Author were completely locked out** — the 2FA enrolment gap above.
2. **The case form was broken for every non-administrator role** — the many-to-many
   template bug above.
3. **Subscriber views failed on linked people** — `survivor_code` was not readable, so any
   list resolving `{{survivor_code}}` returned 403. The codes are pseudonymous, not PII.
4. **Dropdowns showed raw values.**
5. **Junction tables appeared in the navigation.**

Also verified: a fresh installation following the README produces a working system, and
reference codes now begin at 0001 rather than 0002.

---

## 8. Open questions

1. **Dashboard cross-filtering** — does the whiteboard sketch require true cross-filtering?
   This determines whether Insights is sufficient.
2. **`reporters` vocabulary** — the seven values are invented and need confirmation.
3. **`Male` in the gender list** — deliberate omission in the source, or oversight?
4. **`Administrator` and `Super Admin`** are duplicate full-access roles; the built-in
   one could be renamed and the duplicate removed.

## 9. Remaining work

1. Run the import against the populated workbook and review the unmapped report.
2. Triage pass to assign case statuses to migrated TFGBV cases.
3. Dashboard — the nine charts, once question 1 above is settled.
4. CSV import and blank template export.
5. Friendlier validation messages layered over the CHECK constraints.
6. Hardening: encrypted tested backups, TLS, secrets out of `.env`.
