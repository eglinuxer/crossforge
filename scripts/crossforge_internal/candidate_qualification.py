"""Require complete qualification for the exact candidate's component graph.

Build caches remain useful for material preparation. They do not establish a
qualification receipt. Only explicitly authenticated matching rows may retain
their original execution; every remaining owner must supply fresh RUN events.
"""

import copy
from datetime import datetime
import os
from pathlib import Path
import runpy
import subprocess

from . import candidate_rows, ci_replay, component_build, component_inputs, component_qualification, python_qualification, python_sdk
from .identity import content_sha256, digest_value, exact_fields, load_json, require


FILES = ("candidate-qualification.json", "candidate-qualification-inputs.json",
         "candidate-qualification-plan.json", "candidate-execution.jsonl")


def policy(source):
    owners = {}
    for arch in ("x86_64", "aarch64"):
        settings = component_qualification.spec(arch, "toolchain")
        owners[settings["target"]] = settings["stages"]
    for profile, target in (("gcc-smoke", "gcc-testsuite-smoke-evidence"),
                            ("gcc-full", "gcc-testsuite-full-qualification-evidence")):
        arches = ("x86_64", "aarch64") if profile == "gcc-smoke" else ("x86_64",)
        owners[target] = [stage for arch in arches
                          for stage in component_qualification.spec(arch, profile)["stages"]]
    owners["sdk-phase13-base"] = ["vcpkg-sdk-base"]
    for name in ["vcpkg-contract-qualified"] + ["vcpkg-upstream-tier%d-qualified" % tier for tier in (1, 2, 3)]:
        owners[name] = [name]
    for row in python_sdk.matrix(source):
        for target, stage in python_qualification.spec(source, row)["replay"].items():
            owners[target] = [stage]
        owners["python-dev-append-" + row] = ["python-sdk-append"]
    owners["python-dev"] = ["python-sdk-final"]
    owners["packaging-qualified"] = ["crosspack-packages", "crosspack-deb-qualified",
                                      "crosspack-rpm-qualified", "packaging-qualified"]
    # sdk-candidate inherits the complete SDK inside its Dockerfile. The owning
    # Bake target for both stages is therefore sdk-candidate, not sdk-complete-dev.
    owners["sdk-candidate"] = ["sdk-complete-dev", "sdk-candidate"]
    return owners


def plan(source, inputs, reused=None):
    reused = {} if reused is None else reused
    candidate_rows.validate(reused)
    require(set(reused) <= set(python_sdk.matrix(source)), "candidate reuses an unsupported Python row")
    recipes = inputs["parameters"]["recipes"]
    omitted = set()
    for row, value in reused.items():
        component_inputs.verify_files(value["receipt"]["contract"]["inputs"], source)
        require(value["receipt"]["contract"]["inputs"]["parameters"].get("execution") ==
                inputs["parameters"]["execution"], "candidate row execution environment differs")
        omitted.update(python_qualification.spec(source, row)["replay"])
    expected_rows = [candidate_rows.dependency(row, value) for row, value in sorted(reused.items())]
    require([item for item in inputs["dependencies"] if item["component"].startswith("qualification/python-")] == expected_rows,
            "candidate qualified row dependencies differ from checked reuse evidence")
    require(not omitted & set(recipes), "candidate reused row still has qualification producers")
    owners = {}
    for target, stages in policy(source).items():
        if target in omitted:
            continue
        require(target in recipes, "candidate omits a qualification owner: " + target)
        owners[target] = {}
        for stage in stages:
            instructions = recipes[target]["stages"].get(stage, [])
            count = sum(line.startswith("RUN ") for line in instructions)
            require(count > 0, "candidate qualification stage has no RUN: " + target + "/" + stage)
            marker = None
            if stage == "python-sdk-append":
                marker = '--row "' + target[len("python-dev-append-"):] + '"'
            owners[target][stage] = {"runs": count, "marker": marker}
    value = {"schema_version": 1, "kind": "crossforge-candidate-qualification-plan", "owners": owners}
    if reused:
        value.update(schema_version=2, reused_rows={row: item["pin"]["receipt_sha256"] for row, item in sorted(reused.items())})
    return value


