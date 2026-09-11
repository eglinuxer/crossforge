"""Bound source SDK solves with ephemeral, digest-pinned OCI handoffs.

These snapshots belong to one local or read-only CI invocation. They are not
component receipts, registry publications, or release qualification evidence.
"""

import copy
from pathlib import Path
import shutil
import tempfile
import time

from . import bake_materials, component_build, oci_layout, python_sdk
from .identity import content_sha256, digest_value, file_record, load_json, require


def plan(source, graph, roots):
    require(roots and len(roots) == len(set(roots)) and
            not set(roots) - {"python-dev", "sdk-complete-dev"},
            "local SDK handoff requires canonical SDK roots")
    final = "sdk-complete-dev" if "sdk-complete-dev" in roots else "python-dev"
    rows = python_sdk.validate_graph(source, graph, final)
    steps = ["python-dev-append-" + row for row in rows] + ["python-dev"]
    if final == "sdk-complete-dev":
        steps.append(final)
    return {"roots": sorted(roots), "rows": rows, "steps": steps,
            "checkpoints": steps[:-1]}


def snapshot(source, graph, final):
    closure = bake_materials.source_closure(
        source, graph, final, {"mode": "local-sdk-handoff"})
    return content_sha256({"graph": graph, "source_closure": closure,
        "bake_files": [file_record(source, name) for name in
                       ("docker-bake.hcl", "docker-bake.override.json")]})


def solve_graph(graph, cache, target, previous, layout):
    selected = copy.deepcopy(graph)
    selected.pop("group", None)
    replacements = 0
    for name, definition in selected["target"].items():
        definition["output"] = [{"type": "cacheonly"}]
        definition["tags"] = []
        definition["cache-from"] = copy.deepcopy(cache["target"][name]["cache-from"])
        definition["cache-to"] = copy.deepcopy(cache["target"][name]["cache-to"] if name == target else [])
        for key, value in definition.get("contexts", {}).items():
            if previous and value == "target:" + previous["target"]:
                definition["contexts"][key] = previous["reference"]
                replacements += 1
    require(not previous or replacements, "SDK checkpoint has no canonical consumer")
    if layout is not None:
        selected["target"][target]["output"] = [
            {"type": "oci", "dest": str(layout), "tar": False}]
    return selected


def _remove_layout(layout, data):
    require(layout.parent == data and not layout.is_symlink() and layout.is_dir(),
            "SDK cleanup requires an owned checkpoint directory")
    shutil.rmtree(str(layout))


def execute(source, graph, roots, cache, directory, solve):
    """SOLVE runs one bounded Bake command and preserves its exact exit code."""
    directory = Path(directory).resolve()
    evidence = directory / "local-sdk"
    evidence.mkdir()
    selection = plan(source, graph, roots)
    initial = snapshot(source, graph, selection["steps"][-1])
    component_build.write_json(evidence / "plan.json", dict(selection,
        source_snapshot_sha256=initial, qualification_receipt=False))
    # OCI images stay outside uploaded diagnostics. Keep at most the previous
    # and current cumulative image; retain failed transfers for local diagnosis.
    data = Path(tempfile.mkdtemp(prefix="crossforge-sdk-", dir=str(directory.parent)))
    previous, records, status = None, [], 1
    try:
        for target in selection["steps"]:
            require(snapshot(source, graph, selection["steps"][-1]) == initial,
                    "SDK source inputs changed during local handoff")
            layout = data / target if target in selection["checkpoints"] else None
            selected = solve_graph(graph, cache, target, previous, layout)
            recipe = evidence / (target + ".bake.json")
            metadata = directory / ("metadata-" + target + ".json")
            component_build.write_json(recipe, selected)
            started = time.monotonic()
            status = solve(target, recipe, metadata, data)
            record = {"target": target, "exit_code": status,
                      "seconds": round(time.monotonic() - started, 3),
                      "graph_sha256": content_sha256(selected),
                      "input": previous["reference"] if previous else None}
            records.append(record)
            if status:
                return status
            status = 1
            require(snapshot(source, graph, selection["steps"][-1]) == initial,
                    "SDK source inputs changed during local handoff")
            observed = load_json(metadata)
            require(type(observed) is dict and type(observed.get(target)) is dict and
                    observed[target].get("buildx.build.ref"), "SDK build metadata is missing")
            next_checkpoint = None
            if layout is not None:
                require(not layout.is_symlink() and layout.is_dir(),
                        "SDK OCI export is not a directory")
                digest = digest_value(observed[target].get("containerimage.digest"),
                                      "SDK exported image digest", oci=True)
                verified = oci_layout.inspect(layout, digest)
                record["oci"] = verified
                next_checkpoint = {"target": target, "layout": layout,
                    "reference": "oci-layout://%s@%s" % (layout, verified["platform_digest"])}
            component_build.write_json(evidence / (target + ".json"), record)
            if previous:
                _remove_layout(previous["layout"], data)
            previous = next_checkpoint
            status = 0
        status = 1
        data.rmdir()
        status = 0
        return 0
    finally:
        component_build.write_json(evidence / "result.json", {
            "kind": "crossforge-local-sdk-handoff-observation", "schema_version": 1,
            "exit_code": status, "source_snapshot_sha256": initial,
            "roots": selection["roots"], "steps": records,
            "data_directory": str(data), "qualification_receipt": False})
