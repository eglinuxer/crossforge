"""Fresh Python row qualification and explicit reuse of a sealed row artifact.

The existing row finalizer rechecks installed bytes and reports on both paths.
Receipt digests must come from the caller's independent producer trust boundary.
"""

import copy
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from . import bake_materials, component_artifacts, component_build, component_inputs
from . import oci_layout, python_components, qualification_execution
from .identity import content_sha256, file_record, load_json, require


RECORD_PATH = "component/qualification.json"
PROGRESS_PATH = "component/execution.jsonl"
REPORTS = ("aarch64.json", "row.json", "source.json", "x86_64.json")


def spec(source, row):
    native = python_components.spec(source, row, "build", "install")
    replay = {"python-row-" + row: "cpython-row-assemble"}
    for arch in python_components.ARCHES:
        replay["cpython-%s-%s-qualify-build" % (row, arch)] = "cpython-qualify-build"
        replay["cpython-%s-%s-qualify" % (row, arch)] = "cpython-qualify-" + arch
    return {"component": "qualification/python-" + row, "row": row, "version": native["version"],
            "adapter": native["adapter"], "targets": native["targets"], "target": "python-row-" + row,
            "stage": "cpython-row-export", "replay": replay,
            "copies": ["opt/crossforge/python/" + row, "opt/crossforge/qualification/python/" + row]}


def inspection_identity():
    # Acceptance runs in the orchestration container, outside the build worker.
    readelf = next((path for path in (Path("/opt/rh/gcc-toolset-15/root/usr/bin/readelf"), Path("/usr/bin/readelf"))
                    if path.is_file()), None)
    require(readelf is not None, "Python row inspection needs host readelf")
    binaries = {name: file_record(Path("/"), str(path.resolve()).lstrip("/"))["sha256"]
                for name, path in (("python", Path(sys.executable)), ("readelf", readelf))}
    return {"python": sys.version, "readelf": subprocess.check_output([str(readelf), "--version"]).decode("utf-8"),
            "binaries_sha256": binaries}


