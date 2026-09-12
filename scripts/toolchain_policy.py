"""Toolchain qualification inputs, independent of a product release binding.

Docker stages authenticate a small existing component closure. The final SDK
independently derives the same policy from its validated complete release.
"""

import copy
import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location("crossforge_toolchain_component_reader",
                                              str(Path(__file__).with_name("release_component.py")))
component = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(component)


class PolicyError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def _tree(document, prefix):
    """Read a dictionary subtree; unsupported arrays/overlapping leaves fail."""
    result = {}
    tokens = component.decode_json_pointer(prefix)
    for material in document["materials"]:
        path = component.decode_json_pointer(material["path"])
        if path[:len(tokens)] != tokens:
            continue
        require(len(path) > len(tokens), "expected projected dictionary leaves")
        target = result
        for name in path[len(tokens):-1]:
            require(not name.isdigit(), "numeric policy dictionary keys are unsupported")
            target = target.setdefault(name, {})
            require(type(target) is dict, "overlapping qualification policy leaves")
        name = path[-1]
        require(name not in target and not name.isdigit(), "overlapping qualification policy leaves")
        target[name] = copy.deepcopy(material["value"])
    require(result, "missing qualification policy subtree: %s" % prefix)
    return result


def _leaf(document, pointer, kind):
    return component.material_value(document, document["component"], document["scope"],
                                    component.canonical_sha256(document), pointer, kind)


def _identity(document):
    return {"component": document["component"], "canonical_sha256": component.canonical_sha256(document)}


