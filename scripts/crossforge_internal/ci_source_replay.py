"""Force selected compiler stages from locked source inputs, with RUN evidence.

Upstream acquisition and unselected compilers may still use ordinary caches.
This is a source-build observation, not a reusable qualification receipt.
"""

from . import bake_materials, ci_replay
from .ci_replay import ROWS, fresh_vertices
from .identity import require


STAGES = ("toolchain-x86_64", "toolchain-aarch64") + tuple("python-" + row for row in ROWS)


def policy(stage):
    require(stage in STAGES, "source replay supports one toolchain or Python row")
    if stage.startswith("toolchain-"):
        arch = stage[len("toolchain-"):]
        return stage + "-dev", {stage + "-build-export": {
            "binutils-" + arch: "build-binutils.sh", "gcc-" + arch + "-built": "build-gcc.sh"}}
    row = stage[len("python-"):]
    return stage + "-dev", {
        "cpython-build-" + row: {"cpython-build": "build-cpython-native.sh"},
        "cpython-cross-" + row + "-x86_64": {"cpython-cross": "build-cpython-cross.sh"},
        "cpython-cross-" + row + "-aarch64": {"cpython-cross": "build-cpython-cross.sh"}}


def plan(source, graph, stage, roots, execution):
    root, requested = policy(stage)
    require(roots == [root], "source replay requires the complete canonical stage root")
    closure = bake_materials.source_closure(source, graph, root, execution)
    recipes = closure["parameters"]["recipes"]
    owners = {}
    for target, stages in sorted(requested.items()):
        require(target in recipes, "source replay is missing the original compiler producer: " + target)
        for name, script in sorted(stages.items()):
            instructions = recipes[target]["stages"].get(name, [])
            runs = [line for line in instructions if line.startswith("RUN ")]
            require(sum("/work/scripts/" + script in line for line in runs) == 1,
                    "source replay requires the canonical compiler RUN: " + target + "/" + name)
            owners.setdefault(target, {})[name] = {"runs": len(runs), "marker": None}
    return {"schema_version": 1, "kind": "crossforge-ci-source-replay-plan", "stage": stage,
            "solves": {root: {"owners": owners, "source": closure}}}


def override(graph, solve):
    value = ci_replay.override(graph, solve)
    for target in value["target"].values():
        target.update({"tags": [], "output": [{"type": "cacheonly"}], "cache-to": []})
    return value
