"""Python installation and build-audit artifacts through the shared OCI transport.

Build artifacts do not assert qualification. Row consumers reverify every
subject against current producer inputs before executing the existing gates.
"""

import copy
from pathlib import Path
import runpy

from . import bake_materials, component_build, component_inputs
from .identity import content_sha256, digest_value, exact_fields, load_json, require


ARCHES = ("x86_64", "aarch64")


def spec(source, row, arch, kind):
    require(arch in ("build",) + ARCHES, "unsupported Python component architecture")
    require(kind in ("install", "test-context") and (arch != "build" or kind == "install"),
            "unsupported Python component role")
    rows = runpy.run_path(str(Path(source) / "scripts/python_row_contract.py"))
    try:
        selected = rows["bind_release"](load_json(Path(source) / "config/release.json"), row=row)
        contract = selected["contract"]
    except rows["ContractError"] as error:
        raise ValueError(str(error)) from error
    prefix = "/opt/crossforge/python/" + row
    if arch == "build":
        target, stage = "cpython-build-%s-export" % row, "cpython-build-export"
        copies = [prefix + "/build/", "/work/source/source-manifest.json"]
    elif kind == "install":
        target, stage = "cpython-cross-%s-%s-export" % (row, arch), "cpython-cross-export"
        copies = [prefix + "/targets/" + arch + "-unknown-linux-gnu/"]
    else:
        target, stage = "cpython-%s-%s-test-context-export" % (row, arch), "cpython-test-context-export"
        copies = ["/work/build/cpython-%s-%s/target-artifact-audit.log" % (row, arch),
                  "/work/source/source-manifest.json"]
    return {"component": "python/%s-%s-%s" % (row, arch, kind), "target": target, "stage": stage,
            "role": "python-install" if kind == "install" else "python-test-context",
            "row": row, "arch": arch, "kind": kind, "version": selected["entry"]["version"], "adapter": contract["adapter"],
            "targets": sorted(value + "-unknown-linux-gnu" for value in (ARCHES if arch == "build" else (arch,))),
            "copies": copies}


def inputs(source, graph, settings, execution, bindings=None):
    require(type(execution) is dict and "buildkit_image" in execution, "declare pinned BuildKit execution image")
    reference = execution["buildkit_image"]
    require(type(reference) is str and reference.count("@") == 1, "BuildKit execution image must be pinned")
    digest_value(reference.rsplit("@", 1)[1], "BuildKit execution image digest", oci=True)
    definition = graph.get("target", {}).get(settings["target"], {})
    require(definition.get("target") == settings["stage"] and definition.get("dockerfile") == "docker/python.Dockerfile",
            "Python artifact must use its canonical Docker stage")
    args = definition.get("args", {})
    require(all(args.get(key) == settings[field] for key, field in
                (("CPYTHON_ROW", "row"), ("CPYTHON_VERSION", "version"), ("CPYTHON_ADAPTER", "adapter"))),
            "Python artifact row arguments differ")
    if settings["arch"] != "build":
        require(args.get("CROSSFORGE_TARGET_ARCH") == settings["arch"] and
                args.get("CROSSFORGE_TARGET_TRIPLE") == settings["arch"] + "-unknown-linux-gnu",
                "Python artifact target arguments differ")
    result = bake_materials.capture(source, graph, settings["target"], settings["component"], settings["role"],
                                   settings["targets"], execution, artifacts=bindings)
    result["parameters"]["python_component"] = settings
    result["parameters"]["artifact_copies"] = settings["copies"]
    return component_inputs.validate(result)


def verify(subject, expected, role, source_target, builder, docker_config=None, temporary_parent=None):
    exact_fields(subject, ("receipt", "receipt_sha256", "layout"), "Python component subject")
    receipt = load_json(subject["receipt"])
    frontend = expected["parameters"]["recipes"][source_target]["frontend"]
    reference = component_build.verify_local(receipt, subject["receipt_sha256"], expected, role,
        subject["layout"], frontend, builder, docker_config, temporary_parent)
    binding = {"component": expected["component"], "inputs_sha256": component_inputs.identity(expected),
               "artifact_digest": receipt["artifact"]["platform_digest"]}
    return reference, binding


def _replace(graph, target, context, original, reference):
    definition = graph.get("target", {}).get(target, {})
    require(definition.get("contexts", {}).get(context) == "target:" + original,
            "Python component source boundary differs: %s:%s" % (target, context))
    definition["contexts"][context] = reference


