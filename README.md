# TFGBV Case Database

A secure, self-hosted case management system for documenting Technology-Facilitated
Gender-Based Violence (TFGBV) incidents.

It replaces a flat Excel workbook with a relational database that separates personally
identifiable information from case data, enforces role-based access, and records an
audit trail of every change.

---

## Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Setup](#setup)
- [Data model](#data-model)
- [Roles and permissions](#roles-and-permissions)
- [How PII is protected](#how-pii-is-protected)
- [Database guarantees](#database-guarantees)
- [Test data](#test-data)
- [Operations](#operations)
- [Importing the legacy workbook](#importing-the-legacy-workbook)
- [Handling real case data](#handling-real-case-data)
- [Project layout](#project-layout)

---

## Why this exists

Case records were kept in a single spreadsheet containing survivor and perpetrator
details. That approach had four problems this system is built to solve:

| Problem | Approach here |
|---|---|
| PII sat alongside case data, visible to anyone with the file | PII isolated in separate tables, unreadable by every role except Super Admin |
| No way to grant partial access | Five roles, each seeing only what its work requires |
| No record of who changed what | Full revision history on every record |
| Multi-value fields crammed into one cell (`"Facebook, Instagram, TikTok"`) | Proper many-to-many relations, so the data is analysable |

---

## Architecture

| Service | Image | Purpose |
|---|---|---|
| `directus` | `directus/directus:11` | Application, admin UI, REST/GraphQL API |
| `database` | `postgis/postgis:16-3.4-alpine` | PostgreSQL with geospatial support |
| `cache` | `redis:7-alpine` | Cache and rate limiting |
| `pii-access-sync` | built locally | Grants and revokes temporary PII access |

PostGIS rather than plain Postgres because Directus uses geometry types for map fields.
Redis is required for caching to be safely shared if the app is ever scaled beyond one
container.

---

## Requirements

- Docker Desktop (or Docker Engine with Compose v2)
- Python 3.9+ on the host, for the setup scripts — standard library only, no packages
- Roughly 2 GB of free disk

---

## Setup

**1. Configure the environment**

```bash
cp .env.example .env
```

Edit `.env` and set every value. Generate a strong `SECRET`:

```bash
openssl rand -hex 32
```

`ADMIN_EMAIL` and `ADMIN_PASSWORD` create the first administrator, and apply **only when
the database is empty** — changing them later has no effect. Change the password from
inside the application instead.

**2. Start the stack**

```bash
docker compose up -d
```

First run pulls images and initialises the database; allow a few minutes. Check progress
with `docker compose logs -f directus`.

**3. Build the schema**

```bash
python3 scripts/seed_schema.py
```

Creates all collections, fields, relations, and junction tables, then loads the ten
controlled vocabularies.

**4. Create roles and permissions**

```bash
python3 scripts/seed_roles.py
```

**5. Apply database constraints**

```bash
./scripts/apply_migrations.sh
```

Installs the sequences, triggers, and CHECK constraints described under
[Database guarantees](#database-guarantees).

**6. Sign in**

Open <http://localhost:8055> and log in with the administrator credentials from `.env`.

All three scripts are idempotent — re-running them is safe and is the supported way to
repair configuration that has drifted.

---

## Data model

One `cases` table sits at the centre. Everything else either describes a case or supplies
a controlled value for one of its fields.

```
                    organizations
                          │
    survivors ──┐         │        ┌── platforms          (many-to-many)
                ├──────  cases  ───┼── harassment_types   (many-to-many)
 perpetrators ──┘         │        └── interventions      (many-to-many)
       │                  │
       │                  ├── case_categories, case_statuses, severity_levels,
       │                  │   perpetrator_relations, reporters   (single-value)
       │                  │
  survivor_pii            └── pii_access_requests
  perpetrator_pii
```

**Core tables**

| Table | Contents |
|---|---|
| `cases` | One row per case, TFGBV or technical support, distinguished by `case_type` |
| `survivors` / `perpetrators` | Non-identifying attributes only, referenced by pseudonymous codes (`SUR-0001`, `PER-0001`) |
| `survivor_pii` / `perpetrator_pii` | **Restricted.** Names, contacts, and identifying detail |
| `organizations` | Owning organisation; scopes the Author role |
| `pii_access_requests` | Time-boxed requests to view PII on a specific case |

**Controlled vocabularies** (ten tables): case categories, case statuses, genders,
harassment types, identities, interventions, perpetrator relations, platforms, reporters,
severity levels. Each uses a stable text key as its primary key, with a display label and
an `active` flag so a value can be retired without breaking historical records.

### Design decisions worth knowing

**Platforms, harassment types, and interventions are many-to-many.** A single incident
routinely spans several platforms and several forms of harassment. Modelling these as
single foreign keys would have forced a choice of one and lost the rest.

**Survivors and perpetrators share one `identities` vocabulary.** The two lists were
identical; keeping them separate would guarantee they drift apart.

**TFGBV and technical support cases share one table**, separated by `case_type`. They
share most fields, and a single table means reporting spans both without a union.

**Reference codes are generated by the database, not the application.** See
[Database guarantees](#database-guarantees).

**Survivors and perpetrators are identified by pseudonymous codes.** The non-PII tables
are fully usable for analysis without ever holding a name.

---

## Roles and permissions

| Role | Case data | PII | Scope |
|---|---|---|---|
| **Super Admin** | Full control | Full access | Everything |
| **Admin** | Read only | None directly — may request time-boxed access | All organisations |
| **Editor** | Create, read, update, delete | Never | All organisations |
| **Author** | Create, read, update, delete | Never | Own organisation only |
| **Subscriber** | Read, submitted cases only, restricted fields | Never | All organisations |

`Administrator` also appears in the role list — it is Directus's built-in role, created
during installation, and carries the same unrestricted policy as Super Admin.

**Author scoping** is enforced by a permission filter comparing each case's organisation
to the user's own, so an Author cannot read, edit, or delete another organisation's
records. New cases they create are automatically assigned to their organisation.

**Subscriber restrictions** exclude every free-text field — summaries, captions, impact
notes — because that is where identifying detail tends to appear in practice. They also
cannot see drafts.

**Two-factor authentication** is enforced for Admin, Editor, and Author. Users are
prompted to enrol at first sign-in and cannot use the application until they do.

> **Note:** 2FA enforcement is applied by the application, not the API. A valid
> password alone still authenticates against `/auth/login`. If MFA must hold at the API
> layer, place an identity provider such as Keycloak in front of Directus.

### Adding a user

Create the user in **User Directory**, assign a role, and — for Author — set their
organisation. They will be prompted to enrol in 2FA at first sign-in if their role
requires it.

---

## How PII is protected

Survivor and perpetrator identities live in `survivor_pii` and `perpetrator_pii`, two
tables no standard role can read. Everything else references people by pseudonymous code,
so cases remain fully workable without exposing anyone.

When an Admin genuinely needs to see identifying detail:

1. They file a request against a specific case, with a reason and an expiry time.
2. A Super Admin reviews it and sets the status to `approved`.
3. The `pii-access-sync` service grants read access within five minutes.
4. When the expiry passes, access is revoked automatically and the request is marked
   `expired`.

> **Scope of a grant:** while a request is active it confers read access to *all* rows in
> both PII tables, not only the case it names. Directus 11 cannot express the narrower
> rule — a permission filter that walks from a PII row to its case's approvals generates
> invalid SQL, as recorded in [`docs/DECISIONS.md`](docs/DECISIONS.md). The case reference
> is therefore the recorded justification for access and the basis for the audit trail,
> not a technical boundary. Keep expiry windows short, and treat approval as granting the
> whole table for that period.

The service **reconciles** rather than reacting to events: on every pass it makes actual
access match the set of currently-valid approvals, adding what is missing and removing
everything else. A missed event, a failed grant, or an entry somebody added by hand all
converge back to the correct state on the next pass, rather than accumulating silently.

> **Operational note:** this service runs with administrator credentials, and that is
> unavoidable. In Directus, the ability to grant a policy includes the ability to grant
> the administrator policy, so a restricted service account for this task offers no real
> containment. Protect `.env` accordingly, and revoke access by editing the request
> record rather than the grant.

Because PII is isolated in two tables rather than spread across the schema, it can later
be moved to an external vault by replacing how those two tables are read — without
touching the rest of the model.

---

## Database guarantees

Some rules are enforced in PostgreSQL rather than in the application, so they cannot be
bypassed by a direct database write or lost to a race between two people saving at once.
All of them are in [`scripts/migrations.sql`](scripts/migrations.sql).

**Reference codes** are assigned by sequence-backed triggers: `TFGBV0001` for TFGBV
cases, `TS0001` for technical support, `SUR-0001` and `PER-0001` for people. Sequences
are initialised past any existing codes, so imported historical records never collide
with newly generated ones.

**Draft and submitted cases are held to different standards.** A draft may be incomplete.
A *submitted* case must have a report date, category, and organisation. A submitted
technical-support case must additionally carry a severity and a case status. This is what
makes save-as-draft safe: incomplete work can be stored without weakening the rules that
apply to finished records.

Case status is deliberately **not** required on TFGBV cases. The legacy masterfile has no
status column — only the technical-support sheet records one — so requiring it would
block migration of every historical record, or force someone to invent a status for each.
Migrated cases arrive without a status; assigning them is a deliberate triage pass, not
part of the import.

**One PII record per person**, enforced by unique index.

---

## Test data

```bash
python3 scripts/seed_demo_data.py --count 100
```

Generates synthetic cases with realistic distributions — platform mix, harassment-type
frequencies, mostly-stranger perpetrators, a long tail of unresolved cases — so that
dashboards and reports show meaningful shape during development.

Every generated record is invented. No row describes a real person or a real incident;
PII names are deliberately non-human (`SYNTHETIC Survivor 042`), contact details use the
reserved `.invalid` domain, and every case is tagged `[SYNTHETIC TEST DATA]`. Generation
is deterministic, so the same command reproduces the same dataset.

```bash
python3 scripts/seed_demo_data.py --purge
```

**Purge before loading real case data.** Synthetic records are clearly labelled, but the
guarantee should be that they are gone, not that they are marked.

---

## Operations

**Logs**

```bash
docker compose logs -f directus
```

**Stop, keeping all data**

```bash
docker compose down
```

Data lives in the `db_data` volume and `./uploads`; only `docker compose down -v`
destroys it.

**Back up the database**

```bash
docker compose exec -T database pg_dump -U directus directus > backup.sql
```

A backup contains all PII in plaintext. Store it encrypted, and never inside this
repository — `.gitignore` blocks dump formats, but the safer habit is to write backups
somewhere else entirely.

**Restore**

```bash
docker compose exec -T database psql -U directus directus < backup.sql
```

**After changing `pii_access_sync.py` or `seed_schema.py`**

```bash
docker compose build pii-access-sync
```

The service runs from a built image rather than a mounted directory, so changes require
a rebuild.

**Health**

The container healthcheck uses `/server/ping`. The fuller `/server/health` endpoint also
probes file storage, which reports failures on macOS bind mounts even when uploads work
correctly.

---

## Importing the legacy workbook

```bash
pip3 install openpyxl
```

```bash
python3 scripts/import_legacy.py "/path/to/workbook.xlsm"
```

Dry run by default: it reads the workbook, resolves every value against the
vocabularies, and reports exactly what it would create — including anything it could not
map. Nothing is written until `--commit` is passed.

Multi-value cells (`"Facebook, Instagram, Tik Tok"`) are split into the many-to-many
junctions, and labels are matched case- and punctuation-insensitively with an alias table
covering known drift between the workbook and the dashboard sheet. Unmatched values are
always reported, never silently dropped.

The workbook contains PII. Keep it on an encrypted volume outside this repository, and
review the dry-run report before committing an import.

---

## Handling real case data

- The populated case workbook contains survivor and perpetrator PII. Keep it on an
  encrypted volume, outside this repository.
- Never commit case data. `.gitignore` blocks spreadsheet, dump, archive, and export
  formats, but treat that as a safety net rather than a policy.
- Screenshots of the application can contain PII. Check before sharing them.
- Database backups are unencrypted by default. Encrypt them at rest and test a restore
  before relying on one.
- Before production use: serve over TLS, move secrets out of `.env` into a managed secret
  store, and restrict network access to the application.

---

## Project layout

```
docker-compose.yml          Service definitions
.env                        Secrets and configuration (never committed)
.env.example                Template for .env
docker/
  pii-sync.Dockerfile       Image for the access reconciler
scripts/
  seed_schema.py            Collections, fields, relations, vocabularies
  seed_roles.py             Roles, policies, permissions
  migrations.sql            Sequences, triggers, constraints, indexes
  apply_migrations.sh       Applies migrations.sql
  pii_access_sync.py        Temporary PII access reconciler
  seed_demo_data.py         Synthetic test data
  import_legacy.py          One-off import of the legacy workbook
docs/
  DECISIONS.md              Why the system is built this way; open questions
```

The scripts are the source of truth for schema and permissions. Prefer editing them and
re-running over making changes in the admin UI, so the configuration stays reproducible.
