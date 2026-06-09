"""
Unit tests for S4 Pass D — NCES CCD entity roster verification.

All tests are OFFLINE: the CCD roster fetch is monkeypatched, so no network
call is made. They cover entity extraction, demotion of unconfirmed
role-assignment claims (the KIPP case), confirmation, graceful degradation
when the roster is unavailable, and the entity_verification summary block.
"""
import pipeline.s4_verification as s4
from pipeline.ccd_entity_verifier import extract_named_entities


# ── extract_named_entities ───────────────────────────────────────────────────
def test_extract_operator_signal_captures_acronym():
    assert extract_named_entities("Proven local operators: KIPP, MAS, others present") == ["KIPP"]


def test_extract_multiword_titlecase():
    assert "Explore Academy" in extract_named_entities("KIPP, MAS, Cottonwood, Explore Academy active")


def test_extract_rejects_grant_and_program_codes():
    # "CFDA 84.282" / "CIP 13" must NOT be treated as named entities.
    assert extract_named_entities("operators performed CSP-funded (CFDA 84.282) work") == []
    assert extract_named_entities("2351 (CIP 13) completers statewide") == []


def test_extract_signal_requires_real_capitalized_token():
    # "no CMO presence" — 'presence' is lowercase, must not be captured.
    assert extract_named_entities("1 independent charter; no CMO presence") == []


def test_extract_excludes_generic_terms_and_empty():
    assert extract_named_entities("2 authorizer types available: state and local district") == []
    assert extract_named_entities("") == []


# ── Pass D behavior ──────────────────────────────────────────────────────────
def _roster(*names):
    return {
        "leaid": "3500060",
        "district_name": "TEST DISTRICT",
        "charter_schools": [{"name": n, "ncessch": "x", "enrollment": 100} for n in names],
        "fetched_at": "2026-06-01T00:00:00",
        "year": 2023,
        "lookup_mode": "leaid",
        "source": s4.CCD_SOURCE_LABEL,
    }


def _kipp_fact():
    return {
        "datapoint_id": "dp_test_replication_feasibility_001",
        "dimension": "replication_feasibility",
        "fact_key": "replication_readiness_index",
        "claim": "Proven local operators: KIPP, MAS, others present",
        "confidence": "HIGH",
        "verification_status": "PROVISIONAL",
        "in_main_analysis": True,
    }


def test_pass_d_demotes_unconfirmed_role_claim(monkeypatch):
    monkeypatch.setattr(s4, "fetch_charter_roster",
                        lambda **kw: _roster("EAST MOUNTAIN HIGH SCHOOL", "SOUTH VALLEY ACADEMY"))
    facts, summary = s4._pass_d_entity_verification([_kipp_fact()], "nm-test", "NM", None)
    f = facts[0]
    assert f["in_main_analysis"] is False
    assert f["confidence"] == "MODERATE"
    assert f["verification_status"] == "PROVISIONAL"
    assert f["entity_verified"] is False
    assert "KIPP" in f["needs_verification_reason"]
    assert "NCES CCD" in f["needs_verification_reason"]
    assert summary["roster_fetched"] is True
    assert summary["entities_flagged"] == 1
    assert summary["entities_confirmed"] == 0
    assert summary["facts_checked"] == 1


def test_pass_d_confirms_matching_entity(monkeypatch):
    fact = _kipp_fact()
    fact["claim"] = "East Mountain High School operates locally"
    monkeypatch.setattr(s4, "fetch_charter_roster",
                        lambda **kw: _roster("EAST MOUNTAIN HIGH SCHOOL"))
    facts, summary = s4._pass_d_entity_verification([fact], "nm-test", "NM", None)
    f = facts[0]
    assert f["entity_verified"] is True
    assert f["entity_verification_source"] == s4.CCD_SOURCE_LABEL
    assert f["in_main_analysis"] is True  # not demoted
    assert summary["entities_confirmed"] == 1
    assert summary["entities_flagged"] == 0


