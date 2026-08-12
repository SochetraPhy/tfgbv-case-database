-- Database-level guarantees that sit underneath Directus.
--
-- These are deliberately NOT Directus Flows. A Flow runs in the API layer, so it can be
-- skipped by a direct SQL write and can race when two operators submit at once. A
-- sequence plus a BEFORE INSERT trigger is atomic and holds no matter how the row arrives.
--
-- Idempotent — safe to re-run. Applied by scripts/apply_migrations.sh.

-- ---------------------------------------------------------------- reference codes
CREATE SEQUENCE IF NOT EXISTS case_code_seq;
CREATE SEQUENCE IF NOT EXISTS survivor_code_seq;
CREATE SEQUENCE IF NOT EXISTS perpetrator_code_seq;

-- Cases: TFGBV0001 for TFGBV cases, TS0001 for technical support cases.
CREATE OR REPLACE FUNCTION set_case_code() RETURNS trigger AS $$
BEGIN
  IF NEW.case_code IS NULL OR NEW.case_code = '' THEN
    NEW.case_code :=
      CASE WHEN NEW.case_type = 'technical_support' THEN 'TS' ELSE 'TFGBV' END
      || lpad(nextval('case_code_seq')::text, 4, '0');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_case_code ON cases;
CREATE TRIGGER trg_case_code BEFORE INSERT ON cases
  FOR EACH ROW EXECUTE FUNCTION set_case_code();

-- Survivors and perpetrators: pseudonymous references, never names.
CREATE OR REPLACE FUNCTION set_survivor_code() RETURNS trigger AS $$
BEGIN
  IF NEW.survivor_code IS NULL OR NEW.survivor_code = '' THEN
    NEW.survivor_code := 'SUR-' || lpad(nextval('survivor_code_seq')::text, 4, '0');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_survivor_code ON survivors;
CREATE TRIGGER trg_survivor_code BEFORE INSERT ON survivors
  FOR EACH ROW EXECUTE FUNCTION set_survivor_code();

CREATE OR REPLACE FUNCTION set_perpetrator_code() RETURNS trigger AS $$
BEGIN
  IF NEW.perpetrator_code IS NULL OR NEW.perpetrator_code = '' THEN
    NEW.perpetrator_code := 'PER-' || lpad(nextval('perpetrator_code_seq')::text, 4, '0');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_perpetrator_code ON perpetrators;
CREATE TRIGGER trg_perpetrator_code BEFORE INSERT ON perpetrators
  FOR EACH ROW EXECUTE FUNCTION set_perpetrator_code();

-- Start each sequence past any codes already loaded, so migrated legacy data
-- (TFGBV001, SUR-0001 ...) never collides with newly generated codes.
--
-- The third argument is is_called: false on an empty table so the first record gets
-- 0001 rather than 0002, true when rows already exist so the next code continues past
-- the highest one found.
SELECT setval('case_code_seq',
  GREATEST(1, COALESCE(m, 0)), COALESCE(m, 0) > 0)
FROM (SELECT MAX(NULLIF(regexp_replace(case_code, '\D', '', 'g'), ''))::int AS m
      FROM cases) q;
SELECT setval('survivor_code_seq',
  GREATEST(1, COALESCE(m, 0)), COALESCE(m, 0) > 0)
FROM (SELECT MAX(NULLIF(regexp_replace(survivor_code, '\D', '', 'g'), ''))::int AS m
      FROM survivors) q;
SELECT setval('perpetrator_code_seq',
  GREATEST(1, COALESCE(m, 0)), COALESCE(m, 0) > 0)
FROM (SELECT MAX(NULLIF(regexp_replace(perpetrator_code, '\D', '', 'g'), ''))::int AS m
      FROM perpetrators) q;

-- ------------------------------------------------------ save-draft / submit validation
-- Drafts may be incomplete; a submitted case may not be. This is the integrity floor,
-- with a Directus Flow layered on top for a readable message in the form.
-- NOTE: case_status is deliberately NOT required here. The legacy TFGBV masterfile has
-- no status column at all — only the Technical Support sheet records one — so requiring
-- it would either block migration of every historical case or force someone to invent a
-- status for each. Statuses on migrated TFGBV cases have to be a deliberate triage pass,
-- not a side effect of an import.
ALTER TABLE cases DROP CONSTRAINT IF EXISTS cases_submitted_completeness;
ALTER TABLE cases ADD CONSTRAINT cases_submitted_completeness CHECK (
  record_status <> 'submitted' OR (
        date_reported  IS NOT NULL
    AND case_category  IS NOT NULL
    AND organization   IS NOT NULL
  )
);

-- A technical support case records both severity and status in the source data, so both
-- are required for it; a TFGBV case carries neither.
ALTER TABLE cases DROP CONSTRAINT IF EXISTS cases_support_needs_severity;
ALTER TABLE cases ADD CONSTRAINT cases_support_needs_severity CHECK (
  record_status <> 'submitted'
  OR case_type <> 'technical_support'
  OR (severity IS NOT NULL AND case_status IS NOT NULL)
);

-- ------------------------------------------------- PII request subject denormalization
-- A request is filed against a case, but the permission rule has to be evaluated on
-- survivor_pii / perpetrator_pii. Directus cannot generate valid SQL for a permission
-- filter that chains two o2m hops (survivor -> cases -> requests): it emits
-- `CASE WHEN 1 THEN 1 END`, which Postgres rejects. So the request carries its subject
-- directly, giving the rule a single hop. Maintained by trigger, never by hand, so it
-- cannot drift from the case it belongs to.
CREATE OR REPLACE FUNCTION set_pii_request_subject() RETURNS trigger AS $$
BEGIN
  SELECT c.survivor, c.perpetrator INTO NEW.survivor, NEW.perpetrator
  FROM cases c WHERE c.id = NEW."case";
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pii_request_subject ON pii_access_requests;
CREATE TRIGGER trg_pii_request_subject
  BEFORE INSERT OR UPDATE OF "case" ON pii_access_requests
  FOR EACH ROW EXECUTE FUNCTION set_pii_request_subject();

-- ------------------------------------------------------------------ PII one-to-one
-- Exactly one PII row per person, enforced in the database rather than by convention.
CREATE UNIQUE INDEX IF NOT EXISTS survivor_pii_survivor_uniq
  ON survivor_pii (survivor);
CREATE UNIQUE INDEX IF NOT EXISTS perpetrator_pii_perpetrator_uniq
  ON perpetrator_pii (perpetrator);

-- --------------------------------------------------------------------- dashboard
-- Reporting indexes for the dashboard's date and status aggregations.
CREATE INDEX IF NOT EXISTS cases_date_reported_idx ON cases (date_reported);
CREATE INDEX IF NOT EXISTS cases_record_status_idx ON cases (record_status);
CREATE INDEX IF NOT EXISTS cases_organization_idx  ON cases (organization);
