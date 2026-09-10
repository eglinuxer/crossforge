"""Validate selected CI jobs and reject incomplete dynamic workflow results."""

import json

from . import incremental_plan
from .identity import IdentityError, exact_fields, parse_json, require


GROUPS = {
    "inputs": ["inputs"],
    "toolchains": ["toolchain-x86_64", "toolchain-aarch64"],
    "python": ["python-" + row for row in ("cp39", "cp310", "cp311", "cp312", "cp313", "cp314")],
    "vcpkg": ["vcpkg"], "sdk": ["sdk"], "gcc": ["gcc-smoke", "gcc-full"],
}


def output_values(selection, stages):
    incremental_plan.validate_selection(selection, stages)
    allowed = {stage for members in GROUPS.values() for stage in members}
    require(not set(selection["targets"]) - allowed, "incremental CI cannot select a manual-only stage")
    if selection["mode"] == "full":
        require(set(selection["targets"]) == allowed, "full CI selection omits a required stage")
        require(selection["targets"] == {stage: sorted(stages[stage]) for stage in allowed},
                "full CI execution must include every canonical root")
    result = {"selection": json.dumps(selection, sort_keys=True, separators=(",", ":"))}
    for job, members in GROUPS.items():
        selected = [member for member in members if member in selection["targets"]]
        result[job] = "true" if selected else "false"
        # GitHub evaluates job if before matrix expansion. A valid inert matrix
        # also prevents an empty matrix from becoming a workflow syntax error.
        result[job + "-matrix"] = json.dumps(selected or members[:1], separators=(",", ":"))
    return result


def prepare(selection_text, profile, stages):
    require(profile in ("none", "sdk", "python", "toolchain", "full"), "unsupported incremental CI profile")
    if selection_text:
        selection = incremental_plan.validate_selection(parse_json(selection_text), stages)
        if selection["mode"] == "full":
            allowed = {stage for members in GROUPS.values() for stage in members}
            require(set(selection["targets"]) == allowed, "full CI selection omits a required stage")
            # A full fallback always expands the canonical catalog. A source
            # snapshot may name resolved evidence roots instead of Bake groups;
            # neither form can accidentally narrow a full run to a subset.
            selection["targets"] = {stage: sorted(stages[stage]) for stage in allowed}
        return output_values(selection, stages)
    selected = {}
    for job, members in GROUPS.items():
        if profile == "none" or (job == "gcc" and profile in ("sdk", "python")) or (
                job in ("python", "vcpkg", "sdk") and profile == "toolchain"):
            continue
        selected.update({member: sorted(stages[member]) for member in members})
    selection = {"schema_version": 1, "kind": "crossforge-ci-source-plan",
                 "mode": "full" if profile == "full" else "incremental", "targets": selected}
    return output_values(selection, stages)


def check_results(results, stages):
    try:
        exact_fields(results, ["plan"] + list(GROUPS), "incremental workflow jobs")
        require(results["plan"].get("result") == "success", "incremental job planning failed")
        outputs = results["plan"].get("outputs")
        require(type(outputs) is dict and type(outputs.get("selection")) is str, "missing executable selection")
        expected = output_values(parse_json(outputs["selection"]), stages)
        require(outputs == expected, "job outputs differ from selected work")
        for job in GROUPS:
            require(results[job].get("result") == ("success" if expected[job] == "true" else "skipped"),
                    "selected job did not succeed or unselected job executed: %s" % job)
        return True
    except (IdentityError, AttributeError, KeyError, TypeError):
        return False
