"""Plan explicit CI qualification replays and verify their owning RUN events.

These records are CI observations, not reusable qualification receipts. Build
components are authenticated by the existing CI consumer before execution.
"""

from pathlib import Path
import json
import re
import tempfile

from . import bake_materials, component_qualification, python_qualification
from . import qualification_execution
from .identity import digest_value, parse_json, require


ROWS = ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")
STAGES = ("toolchain-x86_64", "toolchain-aarch64", "gcc-smoke", "gcc-full",
          "vcpkg", "sdk") + tuple("python-" + row for row in ROWS)


def policy(source, stage):
    """Return the exact owning targets/stages; never infer gates by a suffix."""
    require(stage in STAGES, "qualification replay does not support this CI stage")
    if stage.startswith("toolchain-"):
        settings = component_qualification.spec(stage[len("toolchain-"):], "toolchain")
        return [settings["target"]], {settings["target"]: settings["stages"]}
    if stage in ("gcc-smoke", "gcc-full"):
        root = "gcc-testsuite-" + ("smoke-evidence" if stage == "gcc-smoke" else "full-qualification-evidence")
        arches = ("x86_64", "aarch64") if stage == "gcc-smoke" else ("x86_64",)
        return [root], {root: [name for arch in arches
            for name in component_qualification.spec(arch, stage)["stages"]]}
    if stage.startswith("python-"):
        settings = python_qualification.spec(source, stage[len("python-"):])
        replay = {target: [name] for target, name in settings["replay"].items()}
        root = stage + "-dev"
        replay[root] = ["python-sdk-append"]
        return [root], replay
    if stage == "vcpkg":
        targets = ["vcpkg-contract-qualified"] + ["vcpkg-upstream-tier%d-qualified" % tier for tier in (1, 2, 3)]
        return [targets[-1]], {target: [target] for target in targets}
    return ["python-dev", "sdk-complete-dev"], dict(
        {"python-dev-append-" + row: ["python-sdk-append"] for row in ROWS},
        **{"python-dev": ["python-sdk-final"], "sdk-complete-dev": ["sdk-complete-dev"]})


def plan(source, graph, stage, roots, execution):
    canonical_roots, requested = policy(source, stage)
    require(roots == sorted(canonical_roots), "qualification replay requires the complete canonical stage roots")
    pending = {(target, name) for target, names in requested.items() for name in names}
    solves = {}
    for root in roots:
        closure = bake_materials.source_closure(source, graph, root, execution)
        recipes = closure["parameters"]["recipes"]
        owners = {}
        for target, name in sorted(pending):
            if target not in recipes:
                continue
            instructions = recipes[target]["stages"].get(name, [])
            count = sum(line.startswith("RUN ") for line in instructions)
            require(count > 0, "replay stage is missing or has no RUN: " + target + "/" + name)
            definition = graph["target"][target]
            marker = None
            if name == "python-sdk-append":
                row = definition.get("args", {}).get("CPYTHON_ROW")
                require(row in ROWS, "replay append row is not implemented")
                marker = '--row "' + row + '"'
            owners.setdefault(target, {})[name] = {"runs": count, "marker": marker}
        require(owners, "replay solve has no independently selected RUN: " + root)
        pending -= {(target, name) for target, names in owners.items() for name in names}
        solves[root] = {"owners": owners, "source": closure}
    require(not pending, "replay graph omits required qualification stages")
    return {"schema_version": 1, "kind": "crossforge-ci-qualification-replay-plan",
            "stage": stage, "solves": solves}


def override(graph, solve):
    # Reset per solve: SDK append steps execute in the first root and are not
    # forced again when the complete SDK root consumes their completed result.
    return {"target": {target: {"no-cache": False,
        "no-cache-filter": sorted(solve["owners"].get(target, {}))}
        for target in graph["target"]}}


def unprefixed_target(graph, root):
    """Buildx prefixes names only when the actual solve has multiple targets.

    Follow resolved Bake context links after component replacement; stale,
    unreachable producer definitions do not participate in this solve.
    https://github.com/docker/buildx/blob/v0.36.1/build/build.go#L453-L508
    """
    targets, visited = graph.get("target", {}), set()

    def visit(name, active):
        require(name in targets and name not in active, "replay solve has a missing or cyclic Bake target")
        if name in visited:
            return
        visited.add(name)
        contexts = targets[name].get("contexts", {})
        require(type(contexts) is dict, "replay solve contexts must be an object")
        for reference in contexts.values():
            require(type(reference) is str, "replay solve context must be text")
            if reference.startswith("target:"):
                visit(reference[len("target:"):], active | {name})

    visit(root, set())
    return root if visited == {root} else None


def fresh_vertices(path, owners, started, completed, allow_shared=False, single_target=None):
    """Use owning target timestamps, retaining cached/failed aliases as fatal."""
    owned = {(target, stage): [] for target, stages in owners.items() for stage in stages}
    require(single_target is None or set(owners) == {single_target},
            "unprefixed replay events require exactly one graph-derived owner")
    bad = set()
    pattern = re.compile(r"^\[(\S+) (\S+) \d+/\d+\] RUN ")
    unprefixed = re.compile(r"^\[(\S+) \d+/\d+\] RUN ")
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = parse_json(line)
            require(type(event) is dict and type(event.get("vertexes", [])) is list,
                    "replay progress must contain BuildKit raw JSON events")
            for vertex in event.get("vertexes", []):
                require(type(vertex) is dict, "replay vertex must be an object")
                digest = digest_value(vertex.get("digest"), "replay vertex digest", oci=True)
                if vertex.get("cached") or vertex.get("error"):
                    bad.add(digest)
                require(type(vertex.get("name", "")) is str, "replay vertex name must be text")
                match = pattern.match(vertex.get("name", ""))
                if match:
                    target, stage = match[1], match[2]
                    require(single_target is None or target == single_target,
                            "prefixed replay event contradicts the single-target solve")
                else:
                    match = unprefixed.match(vertex.get("name", ""))
                    if single_target is None or not match:
                        continue
                    target, stage = single_target, match[1]
                if (target, stage) not in owned:
                    continue
                marker = owners[target][stage]["marker"]
                if marker is None or marker in vertex["name"]:
                    owned[(target, stage)].append(vertex)
    result = []
    with tempfile.TemporaryDirectory(prefix="crossforge-replay-events-") as temporary:
        filtered = Path(temporary) / "progress.jsonl"
        for (target, stage), vertices in sorted(owned.items()):
            require(not {vertex["digest"] for vertex in vertices} & bad,
                    "replay RUN was cached or failed through a Bake alias: " + target)
            filtered.write_text("".join(json.dumps({"vertexes": [vertex]}) + "\n" for vertex in vertices))
            result.extend(dict(vertex, target=target) for vertex in qualification_execution.fresh_vertices(
                filtered, {stage: owners[target][stage]["runs"]}, started, completed))
    require(result and (allow_shared or len({vertex["digest"] for vertex in result}) == len(result)),
            "replay RUN evidence is empty or belongs to multiple targets")
    return result
