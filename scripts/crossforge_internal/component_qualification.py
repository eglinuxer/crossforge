"""Local, fresh toolchain qualification and explicit verified report consumption.

The caller supplies independently trusted component receipt digests. These APIs
do not grant trust to arbitrary local or CI producers, or qualify a final SDK.
"""

import copy
from datetime import datetime
import json
from pathlib import Path
import runpy
import subprocess
import tempfile

from . import bake_materials, component_artifacts, component_build, component_inputs, oci_layout
from . import qualification_execution
from .identity import IdentityError, content_sha256, exact_fields, file_record, load_json, require


RECORD_PATH = "component/qualification.json"
PROGRESS_PATH = "component/execution.jsonl"


def spec(arch, profile):
    require(arch in ("x86_64", "aarch64"), "unsupported qualification architecture")
    require(profile in ("toolchain", "gcc-smoke", "gcc-full"), "unsupported qualification profile")
    require(profile != "gcc-full" or arch == "x86_64", "GCC full qualification is x86_64 only")
    install = "crossforge_toolchain_%s_install" % arch
    contexts = {install: "toolchain-install"}
    if profile == "toolchain":
        target = "toolchain-%s-dev" % arch
        stages = ["toolchain-%s-qualify-build" % arch, "runtime-smoke-%s" % arch]
    else:
        target = "gcc-testsuite-%s-%s" % (arch, "smoke" if profile == "gcc-smoke" else "full-qualified")
        stages = ["gcc-testsuite-%s-base" % arch, target]
        contexts["crossforge_toolchain_%s_test_context" % arch] = "gcc-test-context"
    return {"arch": arch, "profile": profile, "component": "qualification/%s-%s" % (arch, profile),
            "target": target, "stages": stages, "contexts": contexts, "triple": arch + "-unknown-linux-gnu"}


def _gcc_profile(source, profile):
    validator = runpy.run_path(str(source / "scripts/validate-gcc-testsuite.py"))
    try:
        return validator, validator["validate_release_contract"](source / "config/release.json")["profiles"][profile]
    except validator["ValidationError"] as error:
        raise IdentityError(str(error)) from error


def report_copies(source, settings):
    arch, profile = settings["arch"], settings["profile"]
    if profile == "toolchain":
        result = {"/work/qualification/%s.json" % arch: "component/reports/%s.json" % arch}
        if arch == "x86_64":
            result["/work/qualification/clean-rocky.ok"] = "component/reports/x86_64-clean-runtime.ok"
        return result
    _, policy = _gcc_profile(source, "smoke" if profile == "gcc-smoke" else "full")
    tiers = ["host-direct"] if arch == "x86_64" else ["locked-sysroot", "clean-rocky"]
    result = {}
    for tier in tiers:
        name = "%s-%s%s" % (arch, tier, "-full" if profile == "gcc-full" else "")
        paths = [name + ".json"] + ["%s/%s.%s" % (name, suite["id"], suffix)
            for suite in policy["plan"]["suites"] for suffix in ("sum", "log", "make.log")]
        result.update({"/work/qualification/gcc-testsuite/" + path: "component/reports/" + path for path in paths})
    return result


def bind_subjects(source, graph, settings, execution, subjects, builder, docker_config=None, temporary_parent=None):
    """Re-capture source producer inputs, then verify each exact required role."""
    require(type(subjects) is dict and set(subjects) == set(settings["contexts"].values()),
            "qualification subject roles differ")
    resolved = copy.deepcopy(graph)
    definitions = resolved.get("target", {})
    require(settings["target"] in definitions and definitions[settings["target"]].get("target") == settings["target"],
            "qualification must use its canonical Docker stage")
    bindings = {}
    for context, role in settings["contexts"].items():
        subject = subjects[role]
        exact_fields(subject, ("receipt", "receipt_sha256", "layout"), "qualification subject")
        inputs = component_build.toolchain_inputs(source, graph, settings["arch"], role, execution["build"])
        frontend = inputs["parameters"]["recipes"][component_build.toolchain_spec(settings["arch"], role)["target"]]["frontend"]
        receipt = load_json(subject["receipt"])
        reference = component_build.verify_local(receipt, subject["receipt_sha256"], inputs, role,
            subject["layout"], frontend, builder, docker_config, temporary_parent)
        # Replace this boundary everywhere it is used; material capture rejects
        # any leftover source traversal or unused purported subject.
        for definition in definitions.values():
            if context in definition.get("contexts", {}):
                definition["contexts"][context] = reference
        bindings[context] = {"component": inputs["component"], "inputs_sha256": component_inputs.identity(inputs),
                             "artifact_digest": receipt["artifact"]["platform_digest"]}
    return resolved, bindings