def _policy(arch, identity, target, baseline, gcc, binutils, runtime_base, executor):
    require(arch in ("x86_64", "aarch64"), "unsupported toolchain policy architecture")
    require(type(identity) is dict and set(identity) == {"component", "canonical_sha256"} and
            identity["component"] == "toolchain/%s-qualification" % arch, "toolchain policy component differs")
    component.validate_canonical_sha256(identity["canonical_sha256"])
    require(type(target) is dict and target == {"arch": arch, "triple": arch + "-unknown-linux-gnu", "sysroot": target.get("sysroot")},
            "toolchain policy target differs")
    sysroot = target["sysroot"]
    require(type(sysroot) is dict and set(sysroot) == {"status", "lock_file", "canonical_sha256"} and
            sysroot["status"] == "locked" and sysroot["lock_file"] == "locks/sysroot-el8-%s.json" % arch,
            "toolchain sysroot policy differs")
    component.validate_canonical_sha256(sysroot["canonical_sha256"])
    require(type(baseline) is dict and set(baseline) == {"file", "canonical_sha256"} and
            baseline["file"] == "abi/el8/%s.json" % arch, "toolchain ABI policy differs")
    component.validate_canonical_sha256(baseline["canonical_sha256"])
    for tool in (gcc, binutils):
        require(type(tool) is dict and set(tool) == {"version", "source"} and type(tool["version"]) is str and
                re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", tool["version"]), "invalid toolchain version policy")
        source = tool["source"]
        require(type(source) is dict and set(source) == {"status", "repository_nevra", "header_arch", "url", "sha256", "size", "spec_sha256"},
                "toolchain source policy fields differ")
        require(source["status"] == "locked" and type(source["url"]) is str and source["url"].startswith("https://") and
                type(source["size"]) is int and source["size"] > 0 and
                all(type(source[name]) is str and source[name] for name in ("repository_nevra", "header_arch")),
                "toolchain source is not locked")
        for name in ("sha256", "spec_sha256"):
            component.validate_canonical_sha256(source[name])
    require(type(runtime_base) is dict and set(runtime_base) == {"index_digest", "manifest_digest"} and all(type(value) is str and
            re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in runtime_base.values()), "invalid runtime base policy")
    if arch == "x86_64":
        require(executor == {"kind": "native"}, "x86_64 qualification must execute directly")
    else:
        require(type(executor) is dict and set(executor) == {"kind", "binary_sha256", "version", "cpu", "uname_release"} and executor["kind"] == "qemu",
                "aarch64 qualification must use explicit QEMU")
        component.validate_canonical_sha256(executor["binary_sha256"])
        require(all(type(executor[name]) is str and executor[name] for name in ("version", "cpu", "uname_release")),
                "QEMU execution identity is incomplete")
    return {"schema_version": 1, "kind": "crossforge-toolchain-policy", "component": copy.deepcopy(identity),
            "target": copy.deepcopy(target), "abi_baseline": copy.deepcopy(baseline),
            "gcc": copy.deepcopy(gcc), "binutils": copy.deepcopy(binutils),
            "runtime_base": copy.deepcopy(runtime_base), "runtime_executor": copy.deepcopy(executor)}


def from_release(release, arch, identity):
    """The caller validates release and independently derives its component ID."""
    require(arch in ("x86_64", "aarch64"), "unsupported toolchain policy architecture")
    require(release.get("baseline") == "el8", "unsupported toolchain ABI baseline")
    targets = [target for target in release["targets"] if target["arch"] == arch]
    require(len(targets) == 1, "toolchain release target is not unique")
    executor = {"kind": "native"}
    if arch == "aarch64":
        executor = {name: release["qemu"]["executor"][name] for name in ("binary_sha256", "cpu", "uname_release")}
        executor.update(kind="qemu", version=release["qemu"]["version"])
    return _policy(arch, identity, targets[0], release["abi"]["targets"][arch]["baseline"],
        {"version": release["gts"]["gcc_version"], "source": release["gts"]["source"]},
        {"version": release["binutils"]["version"], "source": release["binutils"]["source"]},
        {"index_digest": release["base_image"]["digest"],
         "manifest_digest": release["base_image"]["manifests"]["amd64" if arch == "x86_64" else "arm64"]}, executor)


def load(directory, arch, trusted_sha256):
    """Authenticate five projections through the one required qualification pin."""
    require(arch in ("x86_64", "aarch64"), "unsupported toolchain policy architecture")
    directory = Path(directory)
    try:
        root = component.load_component(directory / ("toolchain/%s-qualification.json" % arch),
            "toolchain/%s-qualification" % arch, "qualification", trusted_sha256)
        def child(parent, name, scope):
            matches = [value["canonical_sha256"] for value in parent["dependencies"] if value["component"] == name]
            require(len(matches) == 1, "toolchain policy dependency is missing: %s" % name)
            return component.load_component(directory / (name + ".json"), name, scope, matches[0])
        require({value["component"] for value in root["dependencies"]} ==
                {"toolchain/%s-build" % arch, "abi/%s-baseline" % arch}, "qualification dependency set differs")
        build = child(root, "toolchain/%s-build" % arch, "build")
        baseline = child(root, "abi/%s-baseline" % arch, "qualification")
        require({value["component"] for value in build["dependencies"]} == {
            "rpm/host-build-common", "rpm/host-gcc-build", "rpm/sysroot-%s" % arch, "sources/gcc", "sources/binutils"},
            "toolchain build dependency set differs")
        gcc = child(build, "sources/gcc", "build")
        binutils = child(build, "sources/binutils", "build")
        require(not baseline["dependencies"] and not gcc["dependencies"] and not binutils["dependencies"],
                "unexpected policy leaf dependencies")
        indices = {component.decode_json_pointer(value["path"])[1] for value in root["materials"]
                   if value["path"].startswith("/targets/")}
        require(len(indices) == 1, "toolchain qualification has ambiguous target materials")
        prefix = "/targets/" + next(iter(indices))
        target = _tree(root, prefix)
        require(target == _tree(build, prefix) and _leaf(root, "/baseline", "string") ==
                _leaf(build, "/baseline", "string") == "el8", "toolchain build/qualification target differs")
        executor = {"kind": "native"}
        if arch == "aarch64":
            executor = {name: _leaf(root, "/qemu/executor/" + name, "string") for name in ("binary_sha256", "cpu", "uname_release")}
            executor.update(kind="qemu", version=_leaf(root, "/qemu/version", "string"))
            require(_leaf(root, "/qemu/executor/status", "string") == "locked", "QEMU executor is not locked")
        return _policy(arch, _identity(root), target, _tree(baseline, "/abi/targets/%s/baseline" % arch),
            {"version": _leaf(gcc, "/gts/gcc_version", "string"), "source": _tree(gcc, "/gts/source")},
            {"version": _leaf(binutils, "/binutils/version", "string"), "source": _tree(binutils, "/binutils/source")},
            {"index_digest": _leaf(root, "/base_image/digest", "string"),
             "manifest_digest": _leaf(root, "/base_image/manifests/" + ("amd64" if arch == "x86_64" else "arm64"), "string")}, executor)
    except component.ComponentError as error:
        raise PolicyError(str(error)) from error


def binding(policy):
    return {"schema_version": 1, "kind": "crossforge-toolchain-input-binding",
            "policy_sha256": component.canonical_sha256(policy)}


def require_binding(report, policy):
    require(component.canonical_sha256(report.get("input_binding")) == component.canonical_sha256(binding(policy)),
            "toolchain report input binding differs")
    require("release_sha256" not in report, "scoped toolchain report cannot claim a complete release binding")
