from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .models import BuildStage

STAGE_ORDER: tuple[BuildStage, ...] = (
    BuildStage.PLAN,
    BuildStage.APPLY_EDITS,
    BuildStage.MANIFEST,
    BuildStage.RENDER,
    BuildStage.ASSEMBLE,
    BuildStage.MASTER,
    BuildStage.PACKAGE,
)

STAGE_DEPENDENCIES: dict[BuildStage, tuple[BuildStage, ...]] = {
    BuildStage.PLAN: (),
    BuildStage.APPLY_EDITS: (BuildStage.PLAN,),
    BuildStage.MANIFEST: (BuildStage.APPLY_EDITS,),
    BuildStage.RENDER: (BuildStage.MANIFEST,),
    BuildStage.ASSEMBLE: (BuildStage.RENDER,),
    BuildStage.MASTER: (BuildStage.ASSEMBLE,),
    BuildStage.PACKAGE: (BuildStage.MASTER,),
}


def stage_index(stage: BuildStage) -> int:
    return STAGE_ORDER.index(stage)


def validate_stage_graph() -> None:
    seen: set[BuildStage] = set()
    for stage in STAGE_ORDER:
        if stage in seen:
            raise ValueError(f"duplicate stage in graph: {stage.value}")
        seen.add(stage)
        for dependency in STAGE_DEPENDENCIES[stage]:
            if dependency not in seen:
                raise ValueError(f"dependency order violation for {stage.value}: {dependency.value}")


def target_to_stage(target: str | BuildStage) -> BuildStage:
    if isinstance(target, BuildStage):
        return target
    return BuildStage(str(target))


def requested_stages(target: BuildStage) -> tuple[BuildStage, ...]:
    return tuple(stage for stage in STAGE_ORDER if stage_index(stage) <= stage_index(target))


def dependency_chain(stage: BuildStage) -> tuple[BuildStage, ...]:
    return STAGE_DEPENDENCIES[stage]


def is_stage_requested(stage: BuildStage, target: BuildStage) -> bool:
    return stage_index(stage) <= stage_index(target)


def downstream_stages(stage: BuildStage, target: BuildStage) -> tuple[BuildStage, ...]:
    return tuple(item for item in STAGE_ORDER if stage_index(stage) < stage_index(item) <= stage_index(target))


def topo_requested_stages(target: BuildStage) -> tuple[BuildStage, ...]:
    validate_stage_graph()
    return requested_stages(target)


def breadth_first_dependencies(stage: BuildStage) -> tuple[BuildStage, ...]:
    validate_stage_graph()
    order: list[BuildStage] = []
    queue: deque[BuildStage] = deque(STAGE_DEPENDENCIES[stage])
    seen: set[BuildStage] = set()
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        queue.extendleft(reversed(STAGE_DEPENDENCIES[current]))
    return tuple(order)
