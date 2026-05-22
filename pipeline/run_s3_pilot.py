from pipeline import PipelineConfig, OperatorPreset, OutputMode, StageStatus
from pipeline.s3_fact_extraction import run

config = PipelineConfig(
    state="NM",
    preset=OperatorPreset.GROWTH,
    mode=OutputMode.SNAPSHOT,
    output_format="markdown",
    cache_enabled=True,
    dry_run=False,
    force_refresh=False,
)

result = run(community_id="nm-albuquerque", state="NM", config=config)

print(f"status: {result.status.value}")
if result.status == StageStatus.SUCCESS:
    fact_count = len(result.output_data) if result.output_data else 0
    print(f"fact count: {fact_count}")
else:
    for err in result.errors:
        print(f"error: {err}")
