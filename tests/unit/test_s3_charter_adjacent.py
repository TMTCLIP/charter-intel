"""
Unit tests for the S3 charter-adjacent school capture (_inject_charter_adjacent_facts).

Charter-adjacent schools are captured as NON-SCORING context: the helper stores
the cleaned list and emits a num_charter_adjacent_schools fact with
in_main_analysis=False (so it never enters the scored saturation count). All
offline — no API calls.
"""
import pipeline.s3_fact_extraction as s3


def _run(adjacent):
    facts_output: dict = {}
    s3._inject_charter_adjacent_facts(facts_output, adjacent, "ms-jackson", "MS", "Jackson")
    return facts_output


def _adjacent_fact(facts_output):
    return next(
        (f for f in facts_output.get("facts", []) if f["fact_key"] == "num_charter_adjacent_schools"),
        None,
    )


def test_emits_nonscoring_count_fact():
    out = _run([
        {"school_name": "Jackson Innovation Academy", "status": "operating", "confidence": "MODERATE"},
        {"school_name": "Delta Microschool", "status": "pending", "confidence": "LOW"},
    ])
    fact = _adjacent_fact(out)
    assert fact is not None
    assert fact["value"] == 2
    assert fact["in_main_analysis"] is False  # never scored
    assert fact["dimension"] == "charter_saturation"
    assert fact["verification_status"] == "PROVISIONAL"


def test_count_confidence_is_most_conservative():
    out = _run([
        {"school_name": "A", "status": "operating", "confidence": "HIGH"},
        {"school_name": "B", "status": "operating", "confidence": "LOW"},
    ])
    assert _adjacent_fact(out)["confidence"] == "LOW"


def test_empty_list_emits_zero_count():
    out = _run([])
    fact = _adjacent_fact(out)
    assert fact is not None
    assert fact["value"] == 0
    assert out["charter_adjacent_schools"] == []


def test_drops_entries_without_name_never_fabricates():
    out = _run([
        {"school_name": "Real School", "status": "operating", "confidence": "MODERATE"},
        {"school_name": "", "status": "operating"},   # dropped
        {"status": "pending"},                          # dropped (no name)
        "garbage",                                      # dropped (non-dict)
    ])
    assert len(out["charter_adjacent_schools"]) == 1
    assert _adjacent_fact(out)["value"] == 1


def test_non_list_input_is_safe():
    out = _run(None)
    assert out["charter_adjacent_schools"] == []
    assert _adjacent_fact(out)["value"] == 0


def test_idempotent_no_duplicate_fact():
    out = _run([{"school_name": "A", "status": "operating", "confidence": "LOW"}])
    # second call must not append a duplicate count fact
    s3._inject_charter_adjacent_facts(out, [{"school_name": "B", "status": "pending"}],
                                      "ms-jackson", "MS", "Jackson")
    count_facts = [f for f in out["facts"] if f["fact_key"] == "num_charter_adjacent_schools"]
    assert len(count_facts) == 1
