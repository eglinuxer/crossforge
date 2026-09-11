"""Explain source changes using the same material inventory as component builds.

These snapshots select work. They never authorize consuming an artifact or
reusing a qualification report; those require the separate receipt interfaces.
"""

import re

from . import bake_materials
from .identity import content_sha256, exact_fields, relative_path, require


GENERATORS = {"docker-bake.hcl", "docker-bake.override.json", "config/release.json",
              "scripts/render-release-components.py", "scripts/render-vcpkg-integration.py",
              "scripts/render-bake.py"}
DOCUMENTS = {"README.md", "SUPPORT.md", "SECURITY.md", "AGENTS.md", "LICENSE-MIT", "LICENSE-APACHE"}
CONTROL_WORKFLOWS = {".github/workflows/" + name + ".yml" for name in (
    "candidate", "release", "release-control-plane", "native-aarch64-release", "component-pilot",
    "replay-qualification")}
SDK_CONTROLLERS = {".github/actions/run-component-sdk/action.yml", "scripts/ci-sdk.py"} | {
    "scripts/crossforge_internal/" + name + ".py" for name in
    ("ci_sdk", "python_sdk", "python_sdk_catalog", "python_sdk_recovery")}
PYTHON_INSTALL_CONTROLLERS = {"scripts/crossforge_internal/python_row_install.py"}


def quick_only(path):
    return path in DOCUMENTS or path.startswith(("docs/", "tests/config/"))


def roots(graph, names):
    """Resolve only explicitly requested roots, never all linked dependencies."""
    selected = set()

    def visit(name, active):
        require(type(name) is str and name not in active, "missing or cyclic CI Bake group")
        if name in graph.get("group", {}):
            for child in graph["group"][name]["targets"]:
                visit(child, active | {name})
        else:
            require(name in graph["target"], "missing CI Bake target: %s" % name)
            selected.add(name)

    for name in names:
        visit(name, set())
    return sorted(selected)


def snapshot(source, graph, stages, compiler_targets, execution):
    """Inventory checked, resolved Bake at an immutable source snapshot."""
    memberships = {stage: roots(graph, names) for stage, names in stages.items()}
    compiler_targets = roots(graph, compiler_targets)
    targets = sorted(set(compiler_targets) | {target for values in memberships.values() for target in values})
    nodes = {target: bake_materials.source_closure(source, graph, target, execution) for target in targets}
    return {"schema_version": 1, "kind": "crossforge-ci-source-snapshot", "stages": memberships,
            "compiler_targets": compiler_targets, "nodes": nodes}


def _known_paths(snapshot):
    paths = set()
    for node in snapshot["nodes"].values():
        paths.update(record["path"] for record in node["files"])
        paths.update(recipe["dockerfile"] for recipe in node["parameters"]["recipes"].values())
    return paths


def _difference(before, after):
    old_files = {value["path"]: value for value in before["files"]} if before else {}
    new_files = {value["path"]: value for value in after["files"]}
    return {"before_sha256": content_sha256(before) if before else None,
            "after_sha256": content_sha256(after),
            "files": sorted(path for path in set(old_files) | set(new_files)
                            if old_files.get(path) != new_files.get(path)),
            "parameters_changed": before is None or
                content_sha256(before["parameters"]) != content_sha256(after["parameters"])}


def _plan(targets, changed, reasons, paths, compiler_changes=()):
    return {"schema_version": 1, "kind": "crossforge-ci-source-plan",
            "mode": "full" if reasons else "incremental", "fallback_reasons": reasons,
            "changed_paths": sorted(paths), "targets": {name: sorted(values) for name, values in sorted(targets.items()) if values},
            "compiler_inputs_changed": sorted(compiler_changes), "changes": changed,
            "artifact_reuse": "not-authorized-by-source-selection"}