def qualification_inputs(source, graph, settings, execution, bindings):
    require(type(execution) is dict and set(execution) == {"build", "host"} and execution["host"],
            "qualification must bind observed build and host identities")
    value = bake_materials.capture(source, graph, settings["target"], settings["component"], "qualification",
        [settings["triple"]], execution, artifacts=bindings)
    # These execute report acceptance, even though they are outside Docker RUN.
    validators = ["scripts/crossforge_internal/component_qualification.py",
                  "scripts/crossforge_internal/qualification_execution.py"]
    if settings["profile"] == "toolchain":
        validators += ["scripts/toolchain_report.py", "scripts/release-components-core.py",
                       "scripts/python_row_contract.py", "scripts/validate-release.py"]
    else:
        validators += ["scripts/validate-gcc-testsuite.py", "scripts/validate-release.py"]
    files = {record["path"]: record for record in value["files"]}
    for path in validators:
        files[path] = file_record(source, path)
    value["files"] = [files[path] for path in sorted(files)]
    value["parameters"]["qualification"] = settings
    value["parameters"]["report_copies"] = report_copies(source, settings)
    recipe = value["parameters"]["recipes"][settings["target"]]["stages"]
    runs = {stage: sum(line.startswith("RUN ") for line in recipe[stage]) for stage in settings["stages"]}
    require(all(runs.values()), "each replayed qualification stage must execute RUN")
    value["parameters"]["required_runs"] = runs
    return component_inputs.validate(value)


def validate_reports(source, settings, directory):
    arch, profile = settings["arch"], settings["profile"]
    root = Path(directory) / "component/reports"
    release = load_json(source / "config/release.json")
    if profile == "toolchain":
        components = runpy.run_path(str(source / "scripts/release-components-core.py"))
        validator = runpy.run_path(str(source / "scripts/toolchain_report.py"))
        target = next(target for target in release["targets"] if target["arch"] == arch)
        value = validator["qualify_prior_toolchain_report"](arch, settings["triple"], root / (arch + ".json"),
            release, content_sha256(release), target["sysroot"]["canonical_sha256"],
            components["toolchain_qualification_component"](release, arch))
        return [{"profile": "toolchain", "target": settings["triple"], "status": "passed", "report": value}]
    validator, policy = _gcc_profile(source, "smoke" if profile == "gcc-smoke" else "full")
    results = []
    for tier in (["host-direct"] if arch == "x86_64" else ["locked-sysroot", "clean-rocky"]):
        name = "%s-%s%s" % (arch, tier, "-full" if profile == "gcc-full" else "")
        report = load_json(root / (name + ".json"))
        summaries = {suite["id"]: root / name / (suite["id"] + ".sum") for suite in policy["plan"]["suites"]}
        try:
            normalized = validator["normalize_summaries"](policy["plan"],
                policy["baselines"][(settings["triple"], tier)]["document"], summaries, report.get("materials"))
        except validator["ValidationError"] as error:
            raise IdentityError(str(error)) from error
        require(content_sha256(report) == content_sha256(normalized), "GCC report differs from exact baseline normalization")
        require(type(report.get("materials")) is dict and report["materials"], "GCC tested materials are missing")
        materials = report["materials"]
        target_policy = next(item for item in policy["plan"]["targets"] if item["arch"] == arch)
        tier_policy = next(item for item in target_policy["runtime_tiers"] if item["name"] == tier)
        component = load_json(source / "config/generated/components/toolchain/gcc-testsuite-qualification.json")
        require(materials.get("qualification_component") == {
            "component": "toolchain/gcc-testsuite-qualification", "canonical_sha256": content_sha256(component)},
            "GCC qualification component differs")
        require(materials.get("site_sha256") == policy["plan"]["site"]["sha256"] and
                materials.get("board") == {key: tier_policy["board"][key] for key in ("name", "sha256")} and
                materials.get("gcc", {}).get("version") == release["gts"]["gcc_version"],
                "GCC tested policy materials differ")
        require(type(materials.get("make")) is list and
                sorted(item.get("suite", "") for item in materials["make"]) == sorted(summaries),
                "GCC executed suite coverage differs")
        for invocation in report["materials"]["make"]:
            require(invocation["log_sha256"] == file_record(root, name + "/" + invocation["suite"] + ".make.log")["sha256"],
                    "GCC make log differs from executed materials")
        results.append({"profile": profile, "target": settings["triple"], "tier": tier, "status": "passed",
                        "report_sha256": file_record(root, name + ".json")["sha256"],
                        "status_counts": report["status_counts"]})
    return results


