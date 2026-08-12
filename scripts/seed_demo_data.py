#!/usr/bin/env python3
"""
Generate synthetic case records for testing and dashboard work.

    python3 scripts/seed_demo_data.py            # create 100 cases
    python3 scripts/seed_demo_data.py --count 50
    python3 scripts/seed_demo_data.py --purge    # remove everything it created

SYNTHETIC DATA — READ THIS
--------------------------
Every record here is invented. No row describes a real person or a real incident, and
the names in the PII tables are deliberately non-human ("SYNTHETIC Survivor 042") so a
generated record can never be confused with, or coincidentally resemble, a real case.
Every case is marked with the SYNTHETIC_MARK below and listed in a manifest file, so
`--purge` removes them completely before real data is loaded.

What *is* modelled on reality is the shape of the data, not its content: platform mix
weighted to how people in Cambodia actually communicate (Facebook and Messenger
dominant, Cool App present, X marginal), harassment types weighted to the frequencies
TFGBV documentation reports, most perpetrators strangers, most cases victim-reported,
and a long tail of unresolved cases. Uniform random data would make every dashboard
chart flat and hide exactly the skew the real charts need to show.
"""
import argparse
import datetime as dt
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_schema as s  # noqa: E402

SYNTHETIC_MARK = "[SYNTHETIC TEST DATA]"
MANIFEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "demo_manifest.json")

# ---------------------------------------------------------------- weighted vocabulary
# (value, weight) — weights are relative frequencies, not percentages.
PLATFORMS = [("facebook", 42), ("messenger", 20), ("tiktok", 12), ("telegram", 8),
             ("instagram", 6), ("youtube", 4), ("cool_app", 3), ("whatsapp", 2),
             ("x", 1), ("email", 1), ("website", 1)]

HARASSMENT = [("misogynistic_hate_speech", 20), ("cyber_bullying", 18),
              ("sexual_harassment_exploitation", 12), ("image_video_based_abuse", 10),
              ("impersonation", 8), ("doxxing", 7), ("cyber_stalking", 6),
              ("discrimination", 5), ("sextortion_blackmail", 4),
              ("mass_reporting_to_silence_victims", 3),
              ("controlling_social_media_accounts", 3),
              ("physical_safety_threats", 2), ("ai_generated_sexual_content", 2)]

GENDERS = [("female", 70), ("lgbtqia_community", 8), ("transgender", 5),
           ("lesbian", 4), ("gender_diverse_group", 4), ("bisexual", 3),
           ("queer_questioning", 2), ("prefer_not_to_say", 4)]

PERP_GENDERS = [("male", 62), ("female", 8), ("prefer_not_to_say", 30)]

IDENTITIES = [("public_user", 22), ("gender_advocate", 14), ("journalist", 12),
              ("cso_member", 10), ("political_activist", 9), ("human_rights_defender", 8),
              ("public_figure", 7), ("educator", 5), ("media_worker", 4),
              ("minority", 3), ("indigenous_peoples", 2),
              ("people_with_disabilities", 2), ("critic", 2), ("unknown", 6)]

RELATIONS = [("stranger", 46), ("not_applicable", 14), ("ex_partner", 12),
             ("colleague", 8), ("current_partner", 7), ("political_actor", 6),
             ("authorities_government_officials", 4), ("manager_supervisor", 3)]

STATUSES = [("ongoing", 26), ("investigating", 22), ("resolved", 20),
            ("referred", 14), ("cannot_resolve", 12), ("rejected", 6)]

INTERVENTIONS = [("documented", 22), ("continue_monitoring_the_case", 16),
                 ("technical_support", 12), ("submitted_report_to_platforms", 10),
                 ("provided_safety_guideline", 8), ("password_reset", 6),
                 ("2fa_setup", 6), ("page_account_recovery", 5),
                 ("provided_digital_security_training", 4),
                 ("mental_health_referrals", 4), ("legal_referrals", 3),
                 ("provided_safety_planning_to_victim", 2),
                 ("secured_digital_device", 1), ("not_applicable", 1)]

SEVERITIES = [("low", 28), ("moderate", 30), ("minimal", 18), ("high", 16),
              ("severe", 8)]

REPORTERS = [("survivor_self", 44), ("operator_outreach", 22),
             ("partner_organization", 12), ("family_or_friend", 9),
             ("hotline", 6), ("anonymous", 5), ("unknown", 2)]

# Generic, non-narrative descriptions. Deliberately not stories about people.
IMPACTS = [
    "Reported feeling unsafe online; reduced posting frequency.",
    "Temporarily deactivated the affected account.",
    "Reported anxiety and disrupted sleep following the incident.",
    "Withdrew from public commentary on the topic.",
    "Reported concern for family members' safety.",
    "No lasting impact reported at time of intake.",
    "Sought peer support within their organisation.",
]


def pick(weighted):
    values, weights = zip(*weighted)
    return random.choices(values, weights=weights, k=1)[0]


def pick_many(weighted, lo, hi):
    n = random.randint(lo, hi)
    chosen = []
    while len(chosen) < n:
        v = pick(weighted)
        if v not in chosen:
            chosen.append(v)
    return chosen


def m2m(field_key, values):
    return {"create": [{field_key: {"id": v}} for v in values],
            "update": [], "delete": []}


def skewed_date(start, days):
    """Cases cluster toward the recent end, as intake grows and memory is fresher."""
    frac = random.random() ** 0.65
    return start + dt.timedelta(days=int(frac * days))