def inputs(source, graph, settings, execution, bindings):
    require(type(execution) is dict and set(execution) == {"build", "host"} and execution["host"],
            "Python qualification must bind observed build and host identities")
    require(settings == spec(source, settings["row"]), "Python row qualification settings differ")
    definition = graph.get("target", {}).get(settings["target"], {})
    require(definition.get("target") == settings["stage"] and definition.get("dockerfile") == "docker/python.Dockerfile",
            "Python row must use its canonical Docker stage")
    value = bake_materials.capture(source, graph, settings["target"], settings["component"], "qualification",
                                  settings["targets"], execution, artifacts=bindings)
    wanted = {"python/%s-build-install" % settings["row"]}
    for arch in python_components.ARCHES:
        wanted.add("toolchain/%s-install" % arch)
        wanted.update("python/%s-%s-%s" % (settings["row"], arch, kind) for kind in ("install", "test-context"))
    require({item["component"] for item in value["dependencies"]} == wanted,
            "Python qualification must consume exactly seven verified subjects")
    files = {record["path"]: record for record in value["files"]}
    for path in ("scripts/crossforge_internal/python_qualification.py", "scripts/crossforge_internal/qualification_execution.py"):
        files[path] = file_record(source, path)
    value["files"] = [files[path] for path in sorted(files)]
    require(not {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"} & set(files),
            "Python row qualification unexpectedly includes source compilers")
    runs = {}
    for target, stage in settings["replay"].items():
        recipe = value["parameters"]["recipes"].get(target, {}).get("stages", {}).get(stage, [])
        count = sum(line.startswith("RUN ") for line in recipe)
        require(count > 0, "Python qualification replay stage has no RUN: " + target)
        runs[stage] = runs.get(stage, 0) + count
    value["parameters"].update(qualification=settings, required_runs=runs, inspection=inspection_identity())
    return component_inputs.validate(value)


def metadata_paths():
    return sorted([component_artifacts.CONTRACT_PATH, RECORD_PATH, PROGRESS_PATH] +
                  ["component/reports/" + name for name in REPORTS])


def validate_row(source, settings, root, temporary_parent=None):
    """Use the existing validator against actual files, never just report claims."""
    source, root = Path(source).resolve(), Path(root)
    reports = root / settings["copies"][1]
    for name in REPORTS:
        actual = file_record(root, settings["copies"][1] + "/" + name)
        mirror = file_record(root, "component/reports/" + name)
        require((actual["sha256"], actual["mode"]) == (mirror["sha256"], mirror["mode"]),
                "row report differs from sealed metadata: " + name)
    with tempfile.TemporaryDirectory(prefix="crossforge-python-row-check-", dir=temporary_parent) as temporary:
        work = Path(temporary)
        for arch in python_components.ARCHES:
            paths = {"abi-baseline.json": "abi/el8/" + arch + ".json",
                     "abi-providers.json": "config/abi-providers.json",
                     "abi-sysroot-inventory.json": "evidence/abi/el8-" + arch + "-sysroot.json",
                     "python-runtime-providers.json": "config/python-runtime-providers.json",
                     "python-provider-catalog.json": "evidence/abi/el8-" + arch + "-python-provider-catalog.json"}
            destination = work / "abi" / arch
            destination.mkdir(parents=True)
            for name, path in paths.items():
                shutil.copyfile(str(source / path), str(destination / name))
        output = work / "row.json"
        subprocess.run([sys.executable, str(source / "docker/finalize-python-row.py"),
            "--root", str(root), "--row", settings["row"], "--version", settings["version"],
            "--adapter", settings["adapter"], "--release", str(source / "config/release.json"),
            "--source-manifest", str(reports / "source.json"), "--abi-input-root", str(work / "abi"),
            "--row-manifest", str(reports / "row.json"),
            "--output", str(output)], check=True, stdout=sys.stderr)
        require(output.read_bytes() == (reports / "row.json").read_bytes(),
                "row manifest differs from installed files and current qualification policy")
    row = load_json(reports / "row.json")
    return {"row": settings["row"], "status": "passed", "manifest_sha256": file_record(reports, "row.json")["sha256"],
            "build_python_sdk_tree": row["build_python_sdk_tree"], "targets": row["qualifications"],
            "runtime_tiers": ["locked-sysroot", "clean-rocky"]}


def record(contract, source, root, started_at, completed_at, temporary_parent=None):
    expected = contract["inputs"]
    require(inspection_identity() == expected["parameters"]["inspection"], "Python inspection environment differs")
    return {"schema_version": 1, "kind": "crossforge-component-qualification", "mode": "executed",
            "inputs_sha256": component_inputs.identity(expected), "producer": contract["producer"],
            "started_at": started_at, "completed_at": completed_at,
            "vertices": qualification_execution.fresh_vertices(Path(root) / PROGRESS_PATH,
                expected["parameters"]["required_runs"], started_at, completed_at),
            "coverage": validate_row(source, expected["parameters"]["qualification"], root, temporary_parent)}


def extract_row(layout, observation, settings, frontend, directory, builder, docker_config=None):
    directory = Path(directory).resolve()
    require(not directory.exists(), "row extraction directory must be new")
    destination = directory / "files"
    destination.mkdir(parents=True)
    reference = "oci-layout://%s@%s" % (Path(layout).resolve(), observation["root_digest"])
    paths = settings["copies"] + ["component"]
    lines = ["# syntax=" + frontend, "FROM scratch"]
    lines += ["COPY --from=row " + json.dumps(["/" + path + "/", "/" + path + "/"]) for path in paths]
    graph = {"target": {"row-extract": {"context": ".", "dockerfile-inline": "\n".join(lines) + "\n",
        "contexts": {"row": reference}, "platforms": ["linux/amd64"],
        "output": [{"type": "local", "dest": str(destination)}]}}}
    component_build.write_json(directory / "extract.bake.json", graph)
    subprocess.run(component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder,
        "--allow=fs.write=" + str(destination), "-f", str(directory / "extract.bake.json"),
        "row-extract", "--progress=plain"], cwd=str(directory), check=True)
    return destination