def bind_build(source, graph, settings, execution, subjects, builder, docker_config=None, temporary_parent=None):
    """Bind this target's toolchain, including its private zstd dependency."""
    require(type(subjects) is dict and set(subjects) ==
            (set() if settings["arch"] == "build" else {"toolchain-install", "build-python"}),
            "Python build subject set differs")
    resolved, bindings = copy.deepcopy(graph), {}
    if settings["arch"] == "build":
        return resolved, bindings
    arch = settings["arch"]
    cross = "cpython-cross-%s-%s" % (settings["row"], arch)
    rows = runpy.run_path(str(Path(source) / "scripts/python_row_contract.py"))
    uses_zstd = rows["contract_for_row"](settings["row"])["zstd"]
    zstd_target = "zstd-%s-build" % arch
    require(resolved.get("target", {}).get(cross, {}).get("contexts", {}).get("crossforge_zstd") ==
            "target:" + (zstd_target if uses_zstd else "zstd-empty"), "Python private zstd boundary differs")
    native = spec(source, settings["row"], "build", "install")
    planned = [("crossforge_cpython_build", subjects["build-python"], native["target"], native["role"],
                inputs(source, graph, native, execution)),
               ("crossforge_toolchain", subjects["toolchain-install"],
                component_build.toolchain_spec(arch, "toolchain-install")["target"], "toolchain-install",
                component_build.toolchain_inputs(source, graph, arch, "toolchain-install", execution))]
    for context, subject, target, role, expected in planned:
        reference, binding = verify(subject, expected, role, target, builder, docker_config, temporary_parent)
        _replace(resolved, cross, context, target, reference)
        bindings[cross + ":" + context] = binding
        if uses_zstd and role == "toolchain-install":
            _replace(resolved, zstd_target, context, target, reference)
            bindings[zstd_target + ":" + context] = binding
    return resolved, bindings


def produce(source, graph, row, arch, kind, execution, producer, subjects, directory, builder, docker_config=None):
    require(component_build.execution_identity(builder, docker_config) == execution,
            "Python component execution environment differs")
    settings = spec(source, row, arch, kind)
    resolved, bindings = bind_build(source, graph, settings, execution, subjects, builder, docker_config, Path(directory).parent)
    return component_build.produce_artifact(source, resolved, settings, settings["role"], execution,
        lambda: inputs(source, resolved, settings, execution, bindings), producer, directory, builder, docker_config)


def bind_row(source, graph, row, execution, subjects, builder, docker_config=None, temporary_parent=None):
    """Supply the existing complete row pipeline with seven verified subjects."""
    required = {"build"} | {arch + "-" + kind for arch in ARCHES for kind in ("install", "test-context", "toolchain")}
    require(type(subjects) is dict and set(subjects) == required, "Python row subject set differs")
    native = spec(source, row, "build", "install")
    native_reference, native_binding = verify(subjects["build"], inputs(source, graph, native, execution),
        native["role"], native["target"], builder, docker_config, temporary_parent)
    resolved, bindings = copy.deepcopy(graph), {}
    row_target = "python-row-" + row
    _replace(resolved, row_target, "crossforge_cpython_build", native["target"], native_reference)
    bindings[row_target + ":crossforge_cpython_build"] = native_binding
    for arch in ARCHES:
        qualify = "cpython-%s-%s-qualify-build" % (row, arch)
        _replace(resolved, qualify, "crossforge_cpython_build", native["target"], native_reference)
        bindings[qualify + ":crossforge_cpython_build"] = native_binding
        target = component_build.toolchain_spec(arch, "toolchain-install")["target"]
        expected = component_build.toolchain_inputs(source, graph, arch, "toolchain-install", execution)
        reference, binding = verify(subjects[arch + "-toolchain"], expected, "toolchain-install", target,
                                    builder, docker_config, temporary_parent)
        _replace(resolved, qualify, "crossforge_toolchain", target, reference)
        bindings[qualify + ":crossforge_toolchain"] = binding
        runtime = "cpython-%s-%s-qualify" % (row, arch)
        _replace(resolved, runtime, "crossforge_sysroot", "sysroot-" + arch, reference)
        bindings[runtime + ":crossforge_sysroot"] = binding
        for kind, context in (("install", "crossforge_cpython_install"), ("test-context", "crossforge_cpython_test_context")):
            settings = spec(source, row, arch, kind)
            build_graph, build_bindings = bind_build(source, graph, settings, execution,
                {"build-python": subjects["build"], "toolchain-install": subjects[arch + "-toolchain"]},
                builder, docker_config, temporary_parent)
            expected = inputs(source, build_graph, settings, execution, build_bindings)
            reference, binding = verify(subjects[arch + "-" + kind], expected, settings["role"], settings["target"],
                                        builder, docker_config, temporary_parent)
            _replace(resolved, qualify, context, settings["target"], reference)
            bindings[qualify + ":" + context] = binding
    return resolved, bindings
