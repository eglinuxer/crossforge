"""Read per-row Python qualification inputs without a complete release.

This is configuration identity, not execution evidence or permission to reuse a
report. The caller supplies an independently trusted component digest; release
consumers derive that digest again from their complete, validated release.
"""

import copy
from pathlib import Path
import re
import runpy


READER = runpy.run_path(str(Path(__file__).with_name("release_component.py")))
ROWS = runpy.run_path(str(Path(__file__).with_name("python_row_contract.py")))


class PolicyError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def _object(document, prefix):
    """Read dictionary leaves only; arrays and overlapping leaves are rejected."""
    result = {}
    prefix = READER["decode_json_pointer"](prefix)
    for material in document["materials"]:
        path = READER["decode_json_pointer"](material["path"])
        if path[:len(prefix)] != prefix:
            continue
        suffix = path[len(prefix):]
        require(suffix and not any(name.isdigit() for name in suffix),
                "expected dictionary policy leaves")
        target = result
        for name in suffix[:-1]:
            target = target.setdefault(name, {})
            require(type(target) is dict, "overlapping policy leaves")
        require(suffix[-1] not in target, "overlapping policy leaves")
        target[suffix[-1]] = copy.deepcopy(material["value"])
    require(result, "missing policy object: /" + "/".join(prefix))
    return result


def _leaf(document, path, kind):
    return READER["material_value"](
        document, document["component"], document["scope"],
        READER["canonical_sha256"](document), path, kind
    )


def _one_prefix(document, prefix):
    tokens = READER["decode_json_pointer"](prefix)
    indices = set()
    for material in document["materials"]:
        path = READER["decode_json_pointer"](material["path"])
        if path[:len(tokens)] == tokens:
            require(len(path) > len(tokens) and re.fullmatch(r"0|[1-9][0-9]*", path[len(tokens)]),
                    "invalid policy table index")
            indices.add(path[len(tokens)])
    require(len(indices) == 1, "qualification policy must select exactly one " + prefix)
    return prefix + "/" + next(iter(indices))


def _digest(value):
    READER["validate_canonical_sha256"](value)


def _identity(value, path):
    require(type(value) is dict and set(value) == {"file", "canonical_sha256"}
            and value["file"] == path, "qualification ABI identity differs: " + path)
    _digest(value["canonical_sha256"])
    return value


def _source(value):
    require(type(value) is dict and set(value) == {"status", "url", "sha256", "size", "sigstore"}
            and value["status"] == "locked", "CPython source policy is not locked")
    _digest(value["sha256"])
    require(type(value["size"]) is int and value["size"] > 0 and
            type(value["url"]) is str and value["url"].startswith("https://"), "invalid CPython source identity")
    sigstore = value["sigstore"]
    require(type(sigstore) is dict and set(sigstore) == {
        "verification", "bundle_url", "bundle_sha256", "bundle_size", "bundle_evidence", "identity", "oidc_issuer"
    } and sigstore["verification"] == "verified", "CPython Sigstore policy fields/status differ")
    _digest(sigstore["bundle_sha256"])
    require(type(sigstore["bundle_size"]) is int and sigstore["bundle_size"] > 0,
            "invalid CPython Sigstore bundle size")
    require(all(type(sigstore[key]) is str and sigstore[key] for key in
                ("bundle_url", "bundle_evidence", "identity", "oidc_issuer")), "incomplete CPython Sigstore policy")
    return value


def component_name(version, arch):
    require(arch in ("x86_64", "aarch64"), "unsupported Python qualification architecture")
    try:
        contract = ROWS["contract_for_version"](version)
    except ROWS["ContractError"] as error:
        raise PolicyError(str(error)) from error
    return "python/%s-%s-qualification" % (contract["row"], arch)