def test_pass_d_does_not_demote_non_role_claims(monkeypatch):
    # A named entity in a non-role dimension is recorded but never demoted.
    fact = {
        "datapoint_id": "dp_test_academic_need_001",
        "dimension": "academic_need",
        "fact_key": "district_proficiency_ela_pct",
        "claim": "Rio Grande Academy reports low proficiency",
        "confidence": "HIGH",
        "verification_status": "VERIFIED",
        "in_main_analysis": True,
    }
    monkeypatch.setattr(s4, "fetch_charter_roster", lambda **kw: _roster("SOMEWHERE ELSE"))
    facts, summary = s4._pass_d_entity_verification([fact], "nm-test", "NM", None)
    f = facts[0]
    assert f["in_main_analysis"] is True
    assert f["entity_verified"] is False
    assert summary["entities_flagged"] == 0


def test_pass_d_skips_when_roster_unavailable(monkeypatch):
    monkeypatch.setattr(s4, "fetch_charter_roster", lambda **kw: None)
    original = _kipp_fact()
    facts, summary = s4._pass_d_entity_verification([dict(original)], "nm-test", "NM", None)
    assert facts[0] == original  # untouched
    assert summary["roster_fetched"] is False
    assert summary["entities_flagged"] == 0


def test_pass_d_ignores_already_excluded_facts(monkeypatch):
    fact = _kipp_fact()
    fact["in_main_analysis"] = False  # already excluded by Pass C
    monkeypatch.setattr(s4, "fetch_charter_roster", lambda **kw: _roster("EAST MOUNTAIN"))
    facts, summary = s4._pass_d_entity_verification([fact], "nm-test", "NM", None)
    assert "entity_verified" not in facts[0]
    assert summary["facts_checked"] == 0


def test_entity_verification_summary_shape(monkeypatch):
    monkeypatch.setattr(s4, "fetch_charter_roster", lambda **kw: _roster("A SCHOOL"))
    _, summary = s4._pass_d_entity_verification([_kipp_fact()], "nm-test", "NM", None)
    for key in ("roster_fetched", "roster_size", "facts_checked",
                "entities_confirmed", "entities_flagged", "roster_source"):
        assert key in summary
    assert summary["roster_source"] == "NCES CCD via Urban Institute API"


# ── Pass C null-URL demotion (Session 23 / tightened Session 25) ─────────────
def test_pass_c_demotes_verified_with_null_url():
    # Untrusted-but-not-blocked source class (MEDIA) + null URL → demoted by Pass C.
    # MEDIA is not in BLOCKED_SOURCE_CLASSES so in_main_analysis starts True; not in
    # TRUSTED_SOURCE_CLASSES so the null-URL condition fires and demotes it.
    f = {"verification_status": "VERIFIED", "source_url": None, "in_main_analysis": True,
         "confidence": "HIGH", "source_class": "MEDIA"}
    out = s4._enforce_main_analysis_rules(f)
    assert out["in_main_analysis"] is False
    assert out["verification_status"] == "PROVISIONAL"
    assert "no source URL" in out["needs_verification_reason"]


def test_pass_c_demotes_verified_with_empty_url():
    # Same as above but with an empty string instead of None.
    f = {"verification_status": "VERIFIED", "source_url": "", "in_main_analysis": True,
         "confidence": "HIGH", "source_class": "MEDIA"}
    out = s4._enforce_main_analysis_rules(f)
    assert out["in_main_analysis"] is False
    assert out["verification_status"] == "PROVISIONAL"


def test_pass_c_exempts_trusted_source_class_null_url():
    # PED_DATA is a trusted source class: null URL must NOT trigger demotion.
    f = {"verification_status": "VERIFIED", "source_url": None, "in_main_analysis": True,
         "confidence": "HIGH", "source_class": "PED_DATA"}
    out = s4._enforce_main_analysis_rules(f)
    assert out["in_main_analysis"] is True
    assert out["verification_status"] == "VERIFIED"


def test_pass_c_demotes_untrusted_class_null_url():
    # MEDIA is untrusted (not in TRUSTED_SOURCE_CLASSES) and not in BLOCKED_SOURCE_CLASSES,
    # so in_main_analysis stays True long enough for the null-URL check to fire.
    f = {"verification_status": "VERIFIED", "source_url": None, "in_main_analysis": True,
         "confidence": "HIGH", "source_class": "MEDIA"}
    out = s4._enforce_main_analysis_rules(f)
    assert out["in_main_analysis"] is False
    assert out["verification_status"] == "PROVISIONAL"


