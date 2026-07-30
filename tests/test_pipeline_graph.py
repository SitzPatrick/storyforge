from app.pipeline.graph import STAGE_DEPENDENCIES, STAGE_ORDER, dependency_chain, requested_stages, validate_stage_graph
from app.pipeline.models import BuildStage, BuildTarget


def test_pipeline_graph_is_linear_and_valid() -> None:
    validate_stage_graph()
    assert STAGE_ORDER == (
        BuildStage.PLAN,
        BuildStage.APPLY_EDITS,
        BuildStage.MANIFEST,
        BuildStage.RENDER,
        BuildStage.ASSEMBLE,
        BuildStage.MASTER,
        BuildStage.PACKAGE,
    )
    assert dependency_chain(BuildStage.PACKAGE) == (BuildStage.MASTER,)
    assert STAGE_DEPENDENCIES[BuildStage.APPLY_EDITS] == (BuildStage.PLAN,)
    assert requested_stages(BuildStage.ASSEMBLE) == (
        BuildStage.PLAN,
        BuildStage.APPLY_EDITS,
        BuildStage.MANIFEST,
        BuildStage.RENDER,
        BuildStage.ASSEMBLE,
    )
    assert BuildTarget.PACKAGE.value == BuildStage.PACKAGE.value