def override(graph, selected):
    result = copy.deepcopy(graph)
    require(set(selected["owners"]) <= set(result["target"]), "candidate replay owner is absent")
    for target, definition in result["target"].items():
        definition["no-cache"] = False
        definition["no-cache-filter"] = sorted(selected["owners"].get(target, {}))
    return result


def fresh_vertices(path, selected, started, completed):
    """Verify each owning target; report genuinely shared BuildKit vertices once.

    GCC smoke/full share their prepared test base. Requiring each owner's fresh
    events retains complete logical coverage, while the shared content digest
    avoids claiming that the same physical RUN executed twice. The existing
    verifier still rejects cached/failed aliases and incomplete/stale events.
    """
    version = selected.get("schema_version") if type(selected) is dict else None
    exact_fields(selected, ("schema_version", "kind", "owners") + (("reused_rows",) if version == 2 else ()),
                 "candidate qualification plan")
    require(type(version) is int and version in (1, 2) and
            selected["kind"] == "crossforge-candidate-qualification-plan" and selected["owners"],
            "unsupported or empty candidate qualification plan")
    result = {}
    if version == 2:
        require(type(selected["reused_rows"]) is dict and selected["reused_rows"], "candidate reuse plan is empty")
        for digest in selected["reused_rows"].values():
            digest_value(digest, "candidate reused row receipt SHA256")
    # The GCC log can be large. Parse it once for the whole owner set.
    vertices = ci_replay.fresh_vertices(path, selected["owners"], started, completed, allow_shared=True)
    for vertex in vertices:
        item = {"target": vertex["target"], "stage": vertex["stage"], "name": vertex["name"],
                "started": vertex["started"], "completed": vertex["completed"]}
        digest = vertex["digest"]
        if digest not in result:
            result[digest] = {"digest": digest, "cached": False, "owners": []}
        else:
            prior = result[digest]["owners"][0]
            require(prior["stage"] == item["stage"] and
                    prior["name"].split("] RUN ", 1)[1] == item["name"].split("] RUN ", 1)[1],
                    "shared candidate RUN has different stage or instructions")
        result[digest]["owners"].append(item)
    return [result[digest] for digest in sorted(result)]


