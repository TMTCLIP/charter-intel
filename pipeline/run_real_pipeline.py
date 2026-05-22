from pipeline import PipelineConfig, OperatorPreset, OutputMode, StageStatus
from pipeline.s3_fact_extraction import run as run_s3
from pipeline.s4_verification import run as run_s4
from pipeline.s5_scoring import run as run_s5
from pipeline.s6_synthesis import run as run_s6
from pipeline.s7_render import run as run_s7

COMMUNITY_ID = "nm-albuquerque"
STATE = "NM"

config = PipelineConfig(
    state=STATE,
    preset=OperatorPreset.GROWTH,
    mode=OutputMode.STRATEGIC_BRIEF,
    output_format="markdown",
    cache_enabled=True,
    dry_run=False,
    force_refresh=False,
)

# S3
s3 = run_s3(community_id=COMMUNITY_ID, state=STATE, config=config)
if s3.status != StageStatus.SUCCESS:
    print(f"S3 FAILED: {s3.errors}")
    raise SystemExit(1)
s3_facts = s3.output_data.get("facts", []) if s3.output_data else []
print(f"S3 fact count: {len(s3_facts)}")

# S4
s4 = run_s4(community_id=COMMUNITY_ID, state=STATE, config=config, previous_result=s3)
if s4.status != StageStatus.SUCCESS:
    print(f"S4 FAILED: {s4.errors}")
    raise SystemExit(1)
s4_summary = s4.output_data.get("summary", {}) if s4.output_data else {}
print(f"S4 verified fact count: {s4_summary.get('in_main_analysis', '?')}")
print(f"S4 blocked fact count:  {s4_summary.get('routed_to_verification', '?')}")

# S5
s5 = run_s5(community_id=COMMUNITY_ID, state=STATE, config=config, previous_result=s4)
if s5.status != StageStatus.SUCCESS:
    print(f"S5 FAILED: {s5.errors}")
    raise SystemExit(1)
sc = s5.output_data
print(f"\nS5 composite score: {sc['composite_score']}")
print(f"S5 tier:            {sc['tier']} — {sc['tier_display_label']}")
print("\nS5 dimension scores:")
for dim, d in sc["dimensions"].items():
    print(f"  {dim:<30} score={d['score']:.2f}  weight={d['weight']:.2f}  confidence={d['confidence']}")

# S6
print("\nRunning S6 synthesis...")
s6 = run_s6(community_id=COMMUNITY_ID, state=STATE, config=config, previous_result=s5)
if s6.status != StageStatus.SUCCESS:
    print(f"S6 FAILED: {s6.errors}")
    raise SystemExit(1)
if s6.warnings:
    for w in s6.warnings:
        print(f"S6 WARNING: {w}")
brief = s6.output_data
print(f"S6 brief generated: audit_passed={brief.get('audit_passed')}  "
      f"audit_flags={len(brief.get('audit_flags', []))}")
print(f"S6 output: {s6.output_path}")

# S7
print("\nRunning S7 render...")
s7 = run_s7(community_id=COMMUNITY_ID, state=STATE, config=config, previous_result=s6)
if s7.status != StageStatus.SUCCESS:
    print(f"S7 FAILED: {s7.errors}")
    raise SystemExit(1)
if s7.warnings:
    for w in s7.warnings:
        print(f"S7 WARNING: {w}")
print(f"S7 output: {s7.output_path}")