def _record(contract, source, directory, started_at, completed_at):
    inputs = contract["inputs"]
    settings = inputs["parameters"]["qualification"]
    return {"schema_version": 1, "kind": "crossforge-component-qualification", "mode": "executed",
            "inputs_sha256": component_inputs.identity(inputs), "producer": contract["producer"],
            "started_at": started_at, "completed_at": completed_at,
            "vertices": qualification_execution.fresh_vertices(directory / PROGRESS_PATH,
                inputs["parameters"]["required_runs"], started_at, completed_at),
            "coverage": validate_reports(source, settings, directory)}


def _utcnow():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def produce(source, graph, arch, profile, execution, producer, subjects, directory, builder, docker_config=None):
    source, directory = Path(source).resolve(), Path(directory).resolve()
    require(not directory.exists(), "qualification output directory must be new")
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "qualification execution environment differs")
    settings = spec(arch, profile)
    # Verify before creating any build output.
    resolved, bindings = bind_subjects(source, graph, settings, execution, subjects, builder, docker_config, directory.parent)
    inputs = qualification_inputs(source, resolved, settings, execution, bindings)
    contract = component_artifacts.contract("qualification", inputs, producer)
    payload = directory / "payload"
    component_build.write_json(payload / component_artifacts.CONTRACT_PATH, contract)
    component_build.write_json(directory / "inputs.json", inputs)
    frontend = inputs["parameters"]["recipes"][settings["target"]]["frontend"]
    for definition in resolved["target"].values():
        for field in ("cache-to", "tags", "attest", "no-cache", "no-cache-filter"):
            definition.pop(field, None)
        definition["output"] = [{"type": "cacheonly"}]
    resolved.pop("group", None)
    resolved["target"][settings["target"]]["no-cache-filter"] = settings["stages"]
    lines = ["# syntax=" + frontend, "FROM scratch"]
    lines += ["COPY --from=gate " + json.dumps([src, "/" + dst])
              for src, dst in sorted(inputs["parameters"]["report_copies"].items())]
    resolved["target"]["component-qualification-reports"] = {
        "context": ".", "dockerfile-inline": "\n".join(lines) + "\n", "platforms": ["linux/amd64"],
        "contexts": {"gate": "target:" + settings["target"]}, "output": [{"type": "local", "dest": str(payload)}]}
    component_build.write_json(directory / "qualification.bake.json", resolved)
    command = component_build.docker_command(docker_config) + ["buildx", "bake", "--builder", builder]
    started_at = _utcnow()
    with (payload / PROGRESS_PATH).open("x", encoding="utf-8") as progress:
        subprocess.run(command + ["--allow=fs.write=" + str(payload), "-f", str(directory / "qualification.bake.json"),
            "component-qualification-reports", "--progress=rawjson"], cwd=str(source), stderr=progress, check=True)
    completed_at = _utcnow()
    require(qualification_execution.execution_identity(builder, docker_config) == execution,
            "qualification execution environment changed")
    current = qualification_inputs(source, resolved, settings, execution, bindings)
    component_inputs.require_match(inputs, current)
    record = _record(contract, source, payload, started_at, completed_at)
    component_build.write_json(payload / RECORD_PATH, record)
    paths = sorted([component_artifacts.CONTRACT_PATH, RECORD_PATH, PROGRESS_PATH] +
                   list(inputs["parameters"]["report_copies"].values()))
    seal = {"target": {"component-artifact": {"context": str(payload),
        "dockerfile-inline": "# syntax=" + frontend + "\nFROM scratch\nCOPY component/ /component/\n",
        "platforms": ["linux/amd64"], "output": [{"type": "oci", "dest": str(directory / "oci"), "tar": False}]}}}
    component_build.write_json(directory / "seal.bake.json", seal)
    metadata_file = directory / "buildx-metadata.json"
    subprocess.run(command + ["--allow=fs.read=" + str(payload), "--allow=fs.write=" + str(directory),
        "-f", str(directory / "seal.bake.json"), "component-artifact", "--progress=plain",
        "--metadata-file", str(metadata_file)], cwd=str(source), check=True)
    observation = oci_layout.inspect(directory / "oci", component_build._build_digest(metadata_file))
    extracted = component_build.extract_metadata(directory / "oci", observation, paths, frontend,
        directory / "extracted", builder, docker_config)
    receipt = component_artifacts.receipt(contract, observation, extracted, paths)
    require(content_sha256(load_json(extracted / RECORD_PATH)) == content_sha256(record) and
            content_sha256(_record(contract, source, extracted, started_at, completed_at)) == content_sha256(record),
            "sealed qualification reports differ")
    component_build.write_json(directory / "receipt.json", receipt)
    return {"receipt": str(directory / "receipt.json"), "receipt_sha256": content_sha256(receipt),
            "artifact": receipt["artifact"], "inputs_sha256": component_inputs.identity(inputs), "qualification": record}


