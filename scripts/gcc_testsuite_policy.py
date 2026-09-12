"""Read GCC test policy from an authenticated component closure.

This is deliberately not a partial release document. Final candidate checks
continue to derive the qualification component from the complete release.
"""

import copy
from pathlib import Path
import re
import runpy

COMPONENT = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
COMPONENT_NAME = "toolchain/gcc-testsuite-qualification"
PROFILES = {"smoke": ("/gcc_testsuite", 3), "full": ("/gcc_testsuite/full", 1)}


class PolicyError(ValueError):
    pass


def require(value, message):
    if not value:
        raise PolicyError(message)


def _policy(arch, identity, gcc_version, profiles, executor):
    require(arch in ("x86_64", "aarch64"), "unsupported GCC test architecture")
    require(type(identity) is dict and set(identity) == {"component", "canonical_sha256"} and
            identity["component"] == COMPONENT_NAME, "GCC test component identity differs")
    COMPONENT["validate_canonical_sha256"](identity["canonical_sha256"])
    require(type(gcc_version) is str and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", gcc_version),
            "GCC version policy differs")
    require(type(profiles) is dict and set(profiles) == set(PROFILES), "GCC test profiles differ")
    if arch == "x86_64":
        require(executor == {"kind": "native"}, "x86_64 GCC tests must execute directly")
    else:
        require(type(executor) is dict and set(executor) == {"kind", "binary_sha256"} and
                executor["kind"] == "qemu", "aarch64 GCC tests require explicit QEMU")
        COMPONENT["validate_canonical_sha256"](executor["binary_sha256"])
    return {"schema_version": 1, "kind": "crossforge-gcc-testsuite-policy", "arch": arch,
            "component": copy.deepcopy(identity), "gcc_version": gcc_version,
            "profiles": copy.deepcopy(profiles), "executor": copy.deepcopy(executor)}


def from_release(release, arch, identity):
    """For callers which already validated the complete release."""
    smoke = release["gcc_testsuite"]
    profiles = {"smoke": {key: copy.deepcopy(smoke[key]) for key in ("profile", "plan", "baselines")},
                "full": copy.deepcopy(smoke["full"])}
    executor = {"kind": "native"} if arch == "x86_64" else {
        "kind": "qemu", "binary_sha256": release["qemu"]["executor"]["binary_sha256"]}
    return _policy(arch, identity, release["gts"]["gcc_version"], profiles, executor)


def load(directory, arch, trusted_sha256):
    require(arch in ("x86_64", "aarch64"), "unsupported GCC test architecture")
    directory = Path(directory)
    try:
        root = COMPONENT["load_component"](directory / (COMPONENT_NAME + ".json"),
                                            COMPONENT_NAME, "qualification", trusted_sha256)
        dependencies = {item["component"]: item["canonical_sha256"] for item in root["dependencies"]}
        require(set(dependencies) == {"rpm/host-gcc-test", "sources/gcc", "toolchain/x86_64-qualification",
                                      "toolchain/aarch64-qualification"}, "GCC test dependency set differs")
        materials = {item["path"]: item["value"] for item in root["materials"]}
        used = set()

        def text(path):
            used.add(path)
            require(type(materials.get(path)) is str, "missing or invalid GCC test material: " + path)
            return materials[path]

        profiles = {}
        for name, (prefix, count) in PROFILES.items():
            require(text(prefix + "/profile") == name, "GCC component profile differs")
            profiles[name] = {"profile": name, "plan": {
                key: text(prefix + "/plan/" + key) for key in ("file", "canonical_sha256")},
                "baselines": [{key: text(prefix + "/baselines/%d/" % index + key)
                               for key in ("target", "runtime_tier", "file", "canonical_sha256")}
                              for index in range(count)]}
        require(set(materials) == used, "GCC component contains unexpected policy materials")

        def child(name, scope):
            return COMPONENT["load_component"](directory / (name + ".json"), name, scope, dependencies[name])

        def leaf(document, pointer):
            return COMPONENT["material_value"](document, document["component"], document["scope"],
                COMPONENT["canonical_sha256"](document), pointer, "string")

        gcc = child("sources/gcc", "build")
        require(not gcc["dependencies"], "GCC source policy has unexpected dependencies")
        executor = {"kind": "native"}
        if arch == "aarch64":
            runtime = child("toolchain/aarch64-qualification", "qualification")
            require(leaf(runtime, "/qemu/executor/status") == "locked", "GCC QEMU executor is not locked")
            executor = {"kind": "qemu", "binary_sha256": leaf(runtime, "/qemu/executor/binary_sha256")}
        return _policy(arch, {"component": COMPONENT_NAME, "canonical_sha256": trusted_sha256},
                       leaf(gcc, "/gts/gcc_version"), profiles, executor)
    except COMPONENT["ComponentError"] as error:
        raise PolicyError(str(error)) from error