def from_documents(root, row_policy, version, arch, trusted_sha256):
    """Authenticate the target and its row policy through one external pin."""
    name = component_name(version, arch)
    try:
        READER["validate_authentic_component"](root, name, "qualification", trusted_sha256)
        contract = ROWS["contract_for_version"](version)
        row = contract["row"]
        policy_name = "implementation/python-%s-qualification-policy" % row
        dependencies = {item["component"]: item for item in root["dependencies"]}
        expected_dependencies = {policy_name, "python/%s-source" % row, "python/%s-%s-build" % (row, arch),
                                 "toolchain/%s-qualification" % arch, "rpm/sysroot-" + arch}
        if contract["zstd"]:
            expected_dependencies.update({"implementation/zstd-build-policy", "zstd/host-build", "zstd/%s-build" % arch})
        require(set(dependencies) == expected_dependencies, "Python qualification dependencies differ")
        READER["validate_authentic_component"](
            row_policy, policy_name, "qualification", dependencies[policy_name]["canonical_sha256"]
        )
        require({item["component"] for item in row_policy["dependencies"]} ==
                {"implementation/python-%s-build-policy" % row}, "Python row policy dependencies differ")
        observed_contract = _object(row_policy, "/@implementation/python_rows/" + row)
        require(READER["canonical_sha256"](observed_contract) == READER["canonical_sha256"](contract),
                "Python row policy differs from the implementation")
        require(len(row_policy["materials"]) == len(contract), "unexpected Python row policy materials")
        python_prefix = _one_prefix(root, "/python/versions")
        require(_leaf(root, python_prefix + "/version", "string") == version and
                _leaf(root, python_prefix + "/adapter", "string") == contract["adapter"],
                "Python qualification row/version differs")
        require(_leaf(root, "/baseline", "string") == "el8", "unsupported Python qualification baseline")
        target = _object(root, _one_prefix(root, "/targets"))
        require(set(target) == {"arch", "triple", "sysroot"} and target["arch"] == arch and
                target["triple"] == arch + "-unknown-linux-gnu", "Python qualification target differs")
        sysroot = target["sysroot"]
        require(type(sysroot) is dict and set(sysroot) == {"status", "lock_file", "canonical_sha256"} and
                sysroot["status"] == "locked" and sysroot["lock_file"] == "locks/sysroot-el8-%s.json" % arch,
                "Python qualification sysroot differs")
        _digest(sysroot["canonical_sha256"])
        abi = {}
        for key, pointer, path in (
            ("provider_manifest", "/abi/provider_manifest", "config/abi-providers.json"),
            ("baseline", "/abi/targets/%s/baseline" % arch, "abi/el8/%s.json" % arch),
            ("sysroot_inventory", "/abi/targets/%s/sysroot_inventory" % arch, "evidence/abi/el8-%s-sysroot.json" % arch),
            ("runtime_provider_policy", "/abi/python/runtime_provider_policy", "config/python-runtime-providers.json"),
            ("provider_catalog", "/abi/python/provider_catalogs/" + arch, "evidence/abi/el8-%s-python-provider-catalog.json" % arch),
        ):
            abi[key] = _identity(_object(root, pointer), path)
        base = {"index_digest": _leaf(root, "/base_image/digest", "string"),
                "manifest_digest": _leaf(root, "/base_image/manifests/" + ("amd64" if arch == "x86_64" else "arm64"), "string")}
        require(all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in base.values()), "invalid runtime image policy")
        executor = {"kind": "native"}
        if arch == "aarch64":
            require(_leaf(root, "/qemu/executor/status", "string") == "locked", "QEMU policy is not locked")
            executor = {key: _leaf(root, "/qemu/executor/" + key, "string")
                        for key in ("binary_sha256", "cpu", "uname_release")}
            executor.update(kind="qemu", version=_leaf(root, "/qemu/version", "string"))
            _digest(executor["binary_sha256"])
            require(all(executor.values()), "incomplete QEMU policy")
        else:
            require(not any(item["path"].startswith("/qemu/") for item in root["materials"]),
                    "x86_64 policy must not select QEMU")
        zstd = None
        if contract["zstd"]:
            require(_leaf(root, "/python/zstd/version", "string") == "1.5.7" and
                    _leaf(root, "/python/zstd/source/status", "string") == "locked", "zstd source/version is not locked")
            zstd = {role: copy.deepcopy(dependencies[dependency]) for role, dependency in (
                ("policy", "implementation/zstd-build-policy"), ("host", "zstd/host-build"), ("target", "zstd/%s-build" % arch)
            )}
        support = _leaf(root, python_prefix + "/support", "string")
        require(support in ("eol", "security", "bugfix", "prerelease"), "unsupported CPython support policy")
        return {"schema_version": 1, "kind": "crossforge-python-qualification-policy",
                "component": {"component": name, "canonical_sha256": trusted_sha256},
                "contract": copy.deepcopy(contract), "version": version, "support": support,
                "source": _source(_object(root, python_prefix + "/source")), "target": target,
                "source_components": {"source": copy.deepcopy(dependencies["python/%s-source" % row]),
                                      "policy": copy.deepcopy(row_policy["dependencies"][0])},
                "abi": abi, "runtime_base": base, "runtime_executor": executor, "zstd_components": zstd,
                "runtime_overlay_binding": dict(kind="release-component", scope="build", **dependencies["rpm/sysroot-" + arch])}
    except (READER["ComponentError"], ROWS["ContractError"]) as error:
        raise PolicyError(str(error)) from error


def load(directory, version, arch, trusted_sha256):
    directory = Path(directory)
    name = component_name(version, arch)
    row = ROWS["contract_for_version"](version)["row"]
    try:
        root = READER["load_component"](directory / (name + ".json"), name, "qualification", trusted_sha256)
        policy_name = "implementation/python-%s-qualification-policy" % row
        # The raw document is authenticated against the root dependency before use.
        row_policy = READER["load_json"](directory / (policy_name + ".json"))
        return from_documents(root, row_policy, version, arch, trusted_sha256)
    except READER["ComponentError"] as error:
        raise PolicyError(str(error)) from error


def from_release(release, version, arch, render_components):
    """Independently derive current expectations; never trust a receipt's pin."""
    try:
        row = ROWS["bind_release"](release, version=version)["contract"]["row"]
    except ROWS["ContractError"] as error:
        raise PolicyError(str(error)) from error
    name = component_name(version, arch)
    documents = render_components(release)
    return from_documents(documents[name], documents["implementation/python-%s-qualification-policy" % row],
                          version, arch, READER["canonical_sha256"](documents[name]))


def binding(policy):
    return {"schema_version": 1, "kind": "crossforge-python-input-binding",
            "policy_sha256": READER["canonical_sha256"](policy)}


def require_binding(report, policy):
    require(READER["canonical_sha256"](report.get("input_binding")) == READER["canonical_sha256"](binding(policy)),
            "Python report input binding differs")
    require("release_sha256" not in report, "scoped Python report cannot claim a complete release binding")


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-components", type=Path, required=True)
    parser.add_argument("--qualification-component-sha256", required=True)
    parser.add_argument("--row", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--arch", required=True)
    arguments = parser.parse_args()
    policy = load(arguments.qualification_components, arguments.version, arguments.arch,
                  arguments.qualification_component_sha256)
    require(policy["contract"]["row"] == arguments.row and
            policy["contract"]["adapter"] == arguments.adapter, "Python qualification row/adapter differs")
    print("verified Python qualification inputs: %s %s" % (arguments.row, arguments.arch))
    return 0


if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(main())
    except (PolicyError, OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)
