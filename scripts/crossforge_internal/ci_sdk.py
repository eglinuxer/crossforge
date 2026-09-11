"""Assemble main's SDK on one worker with authenticated raw component inputs.

Only a missing qualification index permits fresh local row qualification. The
catalog reader remains read-only; authentication or verification errors escape.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import ci_python_rows, component_build, component_ci, python_components, python_handoff
from . import component_recovery
from . import python_qualification, python_sdk, python_sdk_catalog, qualification_execution
from .identity import content_sha256, exact_fields, load_json, require


def selected_root(targets):
    require(type(targets) is list and targets and all(type(name) is str for name in targets) and
            len(targets) == len(set(targets)) and
            not set(targets) - {"python-matrix", "python-dev", "sdk-complete-dev"},
            "component SDK selection must contain only canonical SDK roots")
    # Complete SDK inherits python-dev and runs its append/final gates.
    return "sdk-complete-dev" if "sdk-complete-dev" in targets else "python-dev"


def raw_components(acquired, rows):
    require(acquired["status"] in ("ready", "components-required") and not acquired["required_builds"],
            "SDK requires all planned raw component producers to finish")
    toolchains, python = acquired["raw_toolchains"]["components"], acquired["raw_python"]["components"]
    exact_fields(toolchains, [arch + "-toolchain-install" for arch in python_components.ARCHES], "SDK raw toolchains")
    exact_fields(python, [row + "-" + part for row in rows for part, _, _ in python_handoff.PARTS], "SDK raw Python parts")
    require(all(item.get("status") == "verified-build-component" for item in list(toolchains.values()) + list(python.values())),
            "SDK cannot implicitly compile a missing raw component")
    return {"toolchains": {arch: toolchains[arch + "-toolchain-install"]["subject"] for arch in python_components.ARCHES},
            "rows": {row: {"subjects": dict(
                {arch + "-toolchain": toolchains[arch + "-toolchain-install"]["subject"] for arch in python_components.ARCHES},
                **{part: python[row + "-" + part]["subject"] for part, _, _ in python_handoff.PARTS})} for row in rows}}


def discard_row_intermediates(data):
    """After sealed-row acceptance and diagnostic preservation, keep its OCI and receipt."""
    paths = [data / name for name in ("payload", "extracted")]
    # Check both owned staging roots before deleting either. rmtree does not
    # follow the installed tree's symlinks; no registry objects are removed.
    require(all(not path.is_symlink() and (not path.exists() or path.is_dir()) for path in paths),
            "SDK row intermediate must be an owned directory")
    for path in paths:
        if path.exists():
            shutil.rmtree(str(path))


def fresh_row(source, graph, row, execution, producer, subjects, expected_sha256, data, evidence, builder, docker_config):
    started = time.monotonic()
    print("SDK %s: qualifying both targets on the integration worker" % row, flush=True)
    data.parent.mkdir(parents=True, exist_ok=True)
    try:
        built = python_qualification.produce(source, graph, row, execution, producer, subjects, data, builder, docker_config)
    finally:
        ci_python_rows.preserve_qualification(data, evidence, row)
    receipt = load_json(data / "receipt.json")
    require(built["inputs_sha256"] == expected_sha256 and
            content_sha256(receipt) == built["receipt_sha256"] and receipt["artifact"] == built["artifact"] and
            receipt["contract"]["producer"] == producer,
            "fresh SDK row differs from its planned inputs, receipt or producer")
    discard_row_intermediates(data)
    result = {"origin": "fresh-execution", "producer": producer, "seconds": round(time.monotonic() - started, 3),
              "subject": {"receipt": str(data / "receipt.json"), "receipt_sha256": built["receipt_sha256"], "layout": str(data / "oci")}}
    component_build.write_json(evidence / "result.json", result)
    return result


ROW_JOB_FIELDS = ("source", "graph", "row", "execution", "producer", "subjects", "expected_sha256", "data", "evidence", "builder", "docker_config")


def qualify_request(path, trusted_sha256):
    job = load_json(path)
    exact_fields(job, ROW_JOB_FIELDS, "SDK row worker request")
    require(content_sha256(job) == trusted_sha256, "SDK row worker request differs from the selected SHA256")
    source, data, evidence = (Path(job[name]) for name in ("source", "data", "evidence"))
    ci_python_rows.unchanged(source, job["producer"], job["execution"], job["builder"], job["docker_config"])
    result = fresh_row(source, job["graph"], job["row"], job["execution"], job["producer"], job["subjects"],
        job["expected_sha256"], data, evidence, job["builder"], job["docker_config"])
    ci_python_rows.unchanged(source, job["producer"], job["execution"], job["builder"], job["docker_config"])
    return result


def run_row(source, graph, row, execution, producer, subjects, expected_sha256, data, evidence, builder, docker_config):
    # Qualification dynamically loads modules with runpy, whose sys mutations
    # are not thread-safe. Threads only launch independent interpreter processes.
    job = dict(zip(ROW_JOB_FIELDS, (str(source), graph, row, execution, producer, subjects, expected_sha256,
        str(data), str(evidence), builder, str(docker_config) if docker_config is not None else None)))
    path = data.parent.parent / "requests" / (row + ".json")
    component_build.write_json(path, job)
    subprocess.run([sys.executable, str(Path(source) / "scripts/ci-sdk.py"), "qualify-row", "--request", str(path),
                    "--request-sha256", content_sha256(job)], check=True)
    result = load_json(evidence / "result.json")
    require(result["origin"] == "fresh-execution" and result["producer"] == producer and
            result["subject"]["receipt"] == str(data / "receipt.json") and result["subject"]["layout"] == str(data / "oci"),
            "SDK row worker returned a different producer or artifact location")
    return result


def execute(source, targets, data, evidence, builder, oras, cosign, docker_config=None):
    root = selected_root(targets)
    data, evidence = python_sdk_catalog.directories(data, evidence)
    producer = component_ci.checked_source(source, "main")
    graph = component_ci.source_graph(source, [root], evidence / "source", builder, docker_config)
    rows = python_sdk.validate_graph(source, graph, root)
    execution = qualification_execution.execution_identity(builder, docker_config)
    component_build.write_json(evidence / "request.json", {"targets": sorted(targets), "root": root,
        "producer": producer, "execution": execution, "row_parallelism": 2})
    acquired = python_sdk_catalog.acquire(source, graph, root, execution, data / "catalog", evidence / "acquisition",
        builder, oras, cosign, docker_config, record_recovery=True)
    checkpoint = acquired["component_recovery"]
    if checkpoint is not None:
        component_build.write_json(evidence / "sdk-recovery-reference.json", {"sha256": checkpoint["sha256"],
            "root": root, "document": "acquisition/component-recovery.json"})
    components = raw_components(acquired, rows)
    raw = dict(acquired["raw_toolchains"]["components"], **acquired["raw_python"]["components"])
    roots = sorted({"python-dev" if name == "python-matrix" else name for name in targets})
    recovery = component_recovery.document(component_recovery.context(source, graph, "sdk", roots,
        execution["build"], producer["source_commit"]), raw, component_recovery.requirements(source, graph, True))
    component_build.write_json(evidence / "component-recovery.json", recovery)
    component_build.write_json(evidence / "recovery-reference.json", {"sha256": content_sha256(recovery), "targets": roots})
    exact_fields(acquired["rows"], rows, "SDK row resolutions")
    missing = sorted(row for row in rows if acquired["rows"][row]["status"] == "qualification-required")
    require(acquired["required_rows"] == missing, "SDK qualification requirements differ from row resolutions")
    results = {}
    for row in rows:
        resolved = acquired["rows"][row]
        if row not in missing:
            require(resolved["status"] == "verified-qualified-row", "unsupported SDK row resolution")
            results[row] = {"origin": "authenticated-catalog", "producer": resolved["producer"], "subject": resolved["subject"]}
    # Keep the existing limit of two concurrent Python rows. Both local
    # qualifications use the same pinned builder and physical environment.
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(run_row, source, graph, row, execution, producer, components["rows"][row]["subjects"],
            acquired["rows"][row]["inputs_sha256"], data / "fresh" / row, evidence / "rows" / row, builder, docker_config): row
            for row in missing}
        try:
            for future in as_completed(pending):
                row = pending[future]
                results[row] = future.result()
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    exact_fields(results, rows, "SDK qualified rows")
    for row in rows:
        components["rows"][row]["qualification"] = results[row]["subject"]
    ci_python_rows.unchanged(source, producer, execution, builder, docker_config)
    component_build.write_json(evidence / "rows.json", results)
    component_build.write_json(evidence / "components.json", components)
    integrated = python_sdk.execute(source, graph, root, execution, components, evidence / "integration", builder, docker_config)
    ci_python_rows.unchanged(source, producer, execution, builder, docker_config)
    if checkpoint is not None:
        python_sdk_catalog.verify_checkpoint(source, graph, root, execution, checkpoint, builder, docker_config)
    result = {"schema_version": 1, "kind": "crossforge-ci-sdk-integration", "root": root,
              "selected_targets": sorted(targets), "rows": results, "integration": integrated,
              "component_recovery": checkpoint,
              "publication": "not performed; fresh rows remain local to this job"}
    component_build.write_json(evidence / "result.json", result)
    return result