def test_pass_c_demotes_missing_source_class_null_url():
    # Unknown provenance (no source_class) + null URL → demoted; trust is not assumed.
    f = {"verification_status": "VERIFIED", "source_url": None, "in_main_analysis": True,
         "confidence": "HIGH"}
    out = s4._enforce_main_analysis_rules(f)
    assert out["in_main_analysis"] is False
    assert out["verification_status"] == "PROVISIONAL"


def test_pass_c_keeps_verified_with_url():
    # Regression guard: a VERIFIED fact WITH a source URL must NOT be demoted.
    f = {"verification_status": "VERIFIED", "source_url": "https://ped.example/data",
         "in_main_analysis": True, "confidence": "HIGH", "source_class": "PED_DATA"}
    out = s4._enforce_main_analysis_rules(f)
    assert out["in_main_analysis"] is True
    assert out["verification_status"] == "VERIFIED"
    assert not out.get("needs_verification_reason")


# ── Statewide PEC-roster expansion (Session 23) ──────────────────────────────
def test_statewide_city_filter_keeps_only_city_charters(monkeypatch):
    import pipeline.ccd_entity_verifier as cev
    rows = [
        {"charter": 1, "school_name": "EXPLORE ACADEMY", "ncessch": "P1",
         "leaid": "3500167", "city_location": "ALBUQUERQUE", "enrollment": 500},
        {"charter": 1, "school_name": "FAR AWAY CHARTER", "ncessch": "F1",
         "leaid": "3502250", "city_location": "ROSWELL", "enrollment": 100},
        {"charter": 0, "school_name": "NOT A CHARTER", "ncessch": "N1",
         "city_location": "ALBUQUERQUE"},
    ]
    monkeypatch.setattr(cev, "_fetch_pages", lambda url: rows)
    out = cev._fetch_statewide_city_charters("Albuquerque", 35, 2023)
    assert {o["name"] for o in out} == {"EXPLORE ACADEMY"}


def test_statewide_city_filter_graceful_on_failure(monkeypatch):
    import pipeline.ccd_entity_verifier as cev

    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(cev, "_fetch_pages", boom)
    assert cev._fetch_statewide_city_charters("Albuquerque", 35, 2023) == []


def test_expanded_roster_unions_district_and_statewide(monkeypatch, tmp_path):
    """The LEAID roster misses a PEC charter; the statewide-city pull adds it."""
    import pipeline.ccd_entity_verifier as cev
    # District (Option B) roster returns only the APS-LEAID school.
    monkeypatch.setattr(cev, "_fetch_rows", lambda leaid, state_abbr, city: (
        [{"charter": 1, "school_name": "EAST MOUNTAIN HIGH SCHOOL", "ncessch": "D1",
          "leaid": "3500060", "lea_name": "ALBUQUERQUE PUBLIC SCHOOLS"}],
        2023, "leaid",
    ))
    # Statewide-by-city returns the PEC school (plus a duplicate of the district one).
    monkeypatch.setattr(cev, "_fetch_statewide_city_charters", lambda city, state_fips, year: [
        {"name": "EXPLORE ACADEMY", "ncessch": "P1", "leaid": "3500167", "enrollment": 500},
        {"name": "EAST MOUNTAIN HIGH SCHOOL", "ncessch": "D1", "leaid": "3500060", "enrollment": 402},
    ])
    monkeypatch.setattr(cev, "_roster_cache_path", lambda s, c: str(tmp_path / "roster.json"))

    roster = cev.fetch_charter_roster(
        city="Albuquerque", state_abbr="NM", cid="nm-albuquerque",
        leaid="3500060", state_fips=35, force_refresh=True,
    )
    names = [s["name"] for s in roster["charter_schools"]]
    assert "EXPLORE ACADEMY" in names              # PEC school now captured
    assert "EAST MOUNTAIN HIGH SCHOOL" in names
    assert names.count("EAST MOUNTAIN HIGH SCHOOL") == 1  # deduped by ncessch
    assert roster["lookup_modes"] == ["leaid", "statewide_city"]
    assert roster["state_fips"] == 35