def generate(count):
    orgs = s.api("GET", "/items/organizations?limit=-1&fields=id,name")
    if not orgs:
        raise SystemExit("no organizations — create at least one first")
    org_ids = [o["id"] for o in orgs]
    weights = [70] + [30 / max(1, len(org_ids) - 1)] * (len(org_ids) - 1)

    made = {"cases": [], "survivors": [], "perpetrators": [],
            "survivor_pii": [], "perpetrator_pii": []}
    start = dt.date.today() - dt.timedelta(days=540)

    for i in range(1, count + 1):
        support = random.random() < 0.22
        draft = random.random() < 0.12

        survivor = s.api("POST", "/items/survivors", {
            "gender": pick(GENDERS),
            "identities": m2m("identities_id", pick_many(IDENTITIES, 1, 2))})
        made["survivors"].append(survivor["id"])

        # PII on ~60% of survivors, so PII-flow testing has something to find.
        if random.random() < 0.6:
            pii = s.api("POST", "/items/survivor_pii", {
                "survivor": survivor["id"],
                "full_name": f"SYNTHETIC Survivor {i:03d}",
                "contact_phone": f"+855-000-{i:04d}",
                "contact_email": f"synthetic-survivor-{i:03d}@example.invalid",
                "notes": SYNTHETIC_MARK})
            made["survivor_pii"].append(pii["id"])

        perpetrator = None
        if random.random() < 0.78:
            perpetrator = s.api("POST", "/items/perpetrators", {
                "gender": pick(PERP_GENDERS),
                "identification_status": pick([("suspected", 58), ("known", 24),
                                               ("unknown", 18)]),
                "identities": m2m("identities_id", pick_many(IDENTITIES, 1, 1))})
            made["perpetrators"].append(perpetrator["id"])
            if random.random() < 0.35:
                ppii = s.api("POST", "/items/perpetrator_pii", {
                    "perpetrator": perpetrator["id"],
                    "full_name": f"SYNTHETIC Perpetrator {i:03d}",
                    "known_information": SYNTHETIC_MARK,
                    "profile_urls": [{"url": f"https://example.invalid/profile/{i:03d}"}]})
                made["perpetrator_pii"].append(ppii["id"])

        payload = {
            "case_type": "technical_support" if support else "tfgbv",
            "record_status": "draft" if draft else "submitted",
            "date_reported": skewed_date(start, 540).isoformat(),
            "organization": random.choices(org_ids, weights=weights, k=1)[0],
            "case_category": pick([("victim_reported", 68), ("operator_collected", 32)]),
            "case_status": pick(STATUSES),
            "reported_by": pick(REPORTERS),
            "survivor": survivor["id"],
            "perpetrator_relation": pick(RELATIONS),
            "caption_main_content": f"{SYNTHETIC_MARK} generated record {i:03d}",
            "incident_summary": (
                f"{SYNTHETIC_MARK} Synthetic record for testing and dashboard "
                f"development. Contains no real case information."),
            "impact_on_survivor": random.choice(IMPACTS),
            "platforms": m2m("platforms_id", pick_many(PLATFORMS, 1, 3)),
            "harassment_types": m2m("harassment_types_id", pick_many(HARASSMENT, 1, 3)),
            "interventions": m2m("interventions_id", pick_many(INTERVENTIONS, 1, 2)),
        }
        if perpetrator:
            payload["perpetrator"] = perpetrator["id"]
        # A submitted technical-support case must carry a severity (CHECK constraint).
        if support or random.random() < 0.5:
            payload["severity"] = pick(SEVERITIES)
        if random.random() < 0.4:
            payload["evidence_links"] = [
                {"url": f"https://example.invalid/post/{i:03d}",
                 "captured_at": payload["date_reported"]}]

        case = s.api("POST", "/items/cases", payload)
        made["cases"].append(case["id"])
        if i % 20 == 0:
            print(f"  {i}/{count} cases", flush=True)

    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, "w") as fh:
        json.dump(made, fh, indent=1)
    return made


def purge():
    try:
        with open(MANIFEST) as fh:
            made = json.load(fh)
    except FileNotFoundError:
        raise SystemExit("no manifest — nothing recorded as generated")
    # children first, then parents
    for coll in ("survivor_pii", "perpetrator_pii", "cases", "survivors",
                 "perpetrators"):
        ids = made.get(coll, [])
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            try:
                s.api("DELETE", f"/items/{coll}", batch)
            except SystemExit:
                pass
        print(f"  deleted {len(ids)} from {coll}")
    os.remove(MANIFEST)


def summarise():
    def agg(path):
        return s.api("GET", path)
    print("\n  cases by type:      ",
          agg("/items/cases?aggregate[count]=id&groupBy=case_type"))
    print("  cases by status:    ",
          agg("/items/cases?aggregate[count]=id&groupBy=record_status"))
    top = agg("/items/cases_platforms?aggregate[count]=id&groupBy=platforms_id"
              "&sort=-count.id&limit=5")
    print("  top platforms:      ", top)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--seed", type=int, default=20260812)
    args = ap.parse_args()

    random.seed(args.seed)
    s.login()
    if args.purge:
        purge()
    else:
        generate(args.count)
        summarise()
        print(f"\n  manifest: {MANIFEST}")
        print("  remove with: python3 scripts/seed_demo_data.py --purge")
