import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import s3_extract
import s4_verify
import s5_score


def test_s3_all_facts_have_fact_key():
    bundle = s3_extract.run()
    assert bundle["facts"], "S3 returned no facts"
    for f in bundle["facts"]:
        assert "fact_key" in f, f"Missing fact_key: {f}"
        assert f["fact_key"], f"Empty fact_key: {f}"


def test_s4_does_not_mutate_fact_key():
    s3_bundle = s3_extract.run()
    s3_keys = [f["fact_key"] for f in s3_bundle["facts"]]

    s4_bundle = s4_verify.run(s3_bundle)
    s4_keys = [f["fact_key"] for f in s4_bundle["facts"]]

    assert s3_keys == s4_keys, (
        f"S4 mutated fact_key order or values.\nBefore: {s3_keys}\nAfter:  {s4_keys}"
    )


def test_s4_fact_key_present_on_all_output_facts():
    bundle = s4_verify.run(s3_extract.run())
    for f in bundle["facts"]:
        assert "fact_key" in f, f"S4 dropped fact_key from: {f}"
        assert f["fact_key"], f"S4 cleared fact_key on: {f}"


def test_s4_only_touches_in_main_analysis():
    s3_bundle = s3_extract.run()
    s4_bundle = s4_verify.run(s3_bundle)

    for s3f, s4f in zip(s3_bundle["facts"], s4_bundle["facts"]):
        assert s3f["fact_key"] == s4f["fact_key"]
        for key in ("dimension", "value", "confidence", "source_class"):
            assert s3f[key] == s4f[key], (
                f"S4 mutated '{key}' on {s3f['fact_key']}: {s3f[key]} → {s4f[key]}"
            )


def test_s5_output_shape():
    result = s5_score.run(s4_verify.run(s3_extract.run()))

    assert "composite" in result, "S5 missing composite"
    assert "tier" in result, "S5 missing tier"
    assert "dimensions" in result, "S5 missing dimensions"

    assert isinstance(result["composite"], float), "composite must be float"
    assert isinstance(result["tier"], str), "tier must be str"
    assert isinstance(result["dimensions"], dict), "dimensions must be dict"

    for dim_name, dim in result["dimensions"].items():
        assert "score" in dim, f"dimension '{dim_name}' missing score"
        assert "weight" in dim, f"dimension '{dim_name}' missing weight"
        assert "driver" in dim, f"dimension '{dim_name}' missing driver"


def test_s5_is_deterministic():
    bundle = s4_verify.run(s3_extract.run())
    r1 = s5_score.run(bundle)
    r2 = s5_score.run(bundle)
    assert r1["composite"] == r2["composite"], "S5 is non-deterministic"
    assert r1["tier"] == r2["tier"], "S5 tier is non-deterministic"


def test_blocked_facts_default_in_s5():
    bundle = s4_verify.run(s3_extract.run())
    blocked_keys = {
        f["fact_key"] for f in bundle["facts"] if not f.get("in_main_analysis")
    }
    result = s5_score.run(bundle)

    for dim_name, dim in result["dimensions"].items():
        driver_fact = s5_score.DIMENSIONS[dim_name]["primary"]
        if driver_fact in blocked_keys:
            assert dim["score"] == 5.0, (
                f"Blocked driver '{driver_fact}' in '{dim_name}' should default to 5.0, got {dim['score']}"
            )
            assert "default 5.0" in dim["driver"], (
                f"Driver string should note default for blocked fact in '{dim_name}'"
            )
