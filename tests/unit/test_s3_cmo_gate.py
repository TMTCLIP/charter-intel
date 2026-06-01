"""
Unit tests for the S3 CMO local-presence skepticism gate (Session 24).

All offline: no API calls, no cache reads. The gate reads its CMO list from
config/known_cmos.yaml; tests exercise both the real config and a mocked one.
"""
import textwrap

import pipeline.s3_fact_extraction as s3


def _fact(claim, dimension="replication_feasibility", confidence="HIGH",
          status="VERIFIED", dp_id="dp_test_001"):
    return {
        "datapoint_id": dp_id,
        "dimension": dimension,
        "fact_key": "existing_operator_footprint",
        "claim": claim,
        "confidence": confidence,
        "verification_status": status,
        "in_main_analysis": True,
    }


# (a) CMO name detection
def test_kipp_detected_in_replication_claim():
    facts, summary = s3._enforce_cmo_skepticism(
        [_fact("Proven local operators: KIPP, MAS present")], "Albuquerque", "NM"
    )
    f = facts[0]
    assert f["confidence"] == "MODERATE"
    assert f["verification_status"] == "PROVISIONAL"
    assert f.get("cmo_skepticism_applied") is True
    assert summary["facts_demoted"] == 1
    assert summary["cmos_detected"] == ["KIPP"]


# (b) CMO alias detection
def test_alias_detection_knowledge_is_power_program():
    facts, summary = s3._enforce_cmo_skepticism(
        [_fact("Knowledge Is Power Program expanding into the metro area")],
        "Albuquerque", "NM",
    )
    assert facts[0].get("cmo_skepticism_applied") is True
    assert summary["cmos_detected"] == ["KIPP"]


# (c) Non-targeted dimension is ignored
def test_non_targeted_dimension_not_demoted():
    facts, summary = s3._enforce_cmo_skepticism(
        [_fact("KIPP cited in proficiency discussion", dimension="academic_need")],
        "Albuquerque", "NM",
    )
    f = facts[0]
    assert f["confidence"] == "HIGH"           # untouched
    assert f["verification_status"] == "VERIFIED"
    assert "cmo_skepticism_applied" not in f
    assert summary["facts_demoted"] == 0


# (d) Local operator NOT on the national list is not demoted
def test_local_operator_not_demoted():
    facts, summary = s3._enforce_cmo_skepticism(
        [_fact("Explore Academy is the sole local charter operator")],
        "Albuquerque", "NM",
    )
    f = facts[0]
    assert f["confidence"] == "HIGH"
    assert f["verification_status"] == "VERIFIED"
    assert summary["facts_demoted"] == 0
    assert summary["cmos_detected"] == []


# (e) Output fields on a demoted fact
def test_demoted_fact_output_fields():
    facts, _ = s3._enforce_cmo_skepticism(
        [_fact("IDEA Public Schools operating locally")], "Carlsbad", "NM"
    )
    f = facts[0]
    assert f["confidence"] == "MODERATE"
    assert f["verification_status"] == "PROVISIONAL"
    reason = f["needs_verification_reason"]
    assert "IDEA Public Schools" in reason
    assert "Carlsbad" in reason and "NM" in reason
    assert "NCES CCD" in reason


def test_already_cautious_fact_not_redemoted():
    # A CMO-naming fact that is already MODERATE/PROVISIONAL is left untouched
    # (nothing to downgrade) and is not counted as demoted.
    facts, summary = s3._enforce_cmo_skepticism(
        [_fact("KIPP present", confidence="MODERATE", status="PROVISIONAL")],
        "Albuquerque", "NM",
    )
    assert "cmo_skepticism_applied" not in facts[0]
    assert summary["facts_demoted"] == 0


# (f) Config-driven: behavior follows known_cmos.yaml, not hardcoded names
def test_gate_reads_from_config(tmp_path):
    cfg_file = tmp_path / "cmos.yaml"
    cfg_file.write_text(textwrap.dedent("""
        national_cmos:
          - name: Foobar Schools
            aliases: ["Foobar"]
        skepticism_rule:
          applies_to_dimensions:
            - replication_feasibility
          required_output:
            needs_verification_reason: >
              "{cmo_name}" unconfirmed in {city}, {state}.
    """))
    custom = s3._load_known_cmos(str(cfg_file))
    assert [n for n, _ in custom["entries"]] == ["Foobar Schools"]

    # A claim naming the custom CMO is demoted under the custom config...
    facts, summary = s3._enforce_cmo_skepticism(
        [_fact("Foobar Schools operating locally")], "Albuquerque", "NM",
        cmo_config=custom,
    )
    assert facts[0].get("cmo_skepticism_applied") is True
    assert summary["cmos_detected"] == ["Foobar Schools"]

    # ...while KIPP (not in the custom config) is NOT demoted under it.
    facts2, summary2 = s3._enforce_cmo_skepticism(
        [_fact("KIPP operating locally")], "Albuquerque", "NM", cmo_config=custom
    )
    assert summary2["facts_demoted"] == 0