def full(stages, reason, paths=()):
    require(type(reason) is str and reason, "full selection requires an explanation")
    # Existing full CI covers SDK and GCC. Qt remains explicitly requested.
    return _plan({name: values for name, values in stages.items() if not name.startswith("qt")}, {}, [reason], paths)


def select(before, after, paths):
    """Compare full closures, so downstream invalidation follows actual inputs."""
    require(type(paths) in (list, tuple) and all(type(path) is str for path in paths), "changed paths must be explicit")
    for path in paths:
        relative_path(path)
    paths = sorted(set(paths))
    if before is None:
        return full(after["stages"], "comparison baseline is unavailable", paths)
    require(before["kind"] == after["kind"] == "crossforge-ci-source-snapshot" and
            type(before["schema_version"]) is int and type(after["schema_version"]) is int and
            before["schema_version"] == after["schema_version"] == 1, "unsupported source snapshot")
    if before["stages"] != after["stages"] or before["compiler_targets"] != after["compiler_targets"]:
        return full(after["stages"], "CI root or compiler inventory changed", paths)
    relevant = [path for path in paths if not quick_only(path)]
    if not relevant:
        return _plan({}, {}, [], paths)
    known = _known_paths(before) | _known_paths(after) | GENERATORS | CONTROL_WORKFLOWS | SDK_CONTROLLERS | PYTHON_INSTALL_CONTROLLERS
    unknown = sorted(set(relevant) - known)
    if unknown:
        return full(after["stages"], "unrecognized paths: " + ", ".join(unknown), paths)
    changes = {}
    for target, node in after["nodes"].items():
        previous = before["nodes"].get(target)
        if previous is None or content_sha256(previous) != content_sha256(node):
            changes[target] = _difference(previous, node)
    selected = {stage: [target for target in targets if target in changes]
                for stage, targets in after["stages"].items() if not stage.startswith("qt")}
    controllers = sorted(set(relevant) & SDK_CONTROLLERS)
    if controllers:
        if "sdk" not in after["stages"]:
            return full(after["stages"], "SDK orchestration changed without a canonical SDK stage", paths)
        selected["sdk"] = list(after["stages"]["sdk"])
        for target in selected["sdk"]:
            changes.setdefault(target, _difference(before["nodes"].get(target), after["nodes"][target]))
            changes[target]["orchestration_files"] = controllers
    row_controllers = sorted(set(relevant) & PYTHON_INSTALL_CONTROLLERS)
    if row_controllers:
        stages = [stage for stage in after["stages"] if re.fullmatch(r"python-cp[0-9]+", stage)]
        if not stages:
            return full(after["stages"], "Python installation changed without independent row stages", paths)
        for stage in stages:
            selected[stage] = list(after["stages"][stage])
            for target in selected[stage]:
                changes.setdefault(target, _difference(before["nodes"].get(target), after["nodes"][target]))
                changes[target]["orchestration_files"] = row_controllers
    # Control-plane workflow changes are validated by quick's dedicated tests
    # and workflow lint. They are not compiler inputs. A generated policy or
    # copied implementation change still selects its real Bake consumers above.
    return _plan(selected, changes, [], paths, set(after["compiler_targets"]) & set(changes))


def compact(plan):
    """Bound the job output to the executable selection; keep detail in a file."""
    return {key: plan[key] for key in ("schema_version", "kind", "mode", "targets")}


def validate_selection(value, stages):
    exact_fields(value, ("schema_version", "kind", "mode", "targets"), "CI source selection")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
            value["kind"] == "crossforge-ci-source-plan", "unsupported CI selection schema")
    require(value["mode"] in ("incremental", "full"), "unsupported CI selection mode")
    require(type(value["targets"]) is dict and not set(value["targets"]) - set(stages), "unknown CI selection stage")
    for stage, targets in value["targets"].items():
        require(type(targets) is list and targets and all(type(target) is str for target in targets), "empty or invalid selected targets")
        require(targets == sorted(set(targets)), "selected targets must be unique and sorted")
        for target in targets:
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", target), "invalid selected target")
    return value