def verify_local(receipt, trusted_sha256, expected_inputs, source, layout, builder,
                 docker_config=None, temporary_parent=None):
    """Verify prior execution, retaining its producer and interval verbatim."""
    component_artifacts.validate_receipt(receipt)
    require(content_sha256(receipt) == trusted_sha256, "qualification receipt differs from trusted reference")
    component_inputs.require_match(receipt["contract"]["inputs"], expected_inputs)
    settings = expected_inputs["parameters"]["qualification"]
    require(settings == spec(settings["arch"], settings["profile"]), "qualification profile contract differs")
    paths = sorted([component_artifacts.CONTRACT_PATH, RECORD_PATH, PROGRESS_PATH] +
                   list(expected_inputs["parameters"]["report_copies"].values()))
    require(paths == [record["path"] for record in receipt["metadata"]], "qualification evidence set differs")
    observation = oci_layout.inspect(layout, receipt["artifact"]["root_digest"])
    frontend = expected_inputs["parameters"]["recipes"][settings["target"]]["frontend"]
    with tempfile.TemporaryDirectory(prefix="crossforge-qualification-verify-", dir=temporary_parent) as temp:
        extracted = component_build.extract_metadata(layout, observation, paths, frontend, Path(temp) / "extract", builder, docker_config)
        digest = component_artifacts.verify_receipt(receipt, trusted_sha256, expected_inputs, "qualification", observation, extracted, paths)
        actual = load_json(extracted / RECORD_PATH)
        expected = _record(receipt["contract"], Path(source), extracted, actual.get("started_at"), actual.get("completed_at"))
        require(content_sha256(actual) == content_sha256(expected), "qualification execution or coverage differs")
    return {"mode": "verified-prior-execution", "reference": "oci-layout://%s@%s" % (Path(layout).resolve(), digest),
            "receipt_sha256": trusted_sha256, "qualification": expected}