def produce(source, graph, row, execution, producer, subjects, directory, builder, docker_config=None):
    """Execute both targets' gates and the row finalizer, then seal their bytes."""
    source, directory = Path(source).resolve(), Path(directory).resolve()
    require(not directory.exists(), "Python qualification output directory must be new")
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "Python qualification execution environment differs")
    settings = spec(source, row)
    resolved, bindings = python_components.bind_row(source, graph, row, execution["build"], subjects,
        builder, docker_config, directory.parent)
    expected = inputs(source, resolved, settings, execution, bindings)
    contract = component_artifacts.contract("qualification", expected, producer)
    payload = directory / "payload"
    component_build.write_json(payload / component_artifacts.CONTRACT_PATH, contract)
    component_build.write_json(directory / "inputs.json", expected)
    build = copy.deepcopy(resolved)
    build.pop("group", None)
    build["target"] = {name: definition for name, definition in build["target"].items()
                       if name in expected["parameters"]["bake_targets"]}
    for definition in build["target"].values():
        for field in ("cache-to", "tags", "attest", "no-cache", "no-cache-filter"):
            definition.pop(field, None)
        definition["output"] = [{"type": "cacheonly"}]
    for target, stage in settings["replay"].items():
        build["target"][target]["no-cache-filter"] = [stage]
    build["target"][settings["target"]]["output"] = [{"type": "local", "dest": str(payload)}]
    component_build.write_json(directory / "qualification.bake.json", build)
    command = component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder]
    started = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with (payload / PROGRESS_PATH).open("x", encoding="utf-8") as progress:
        subprocess.run(command + ["--allow=fs.write=" + str(payload), "-f", str(directory / "qualification.bake.json"),
            settings["target"], "--progress=rawjson"], cwd=str(source), stderr=progress, check=True)
    completed = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "Python qualification execution environment changed")
    component_inputs.require_match(expected, inputs(source, resolved, settings, execution, bindings))
    (payload / "component/reports").mkdir()
    for name in REPORTS:
        shutil.copy2(str(payload / settings["copies"][1] / name), str(payload / "component/reports" / name))
    qualified = record(contract, source, payload, started, completed, directory)
    component_build.write_json(payload / RECORD_PATH, qualified)
    frontend = expected["parameters"]["recipes"][settings["target"]]["frontend"]
    lines = ["# syntax=" + frontend, "FROM scratch"]
    lines += ["COPY " + json.dumps([path + "/", "/" + path + "/"]) for path in settings["copies"] + ["component"]]
    seal = {"target": {"component-artifact": {"context": str(payload), "dockerfile-inline": "\n".join(lines) + "\n",
        "platforms": ["linux/amd64"], "output": [{"type": "oci", "dest": str(directory / "oci"), "tar": False}]}}}
    component_build.write_json(directory / "seal.bake.json", seal)
    metadata = directory / "buildx-metadata.json"
    subprocess.run(command + ["--allow=fs.read=" + str(payload), "--allow=fs.write=" + str(directory),
        "-f", str(directory / "seal.bake.json"), "component-artifact", "--progress=plain",
        "--metadata-file", str(metadata)], cwd=str(source), check=True)
    observed = oci_layout.inspect(directory / "oci", component_build._build_digest(metadata))
    extracted = extract_row(directory / "oci", observed, settings, frontend, directory / "extracted", builder, docker_config)
    receipt = component_artifacts.receipt(contract, observed, extracted, metadata_paths())
    require(content_sha256(load_json(extracted / RECORD_PATH)) == content_sha256(qualified) and
            content_sha256(record(contract, source, extracted, started, completed, directory)) == content_sha256(qualified),
            "sealed Python qualification differs")
    component_inputs.require_match(expected, inputs(source, resolved, settings, execution, bindings))
    component_build.write_json(directory / "receipt.json", receipt)
    return {"receipt": str(directory / "receipt.json"), "receipt_sha256": content_sha256(receipt),
            "artifact": receipt["artifact"], "inputs_sha256": component_inputs.identity(expected), "qualification": qualified}


def verify_local(receipt, trusted_sha256, expected, source, layout, builder, docker_config=None, temporary_parent=None):
    component_artifacts.validate_receipt(receipt)
    require(content_sha256(receipt) == trusted_sha256, "Python qualification receipt differs from trusted reference")
    component_inputs.require_match(receipt["contract"]["inputs"], expected)
    settings = expected["parameters"]["qualification"]
    require(settings == spec(source, settings["row"]), "Python qualification settings differ")
    require([item["path"] for item in receipt["metadata"]] == metadata_paths(), "Python qualification evidence set differs")
    observation = oci_layout.inspect(layout, receipt["artifact"]["root_digest"])
    frontend = expected["parameters"]["recipes"][settings["target"]]["frontend"]
    with tempfile.TemporaryDirectory(prefix="crossforge-python-qualification-verify-", dir=temporary_parent) as temporary:
        extracted = extract_row(layout, observation, settings, frontend, Path(temporary) / "extract", builder, docker_config)
        digest = component_artifacts.verify_receipt(receipt, trusted_sha256, expected, "qualification", observation,
                                                   extracted, metadata_paths())
        actual = load_json(extracted / RECORD_PATH)
        prior = record(receipt["contract"], source, extracted, actual.get("started_at"), actual.get("completed_at"), temporary)
        require(content_sha256(actual) == content_sha256(prior), "Python qualification execution or installed bytes differ")
    return {"mode": "verified-prior-execution", "reference": "oci-layout://%s@%s" % (Path(layout).resolve(), digest),
            "receipt_sha256": trusted_sha256, "qualification": prior}