def execute(source, binding_path, directory, sha256, builder, output, reference, generator, docker_config=None):
    """Build the source-bound candidate once and retain fresh execution evidence."""
    from . import candidate_components, candidate_publication
    source, directory, output = Path(source), Path(directory), Path(output)
    ready = candidate_components.check(source, binding_path, directory, sha256, builder, docker_config)
    owner = candidate_publication.producer(os.environ, "sdk")
    require(owner["source_commit"] == ready["source_commit"], "candidate execution source differs")
    release = load_json(source / "config/release.json")
    manifest = runpy.run_path(str(source / "scripts/candidate_manifest.py"))
    tag = manifest["candidate_tag"](release, owner["source_commit"], str(owner["run_id"]), str(owner["attempt"]))
    require(reference == release["product"]["image_repository"] + ":" + tag,
            "candidate execution reference differs from its original producer")
    policy = release["sbom"]["generator"]
    require(generator == policy["repository"] + "@" + policy["digest"], "candidate SBOM generator differs")
    require(output.is_dir() and not output.is_symlink() and all(not (output / name).exists() and
            not (output / name).is_symlink() for name in FILES + ("build-metadata.json",)),
            "candidate execution requires new metadata and evidence files")
    component_build.write_json(output / FILES[1], ready["inputs"])
    component_build.write_json(output / FILES[2], ready["qualification"])
    target = candidate_components.TARGET
    command = component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "-f", str(directory / "components.bake.json"), target,
        "--set", target + ".tags=" + reference, "--set", target + ".output=type=image,push=true",
        "--set", target + ".attest=type=provenance,mode=max,version=v1",
        "--set", target + ".attest+=type=sbom,generator=" + generator,
        "--metadata-file", str(output / "build-metadata.json"), "--progress=rawjson"]
    started = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with (output / FILES[3]).open("x", encoding="utf-8") as progress:
        subprocess.run(command, cwd=str(source), stderr=progress, check=True)
    completed = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    candidate_components.check(source, binding_path, directory, sha256, builder, docker_config)
    vertices = fresh_vertices(output / FILES[3], ready["qualification"], started, completed)
    image = runpy.run_path(str(source / "scripts/resolve_candidate_image.py"))
    digest = image["buildx_digest"](load_json(output / "build-metadata.json"), target)
    result = {"schema_version": 1, "kind": "crossforge-candidate-qualification-execution", "status": "passed",
        "producer": owner, "image_digest": digest, "inputs_sha256": component_inputs.identity(ready["inputs"]),
        "source_binding_sha256": ready["source_binding_sha256"],
        "component_selection_sha256": content_sha256(ready["selection"]),
        "execution": ready["qualification_execution"], "started_at": started, "completed_at": completed,
        "vertices": vertices}
    if ready["qualification"]["schema_version"] == 2:
        result.update(schema_version=2, reused_rows=copy.deepcopy(ready["qualification"]["reused_rows"]))
    component_build.write_json(output / FILES[0], result)
    return result


def verify(source, directory, owner, image_digest, selection, source_binding):
    """Recheck the immutable execution payload during sealing and recovery."""
    directory = Path(directory)
    for name in FILES:
        require((directory / name).is_file() and not (directory / name).is_symlink(), "candidate execution evidence is missing")
    result, inputs, selected = [load_json(directory / name) for name in FILES[:3]]
    version = result.get("schema_version") if type(result) is dict else None
    exact_fields(result, ("schema_version", "kind", "status", "producer", "image_digest", "inputs_sha256",
        "source_binding_sha256", "component_selection_sha256", "execution", "started_at", "completed_at", "vertices") +
        (("reused_rows",) if version == 2 else ()),
        "candidate qualification execution")
    require(type(version) is int and version in (1, 2) and
            result["kind"] == "crossforge-candidate-qualification-execution" and result["status"] == "passed",
            "candidate qualification execution did not pass")
    digest_value(image_digest, "candidate execution image digest", oci=True)
    require(result["producer"] == owner and result["image_digest"] == image_digest and
            result["source_binding_sha256"] == content_sha256(source_binding) and
            result["component_selection_sha256"] == content_sha256(selection),
            "candidate execution producer, image, source or component selection differs")
    component_inputs.verify_files(inputs, source)
    require(inputs["component"] == "qualification/candidate-sdk" and inputs["scope"] == "qualification" and
            inputs["targets"] == ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"] and
            component_inputs.identity(inputs) == result["inputs_sha256"], "candidate qualification inputs differ")
    exact_fields(result["execution"], ("build", "host"), "candidate qualification environment")
    require(result["execution"]["host"] and inputs["parameters"]["execution"] == result["execution"],
            "candidate qualification physical environment differs")
    from . import candidate_components
    candidate_components.validate_selection(selection, owner["source_commit"])
    raw, reused = candidate_components.selection_parts(selection)
    require(bool(reused) == (version == 2), "candidate execution and row selection versions differ")
    candidate_rows.check_inputs(source, result["execution"], reused, raw)
    require(selected == plan(source, inputs, reused), "candidate qualification coverage differs")
    if version == 2:
        require(result["reused_rows"] == selected["reused_rows"], "candidate prior row receipt selection differs")
    require(result["vertices"] == fresh_vertices(directory / FILES[3], selected,
            result["started_at"], result["completed_at"]), "candidate fresh execution evidence differs")
    return result