# ── _run_consistency_checks (Session 10) ─────────────────────────────────────

def _consent_barrier():
    return [{"id": "test_consent", "severity": "consent_required"}]


def _prohibition_barrier():
    return [{"id": "test_prohibition", "severity": "prohibition"}]


def _ctx(barriers, community_id="ms-test", state="MS"):
    return {"statutory_barriers": barriers, "community_id": community_id, "state": state, "s4_config": {}}


# CHECK-001: num_accessible_authorizers=0 corrected to 1 when consent_required barrier exists
def test_check_001_corrects_zero_authorizers():
    facts = [{"fact_key": "num_accessible_authorizers", "value": 0, "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0]["value"] == 1


def test_check_001_passthrough_when_authorizers_nonzero():
    facts = [{"fact_key": "num_accessible_authorizers", "value": 2, "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0]["value"] == 2


def test_check_001_no_fire_without_consent_barrier():
    facts = [{"fact_key": "num_accessible_authorizers", "value": 0, "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx([]))  # no barriers
    assert out[0]["value"] == 0  # uncorrected — no barrier present


# CHECK-002 primary: PCI claim contains ineligibility language + UNVERIFIED status
def test_check_002_primary_demotes_pci_on_ineligibility_claim():
    facts = [{"fact_key": "political_climate_index", "value": 3,
              "claim": "District ineligible for charter authorization",
              "verification_status": "UNVERIFIED", "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0]["in_main_analysis"] is False
    assert "CHECK-002" in (out[0].get("needs_verification_reason") or "")


def test_check_002_primary_no_fire_when_verified():
    # VERIFIED PCI with ineligibility language in claim — primary trigger requires UNVERIFIED
    facts = [{"fact_key": "political_climate_index", "value": 3,
              "claim": "District ineligible for charter authorization",
              "verification_status": "VERIFIED", "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0]["in_main_analysis"] is True  # VERIFIED PCI not demoted by primary path


# CHECK-002 secondary: adjacent auth fact contains ineligibility + PCI is PROVISIONAL
def test_check_002_secondary_demotes_pci_via_adjacent_claim():
    facts = [
        {"fact_key": "num_accessible_authorizers", "value": 0,
         "claim": "Zero accessible authorizers (district ineligible)",
         "verification_status": "VERIFIED", "in_main_analysis": True},
        {"fact_key": "political_climate_index", "value": 4,
         "claim": "Political climate: MIXED_LEAN_HOSTILE (4/9)",
         "verification_status": "PROVISIONAL", "in_main_analysis": True},
    ]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    pci = next(f for f in out if f["fact_key"] == "political_climate_index")
    assert pci["in_main_analysis"] is False
    assert "CHECK-002" in (pci.get("needs_verification_reason") or "")


def test_check_002_secondary_no_fire_when_no_adjacent_ineligibility():
    # Adjacent claim has no ineligibility language → secondary trigger does not fire
    facts = [
        {"fact_key": "num_accessible_authorizers", "value": 1,
         "claim": "One authorizer available (consent required)",
         "verification_status": "VERIFIED", "in_main_analysis": True},
        {"fact_key": "political_climate_index", "value": 4,
         "claim": "Political climate: MIXED_LEAN_HOSTILE (4/9)",
         "verification_status": "PROVISIONAL", "in_main_analysis": True},
    ]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    pci = next(f for f in out if f["fact_key"] == "political_climate_index")
    assert pci["in_main_analysis"] is True  # no ineligibility signal, not demoted


# CHECK-003: school_board_orientation claims restriction when not prohibited
def test_check_003_demotes_orientation_when_not_prohibited():
    facts = [{"fact_key": "school_board_charter_orientation",
              "value": "District not D/F rated; charters legally restricted",
              "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0]["in_main_analysis"] is False
    assert "CHECK-003" in (out[0].get("needs_verification_reason") or "")


def test_check_003_passthrough_when_prohibited():
    # When a prohibition barrier exists, the restriction claim is factually accurate — no demotion
    facts = [{"fact_key": "school_board_charter_orientation",
              "value": "District ineligible — charters prohibited",
              "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_prohibition_barrier()))
    assert out[0]["in_main_analysis"] is True


def test_check_003_passthrough_neutral_orientation():
    facts = [{"fact_key": "school_board_charter_orientation",
              "value": "Board generally supportive",
              "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0]["in_main_analysis"] is True


# Option B no-op: trust hierarchy disabled by default
def test_trust_hierarchy_is_noop_when_disabled():
    facts = [{"fact_key": "some_fact", "value": "x", "in_main_analysis": True}]
    out = s4._run_trust_hierarchy_check(facts, {}, {"s4_trust_hierarchy_enabled": False})
    assert out == facts


def test_trust_hierarchy_raises_when_enabled():
    import pytest
    facts = [{"fact_key": "some_fact", "value": "x"}]
    with pytest.raises(NotImplementedError):
        s4._run_trust_hierarchy_check(facts, {}, {"s4_trust_hierarchy_enabled": True})


# ─────────────────────────────────────────────────────────────────────────────
# Consistency-check flag tagging (Steps 2 + 3 feature)
# ─────────────────────────────────────────────────────────────────────────────

def test_check_001_sets_corrected_flag():
    """CHECK-001 must tag corrected_by_consistency_check=True and the check ID/reason
    on the fact it fixes (num_accessible_authorizers 0 → 1)."""
    facts = [{"fact_key": "num_accessible_authorizers", "value": 0, "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    f = out[0]
    assert f["value"] == 1
    assert f.get("corrected_by_consistency_check") is True
    assert f.get("consistency_check_id") == "CHECK-001"
    assert f.get("consistency_check_reason") is not None


def test_check_001_does_not_tag_when_value_already_nonzero():
    """No tagging when num_accessible_authorizers is already ≥1."""
    facts = [{"fact_key": "num_accessible_authorizers", "value": 2, "in_main_analysis": True}]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0].get("corrected_by_consistency_check") is not True


def test_check_002_sets_demoted_flag():
    """CHECK-002 must tag demoted_by_consistency_check=True and CHECK-002 id on the
    political_climate_index fact it demotes to in_main_analysis=False."""
    facts = [{
        "fact_key": "political_climate_index",
        "value": 3.0,
        "in_main_analysis": True,
        "claim": "District only eligible for D/F students; climate hostile.",
        "verification_status": "UNVERIFIED",
    }]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    f = out[0]
    assert f["in_main_analysis"] is False
    assert f.get("demoted_by_consistency_check") is True
    assert f.get("consistency_check_id") == "CHECK-002"
    assert f.get("consistency_check_reason") is not None


def test_check_002_does_not_set_demoted_flag_on_clean_fact():
    """A PCI fact with no ineligibility language must NOT have the demotion flag."""
    facts = [{
        "fact_key": "political_climate_index",
        "value": 7.0,
        "in_main_analysis": True,
        "claim": "School board broadly supportive of charter growth.",
        "verification_status": "VERIFIED",
    }]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    assert out[0].get("demoted_by_consistency_check") is not True


def test_check_003_sets_demoted_flag():
    """CHECK-003 must tag demoted_by_consistency_check=True on school_board_charter_orientation
    when it claims restriction/ineligibility but no prohibition barrier exists."""
    facts = [{
        "fact_key": "school_board_charter_orientation",
        "value": "ineligible",
        "in_main_analysis": True,
    }]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    f = out[0]
    assert f["in_main_analysis"] is False
    assert f.get("demoted_by_consistency_check") is True
    assert f.get("consistency_check_id") == "CHECK-003"
    assert f.get("consistency_check_reason") is not None


def test_non_check_facts_do_not_have_demotion_flag():
    """Facts untouched by consistency checks must not have demoted_by_consistency_check."""
    facts = [
        {"fact_key": "k12_enrollment_total", "value": 5000, "in_main_analysis": True},
        {"fact_key": "district_proficiency_ela_pct", "value": 42.0, "in_main_analysis": True},
    ]
    out = s4._run_consistency_checks(facts, _ctx(_consent_barrier()))
    for f in out:
        assert not f.get("demoted_by_consistency_check")
        assert not f.get("corrected_by_consistency_check")
