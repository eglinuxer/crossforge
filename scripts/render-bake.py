#!/usr/bin/env python3
"""Render the checked-in Bake override from config/release.json."""

import argparse
import json
import re
import runpy
import sys
from pathlib import Path


ROW_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("python_row_contract.py"))
)
ContractError = ROW_CONTRACT["ContractError"]
IMPLEMENTED_ROWS = ROW_CONTRACT["IMPLEMENTED_ROWS"]
LATEST_PHASE = ROW_CONTRACT["LATEST_PHASE"]
bind_python_row = ROW_CONTRACT["bind_release"]
rows_for_phase = ROW_CONTRACT["rows_for_phase"]

PYTHON_TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
COMPONENT_ARGUMENT_RE = re.compile(
    r"^CROSSFORGE_COMPONENT_[A-Z0-9_]+_SHA256\Z"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
TOOLCHAIN_CONTEXT_TARGETS = {
    "crossforge_toolchain_%s_%s" % (arch, role): target
    for arch in ("x86_64", "aarch64")
    for role, target in (
        ("install", "toolchain-%s-build-export" % arch),
        ("test_context", "gcc-%s-test-context-export" % arch),
    )
}


def component_argument_name(component):
    if not isinstance(component, str) or not component:
        raise ValueError("component argument identity must be non-empty text")
    normalized = component.upper().replace("/", "_").replace("-", "_")
    name = "CROSSFORGE_COMPONENT_%s_SHA256" % normalized
    if COMPONENT_ARGUMENT_RE.match(name) is None:
        raise ValueError("component has no safe Bake argument name: %r" % component)
    return name


def component_digest_arguments(repository, release, require_tracked=True):
    """Derive validated per-component identities without a global digest."""
    renderer = runpy.run_path(
        str(repository / "scripts/render-release-components.py")
    )
    try:
        documents = renderer["render_documents"](
            repository=repository,
            release=release,
            implemented_rows=IMPLEMENTED_ROWS,
        )
        if require_tracked:
            drift = renderer["output_drift"](repository, documents)
            if drift:
                raise ValueError(
                    "release component projections are stale: %s"
                    % "; ".join(drift)
                )
    except renderer["ProjectionError"] as error:
        raise ValueError(str(error)) from error

    binding = documents[renderer["BINDING_PATH"]]
    component_documents = {
        document["component"]: document
        for document in documents.values()
        if document.get("kind") == "crossforge-release-component"
    }
    records = binding["components"]
    if {record["component"] for record in records} != set(component_documents):
        raise ValueError("release binding does not cover every component")

    arguments = {}
    owners = {}
    for record in records:
        component = record["component"]
        name = component_argument_name(component)
        if name in arguments:
            raise ValueError(
                "component Bake argument collision: %s and %s"
                % (owners[name], component)
            )
        digest = record["canonical_sha256"]
        if not isinstance(digest, str) or SHA256_RE.match(digest) is None:
            raise ValueError("component has an invalid canonical digest: %s" % component)
        expected = renderer["canonical_sha256"](component_documents[component])
        if digest != expected:
            raise ValueError("component binding digest differs: %s" % component)
        owners[name] = component
        arguments[name] = digest
    return arguments


def main_docker_stage_contract(repository):
    dockerfile = (repository / "docker/Dockerfile").read_text(encoding="utf-8")
    matches = list(
        re.finditer(
            r"^FROM(?:\s+--platform=[^\s]+)?\s+([^\s]+)"
            r"(?:\s+AS\s+([a-zA-Z0-9_.-]+))?\s*$",
            dockerfile,
            re.MULTILINE,
        )
    )
    stages = {}
    for index, match in enumerate(matches):
        name = match.group(2)
        if name is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(
            dockerfile
        )
        block = dockerfile[match.start():end]
        stages[name] = {
            "arguments": set(
                re.findall(
                    r"^ARG\s+(CROSSFORGE_COMPONENT_[A-Z0-9_]+_SHA256)\s*$",
                    block,
                    re.MULTILINE,
                )
            ),
            "dependencies": {
                dependency
                for dependency in set(
                    re.findall(r"(?:--from=|,from=)([a-zA-Z0-9_.-]+)", block)
                )
                | {match.group(1)}
                if dependency in {item.group(2) for item in matches}
            },
            "contexts": (set(re.findall(r"(?:--from=|,from=)([a-zA-Z0-9_.-]+)", block))
                         | {match.group(1)}) & set(TOOLCHAIN_CONTEXT_TARGETS),
        }
    return stages


def main_bake_target_stages(repository):
    hcl = (repository / "docker-bake.hcl").read_text(encoding="utf-8")
    blocks = list(
        re.finditer(
            r'^target\s+"([^"]+)"\s*\{\s*\n(.*?)^\}',
            hcl,
            re.MULTILINE | re.DOTALL,
        )
    )
    external = set()
    inherited = {}
    for match in blocks:
        name = match.group(1)
        body = match.group(2)
        dockerfile = re.search(
            r'^\s*dockerfile\s*=\s*"([^"]+)"\s*$', body, re.MULTILINE
        )
        if dockerfile is not None and dockerfile.group(1) != "docker/Dockerfile":
            external.add(name)
        inherits = re.search(
            r'^\s*inherits\s*=\s*\[(.*?)\]\s*$',
            body,
            re.MULTILINE | re.DOTALL,
        )
        inherited[name] = (
            set(re.findall(r'"([^"]+)"', inherits.group(1)))
            if inherits is not None
            else set()
        )
    changed = True
    while changed:
        changed = False
        for name, parents in inherited.items():
            if name not in external and parents & external:
                external.add(name)
                changed = True
    result = {}
    for match in blocks:
        if match.group(1) in external:
            continue
        target = re.search(
            r'^\s*target\s*=\s*"([^"]+)"\s*$',
            match.group(2),
            re.MULTILINE,
        )
        if target is not None:
            result[match.group(1)] = target.group(1)
    return result


def scoped_main_component_arguments(repository, component_arguments):
    stages = main_docker_stage_contract(repository)
    target_stages = main_bake_target_stages(repository)
    result = {}
    for target, root in target_stages.items():
        if root not in stages:
            raise ValueError("Bake target references an unknown Docker stage: %s" % root)
        pending = [root]
        closure = set()
        arguments = set()
        while pending:
            stage = pending.pop()
            if stage in closure:
                continue
            closure.add(stage)
            arguments.update(stages[stage]["arguments"])
            pending.extend(stages[stage]["dependencies"])
        missing = arguments - set(component_arguments)
        if missing:
            raise ValueError(
                "%s requires unknown component arguments: %s"
                % (target, ", ".join(sorted(missing)))
            )
        if arguments:
            result[target] = {
                name: component_arguments[name] for name in sorted(arguments)
            }
    return result


def scoped_main_toolchain_contexts(repository):
    """Bind only contexts reached by a target; never add producer self-edges."""
    stages = main_docker_stage_contract(repository)
    result = {}
    for target, root in main_bake_target_stages(repository).items():
        pending, visited, contexts = [root], set(), set()
        while pending:
            stage = pending.pop()
            if stage in visited:
                continue
            if stage not in stages:
                raise ValueError("unknown Docker stage: %s" % stage)
            visited.add(stage)
            contexts.update(stages[stage]["contexts"])
            pending.extend(stages[stage]["dependencies"])
        if contexts:
            result[target] = {name: "target:" + TOOLCHAIN_CONTEXT_TARGETS[name]
                              for name in sorted(contexts)}
    return result


def render_zstd_graph(config, targets, component_arguments, rocky_amd64_image):
    version = config["python"]["zstd"]["version"]

    def digest(component):
        name = component_argument_name(component)
        try:
            return component_arguments[name]
        except KeyError as error:
            raise ValueError("missing zstd component digest: %s" % component) from error

    common_args = {
        "ZSTD_VERSION": version,
        "ZSTD_SOURCE_COMPONENT_SHA256": digest("sources/zstd"),
        "ZSTD_BUILD_POLICY_COMPONENT_SHA256": digest(
            "implementation/zstd-build-policy"
        ),
    }
    targets["zstd-source"] = {
        "inherits": ["_zstd_common"],
        "target": "zstd-source",
        "args": {
            "ZSTD_VERSION": version,
            "ZSTD_SOURCE_COMPONENT_SHA256": common_args[
                "ZSTD_SOURCE_COMPONENT_SHA256"
            ],
        },
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image
        },
        "output": ["type=cacheonly"],
    }
    host_args = dict(common_args)
    host_args["ZSTD_BUILD_COMPONENT_SHA256"] = digest("zstd/host-build")
    targets["zstd-host-build"] = {
        "inherits": ["_zstd_common"],
        "target": "zstd-host-build-export",
        "args": host_args,
        "contexts": {
            "crossforge_host_common": "target:host-build-common-locked",
            "crossforge_zstd_source": "target:zstd-source",
        },
        "output": ["type=cacheonly"],
    }
    for arch, triple in PYTHON_TARGETS.items():
        build_component = "zstd/%s-build" % arch
        arguments = dict(common_args)
        arguments.update(
            {
                "ZSTD_TARGET_ARCH": arch,
                "ZSTD_TARGET_TRIPLE": triple,
                "ZSTD_BUILD_COMPONENT": build_component,
                "ZSTD_BUILD_COMPONENT_SHA256": digest(build_component),
            }
        )
        targets["zstd-%s-build" % arch] = {
            "inherits": ["_zstd_common"],
            "target": "zstd-target-build-export",
            "args": arguments,
            "contexts": {
                "crossforge_host_common": "target:host-build-common-locked",
                "crossforge_zstd_source": "target:zstd-source",
                "crossforge_toolchain": (
                    "target:toolchain-%s-build-export" % arch
                ),
            },
            "output": ["type=cacheonly"],
        }


def render_ninja_graph(config, targets, component_arguments):
    ninja = config["host_tools"]["ninja"]
    cmake = config["host_tools"]["cmake"]

    def digest(component):
        argument = component_argument_name(component)
        try:
            return component_arguments[argument]
        except KeyError as error:
            raise ValueError(
                "missing host-tool component digest: %s" % component
            ) from error

    targets["ninja-source"] = {
        "inherits": ["_host_tools_common"],
        "target": "ninja-source-export",
        "args": {
            "NINJA_BINARY_URL": ninja["binary"]["url"],
            "NINJA_SOURCE_URL": ninja["source"]["url"],
            "NINJA_SOURCE_COMPONENT_SHA256": digest("sources/ninja"),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }
    targets["ninja-host-tool"] = {
        "inherits": ["_host_tools_common"],
        "target": "ninja-host-tool-export",
        "args": {
            "NINJA_VERSION": ninja["version"],
            "NINJA_SOURCE_COMPONENT_SHA256": digest("sources/ninja"),
            "NINJA_POLICY_COMPONENT_SHA256": digest(
                "implementation/ninja-host-tool"
            ),
            "NINJA_TOOL_COMPONENT_SHA256": digest("host-tools/ninja"),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified",
            "crossforge_ninja_source": "target:ninja-source",
        },
        "output": ["type=cacheonly"],
    }
    targets["cmake-host-tool"] = {
        "inherits": ["_host_tools_common"],
        "target": "cmake-host-tool-export",
        "args": {
            "CMAKE_VERSION": cmake["version"],
            "CMAKE_BINARY_URL": cmake["binary"]["url"],
            "CMAKE_SOURCE_COMPONENT_SHA256": digest("sources/cmake"),
            "CMAKE_POLICY_COMPONENT_SHA256": digest(
                "implementation/cmake-host-tool"
            ),
            "CMAKE_TOOL_COMPONENT_SHA256": digest("host-tools/cmake"),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified",
            "crossforge_ninja_host_tool": "target:ninja-host-tool",
            "crossforge_cmake_source": "target:cmake-source",
        },
        "output": ["type=cacheonly"],
    }
    targets["cmake-source"] = {
        "inherits": ["_host_tools_common"],
        "target": "cmake-source-export",
        "args": {
            "CMAKE_SOURCE_COMPONENT_SHA256": digest("sources/cmake"),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified",
        },
        "output": ["type=cacheonly"],
    }
    return {
        "phase13-host-tools": {
            "targets": [
                "validate",
                "host-runtime-qualified",
                "ninja-source",
                "ninja-host-tool",
                "cmake-source",
                "cmake-host-tool",
            ]
        }
    }


def render_vcpkg_graph(
    config,
    targets,
    component_arguments,
    contract_policy,
):
    vcpkg = config["vcpkg"]
    source_argument = component_argument_name("sources/vcpkg")
    try:
        source_sha256 = component_arguments[source_argument]
    except KeyError as error:
        raise ValueError("missing vcpkg source component digest") from error
    def digest(component):
        argument = component_argument_name(component)
        try:
            return component_arguments[argument]
        except KeyError as error:
            raise ValueError(
                "missing vcpkg component digest: %s" % component
            ) from error

    patchelf = contract_policy["assets"]["patchelf"]
    targets["vcpkg-contract-assets"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-contract-assets-export",
        "args": {
            "VCPKG_CONTRACT_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-contract-qualification"
            ),
            "VCPKG_PATCHELF_URL": patchelf["url"],
            "VCPKG_PATCHELF_SHA256": patchelf["sha256"],
            "VCPKG_PATCHELF_SHA512": patchelf["sha512"],
            "VCPKG_PATCHELF_SIZE": str(patchelf["size"]),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-upstream-tier1-assets"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-upstream-tier1-assets-export",
        "args": {
            "VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-upstream-tier1-qualification"
            ),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-upstream-tier2-assets"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-upstream-tier2-assets-export",
        "args": {
            "VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-upstream-tier2-qualification"
            ),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-upstream-tier3-assets"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-upstream-tier3-assets-export",
        "args": {
            "VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-upstream-tier3-qualification"
            ),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }

    targets["vcpkg-source"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-source-export",
        "args": {
            "VCPKG_REPOSITORY": vcpkg["repository"],
            "VCPKG_RELEASE_TAG": vcpkg["release"]["tag"],
            "VCPKG_RELEASE_COMMIT": vcpkg["release"]["commit"],
            "VCPKG_TOOL_URL": vcpkg["tool"]["url"],
            "VCPKG_TOOL_SHA256": vcpkg["tool"]["sha256"],
            "VCPKG_TOOL_SIGNATURE_URL": vcpkg["tool"]["signature"][
                "url"
            ],
            "VCPKG_SOURCE_COMPONENT_SHA256": source_sha256,
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }
    targets["sdk-phase13-base"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-sdk-base",
        "args": {
            "VCPKG_SOURCE_COMPONENT_SHA256": source_sha256,
            "VCPKG_INTEGRATION_COMPONENT_SHA256": digest(
                "implementation/vcpkg-integration"
            ),
            "VCPKG_SDK_COMPONENT_SHA256": digest("vcpkg/sdk-build"),
            "TOOLCHAIN_X86_64_QUALIFICATION_COMPONENT_SHA256": digest("toolchain/x86_64-qualification"),
            "TOOLCHAIN_AARCH64_QUALIFICATION_COMPONENT_SHA256": digest("toolchain/aarch64-qualification"),
            "NINJA_TOOL_COMPONENT_SHA256": digest("host-tools/ninja"),
            "CMAKE_TOOL_COMPONENT_SHA256": digest("host-tools/cmake"),
        },
        "contexts": {
            "crossforge_sdk_base": "target:sdk-toolchains-dev",
            "crossforge_cmake_host_tool": "target:cmake-host-tool",
            "crossforge_ninja_host_tool": "target:ninja-host-tool",
            "crossforge_qemu_validated": "target:qemu-aarch64-validated",
            "crossforge_vcpkg_source": "target:vcpkg-source",
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-contract-qualified"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-contract-qualified",
        "args": {
            "VCPKG_CONTRACT_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-contract-qualification"
            ),
            "VCPKG_CONTRACT_QUALIFICATION_COMPONENT_SHA256": digest(
                "vcpkg/contract-qualification"
            ),
        },
        "contexts": {
            "crossforge_vcpkg_contract_assets": "target:vcpkg-contract-assets",
            "crossforge_vcpkg_sdk": "target:sdk-phase13-base",
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-upstream-tier1-qualified"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-upstream-tier1-qualified",
        "args": {
            "VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-upstream-tier1-qualification"
            ),
            "VCPKG_UPSTREAM_TIER1_QUALIFICATION_COMPONENT_SHA256": digest(
                "vcpkg/upstream-tier1-qualification"
            ),
        },
        "contexts": {
            "crossforge_vcpkg_contract": "target:vcpkg-contract-qualified",
            "crossforge_vcpkg_contract_assets": "target:vcpkg-contract-assets",
            "crossforge_vcpkg_upstream_tier1_assets": (
                "target:vcpkg-upstream-tier1-assets"
            ),
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-upstream-tier2-qualified"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-upstream-tier2-qualified",
        "args": {
            "VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-upstream-tier2-qualification"
            ),
            "VCPKG_UPSTREAM_TIER2_QUALIFICATION_COMPONENT_SHA256": digest(
                "vcpkg/upstream-tier2-qualification"
            ),
        },
        "contexts": {
            "crossforge_vcpkg_contract_assets": "target:vcpkg-contract-assets",
            "crossforge_vcpkg_tier1": "target:vcpkg-upstream-tier1-qualified",
            "crossforge_vcpkg_upstream_tier2_assets": (
                "target:vcpkg-upstream-tier2-assets"
            ),
        },
        "output": ["type=cacheonly"],
    }
    targets["vcpkg-upstream-tier3-qualified"] = {
        "inherits": ["_vcpkg_common"],
        "target": "vcpkg-upstream-tier3-qualified",
        "args": {
            "VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256": digest(
                "implementation/vcpkg-upstream-tier3-qualification"
            ),
            "VCPKG_UPSTREAM_TIER3_QUALIFICATION_COMPONENT_SHA256": digest(
                "vcpkg/upstream-tier3-qualification"
            ),
        },
        "contexts": {
            "crossforge_vcpkg_contract_assets": "target:vcpkg-contract-assets",
            "crossforge_vcpkg_tier2": "target:vcpkg-upstream-tier2-qualified",
            "crossforge_vcpkg_upstream_tier3_assets": (
                "target:vcpkg-upstream-tier3-assets"
            ),
        },
        "output": ["type=cacheonly"],
    }
    return {
        "phase13-source": {
            "targets": [
                "validate",
                "host-runtime-qualified",
                "vcpkg-source",
            ]
        },
        "phase13-integration": {
            "targets": [
                "ninja-host-tool",
                "vcpkg-source",
                "sdk-toolchains-dev",
                "sdk-phase13-base",
            ]
        },
        "phase13-contract": {
            "targets": ["vcpkg-contract-qualified"]
        },
        "phase13-ports": {
            "targets": ["vcpkg-upstream-tier3-qualified"]
        },
    }


def render_packaging_graph(
    config,
    targets,
    component_arguments,
    qualification_policy,
):
    nfpm = config["nfpm"]

    def digest(component):
        argument = component_argument_name(component)
        try:
            return component_arguments[argument]
        except KeyError as error:
            raise ValueError(
                "missing packaging component digest: %s" % component
            ) from error

    debian = qualification_policy["deb_test_image"]
    debian_image = "%s:%s@%s" % (
        debian["repository"],
        debian["tag"],
        debian["amd64_manifest"],
    )
    targets["nfpm-tool"] = {
        "inherits": ["_packaging_common"],
        "target": "nfpm-tool-export",
        "args": {
            "NFPM_BINARY_URL": nfpm["binary"]["url"],
            "NFPM_SOURCE_URL": nfpm["source"]["archive"]["url"],
            "NFPM_CHECKSUMS_URL": nfpm["checksums"]["url"],
            "NFPM_SIGSTORE_URL": nfpm["sigstore"]["url"],
            "NFPM_SOURCE_COMPONENT_SHA256": digest("sources/nfpm"),
        },
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified"
        },
        "output": ["type=cacheonly"],
    }
    targets["packaging-sdk-dev"] = {
        "inherits": ["_packaging_common"],
        "target": "packaging-sdk",
        "args": {
            "NFPM_VERSION": nfpm["version"],
            "NFPM_BINARY_SHA256": nfpm["binary"]["extracted_sha256"],
            "NFPM_SOURCE_COMPONENT_SHA256": digest("sources/nfpm"),
            "CROSSPACK_IMPLEMENTATION_COMPONENT_SHA256": digest(
                "implementation/crosspack"
            ),
            "CROSSFORGE_LAUNCHER_COMPONENT_SHA256": digest(
                "implementation/launcher"
            ),
            "CROSSPACK_SDK_COMPONENT_SHA256": digest(
                "packaging/sdk-build"
            ),
        },
        "contexts": {
            "crossforge_sdk_base": "target:sdk-phase13-base",
            "crossforge_nfpm_tool": "target:nfpm-tool",
        },
        "output": ["type=cacheonly"],
    }
    targets["packaging-qualified"] = {
        "inherits": ["_packaging_common"],
        "target": "packaging-qualified",
        "args": {
            "NFPM_SOURCE_COMPONENT_SHA256": digest("sources/nfpm"),
            "CROSSPACK_IMPLEMENTATION_COMPONENT_SHA256": digest(
                "implementation/crosspack"
            ),
            "CROSSFORGE_LAUNCHER_COMPONENT_SHA256": digest(
                "implementation/launcher"
            ),
            "CROSSPACK_SDK_COMPONENT_SHA256": digest(
                "packaging/sdk-build"
            ),
            "CROSSPACK_QUALIFICATION_POLICY_COMPONENT_SHA256": digest(
                "implementation/crosspack-qualification"
            ),
            "CROSSPACK_QUALIFICATION_COMPONENT_SHA256": digest(
                "packaging/qualification"
            ),
        },
        "contexts": {
            "crossforge_packaging_sdk": "target:packaging-sdk-dev",
            "crossforge_debian": "docker-image://%s" % debian_image,
        },
        "output": ["type=cacheonly"],
    }
    targets["sdk-complete-dev"] = {
        "inherits": ["_packaging_common"],
        "target": "sdk-complete-dev",
        "args": {
            "COMPLETE_SDK_POLICY_COMPONENT_SHA256": digest(
                "implementation/complete-sdk-qualification"
            ),
            "CROSSFORGE_LAUNCHER_COMPONENT_SHA256": digest(
                "implementation/launcher"
            ),
            "CROSSPACK_QUALIFICATION_COMPONENT_SHA256": digest(
                "packaging/qualification"
            ),
            "PYTHON_QUALIFICATION_COMPONENT_SHA256": digest(
                "python/qualification"
            ),
            "COMPLETE_SDK_QUALIFICATION_COMPONENT_SHA256": digest(
                "product/sdk-qualification"
            ),
        },
        "contexts": {
            "crossforge_packaging_qualified": "target:packaging-qualified",
            "crossforge_python_sdk": "target:python-dev",
        },
        "output": ["type=cacheonly"],
    }
    targets["sdk-candidate"] = {
        "inherits": ["sdk-complete-dev"],
        "target": "sdk-candidate",
        "args": {
            "CROSSFORGE_PRODUCT_VERSION": config["product"]["version"],
            "CROSSFORGE_PRODUCT_IDENTITY_SHA256": digest("product/identity"),
            "CROSSFORGE_COMPONENT_TOOLCHAIN_GCC_TESTSUITE_QUALIFICATION_SHA256": (
                digest("toolchain/gcc-testsuite-qualification")
            ),
        },
        "contexts": {
            "crossforge_gcc_testsuite_full_qualified": (
                "target:gcc-testsuite-full-qualification-evidence"
            ),
            "crossforge_gcc_testsuite_smoke_qualified": (
                "target:gcc-testsuite-smoke-evidence"
            ),
            "crossforge_vcpkg_qualified": (
                "target:vcpkg-upstream-tier3-qualified"
            ),
            "crossforge_sigstore_qualified": (
                "target:sigstore-sources-qualified"
            ),
        },
        "output": ["type=cacheonly"],
    }
    return {
        "phase14-source": {
            "targets": ["host-runtime-qualified", "nfpm-tool"]
        },
        "phase14-sdk": {
            "targets": ["sdk-phase13-base", "nfpm-tool", "packaging-sdk-dev"]
        },
        "phase14": {"targets": ["packaging-qualified"]},
        "phase15": {"targets": ["sdk-complete-dev"]},
        "candidate": {"targets": ["sdk-candidate"]},
    }


def python_row(contract, entry):
    version = entry["version"]
    return {
        "row": contract["row"],
        "minor": contract["minor"],
        "version": version,
        "adapter": contract["adapter"],
        "zstd": contract["zstd"],
        "introduced_phase": contract["introduced_phase"],
    }


def cacheonly_python_target(target, row, contexts=None, extra_args=None):
    arguments = {
        "CPYTHON_ROW": row["row"],
        "CPYTHON_MINOR": row["minor"],
        "CPYTHON_VERSION": row["version"],
        "CPYTHON_ADAPTER": row["adapter"],
        "CPYTHON_SOURCE_COMPONENT": row["source_component"],
        "CPYTHON_SOURCE_COMPONENT_SHA256": row["source_component_sha256"],
        "CPYTHON_BUILD_POLICY_COMPONENT": row["build_policy_component"],
        "CPYTHON_BUILD_POLICY_COMPONENT_SHA256": row[
            "build_policy_component_sha256"
        ],
    }
    if extra_args:
        arguments.update(extra_args)
    result = {
        "inherits": ["_python_common"],
        "target": target,
        "args": arguments,
        "output": ["type=cacheonly"],
    }
    if contexts:
        result["contexts"] = contexts
    return result


def render_python_graph(config, targets, component_arguments):
    rows = []
    zstd_version = config["python"]["zstd"]["version"]
    for record in IMPLEMENTED_ROWS:
        try:
            binding = bind_python_row(config, row=record["row"])
        except ContractError as error:
            raise ValueError(str(error)) from error
        row = python_row(binding["contract"], binding["entry"])
        source_component = "python/%s-source" % row["row"]
        source_argument = component_argument_name(source_component)
        try:
            source_digest = component_arguments[source_argument]
        except KeyError as error:
            raise ValueError(
                "Python row lacks a source component digest: %s" % row["row"]
            ) from error
        row["source_component"] = source_component
        row["source_component_sha256"] = source_digest
        build_policy_component = (
            "implementation/python-%s-build-policy" % row["row"]
        )
        build_policy_argument = component_argument_name(
            build_policy_component
        )
        try:
            build_policy_digest = component_arguments[build_policy_argument]
        except KeyError as error:
            raise ValueError(
                "Python row lacks a build-policy component digest: %s"
                % row["row"]
            ) from error
        row["build_policy_component"] = build_policy_component
        row["build_policy_component_sha256"] = build_policy_digest
        source = binding["entry"]["source"]
        if source["status"] != "locked":
            raise ValueError(
                "enabled CPython row is not source-locked: %s" % row["minor"]
            )
        patches = binding["entry"].get("patches", [])
        if not isinstance(patches, list):
            raise ValueError("Python row patch list is not an array")
        expected_prefix = "patches/cpython/%s/" % row["minor"]
        if any(
            not isinstance(patch, dict)
            or not isinstance(patch.get("file"), str)
            or not patch["file"].startswith(expected_prefix)
            for patch in patches
        ):
            raise ValueError(
                "Python row patch escapes its minor context: %s" % row["row"]
            )
        row["patch_context_target"] = (
            "cpython-patches-%s" % row["row"] if patches else None
        )
        row["patch_context"] = "target:%s" % (
            row["patch_context_target"] or "cpython-empty-patches"
        )
        row["zstd_version"] = zstd_version if row["zstd"] else "none"
        rows.append(row)

    phase_order = [row["introduced_phase"] for row in rows]
    if phase_order != sorted(phase_order):
        raise ValueError("Python rows must be introduced in append-only phase order")

    release_targets = {
        item["arch"]: item["triple"] for item in config["targets"]
    }
    if release_targets != PYTHON_TARGETS:
        raise ValueError("release targets differ from the Python matrix contract")

    base = config["base_image"]
    rocky_amd64_context = "docker-image://%s:%s@%s" % (
        base["repository"],
        base["tag"],
        base["manifests"]["amd64"],
    )

    targets["cpython-empty-patches"] = {
        "inherits": ["_python_common"],
        "target": "cpython-empty-patches",
        "contexts": {"crossforge_rocky_amd64": rocky_amd64_context},
        "output": ["type=cacheonly"],
    }
    targets["zstd-empty"] = {
        "inherits": ["_python_common"],
        "target": "zstd-empty",
        "contexts": {"crossforge_rocky_amd64": rocky_amd64_context},
        "output": ["type=cacheonly"],
    }
    for row in rows:
        if row["patch_context_target"] is None:
            continue
        targets[row["patch_context_target"]] = {
            "inherits": ["_python_common"],
            "target": "cpython-patch-context",
            "contexts": {
                "crossforge_cpython_patch_files": "patches/cpython/%s"
                % row["minor"]
            },
            "output": ["type=cacheonly"],
        }

    groups = {}
    for row in rows:
        row_name = row["row"]
        source_name = "cpython-source-%s" % row_name
        prepared_name = "cpython-prepared-%s" % row_name
        build_name = "cpython-build-%s" % row_name
        build_export_name = build_name + "-export"
        export_name = "python-row-%s" % row_name
        dev_name = "python-%s-dev" % row_name

        targets[source_name] = cacheonly_python_target(
            "cpython-source",
            row,
            {"crossforge_rocky_amd64": rocky_amd64_context},
        )
        targets[prepared_name] = cacheonly_python_target(
            "cpython-prepared",
            row,
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_source": "target:%s" % source_name,
                "crossforge_cpython_patches": row["patch_context"],
            },
        )
        targets[build_name] = cacheonly_python_target(
            "cpython-build",
            row,
            {
                "crossforge_cpython_prepared": "target:%s" % prepared_name,
                "crossforge_zstd": (
                    "target:zstd-host-build"
                    if row["zstd"]
                    else "target:zstd-empty"
                ),
            },
            {"CPYTHON_ZSTD_VERSION": row["zstd_version"]},
        )

        qualification_names = []
        targets[build_export_name] = cacheonly_python_target(
            "cpython-build-export", row, {"crossforge_cpython_build_output": "target:" + build_name})
        final_qualification = {}
        for arch, triple in PYTHON_TARGETS.items():
            cross_name = "cpython-cross-%s-%s" % (row_name, arch)
            qualify_build_name = "cpython-%s-%s-qualify-build" % (
                row_name,
                arch,
            )
            qualify_name = "cpython-%s-%s-qualify" % (row_name, arch)
            target_args = {
                "CROSSFORGE_TARGET_ARCH": arch,
                "CROSSFORGE_TARGET_TRIPLE": triple,
            }
            targets[cross_name] = cacheonly_python_target(
                "cpython-cross",
                row,
                {
                    "crossforge_host_python": "target:host-python-build-locked",
                    "crossforge_cpython_prepared": "target:%s" % prepared_name,
                    "crossforge_cpython_build": "target:%s" % build_export_name,
                    "crossforge_toolchain": (
                        "target:toolchain-%s-build-export" % arch
                    ),
                    "crossforge_zstd": (
                        "target:zstd-%s-build" % arch
                        if row["zstd"]
                        else "target:zstd-empty"
                    ),
                },
                dict(
                    target_args,
                    CPYTHON_ZSTD_VERSION=row["zstd_version"],
                ),
            )
            install_name = cross_name + "-export"
            test_context_name = "cpython-%s-%s-test-context-export" % (row_name, arch)
            for name, stage in ((install_name, "cpython-cross-export"),
                                (test_context_name, "cpython-test-context-export")):
                targets[name] = cacheonly_python_target(stage, row,
                    {"crossforge_cpython_cross_output": "target:" + cross_name}, target_args)
            qualification_component = "python/%s-%s-qualification" % (row_name, arch)
            try:
                qualification_digest = component_arguments[component_argument_name(qualification_component)]
            except KeyError as error:
                raise ValueError("missing Python qualification component digest: %s" % qualification_component) from error
            targets[qualify_build_name] = cacheonly_python_target(
                "cpython-qualify-build",
                row,
                {"crossforge_host_python": "target:host-python-build-locked",
                 "crossforge_toolchain": "target:toolchain-%s-build-export" % arch,
                 "crossforge_cpython_build": "target:" + build_export_name,
                 "crossforge_cpython_install": "target:" + install_name,
                 "crossforge_cpython_test_context": "target:" + test_context_name},
                dict(target_args, CPYTHON_QUALIFICATION_COMPONENT_SHA256=qualification_digest),
            )
            runtime_contexts = {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_qualify_build": (
                    "target:%s" % qualify_build_name
                ),
                "crossforge_sysroot": "target:sysroot-%s" % arch,
                "crossforge_clean_runtime": (
                    "target:python-runtime-clean-%s" % arch
                ),
            }
            if arch == "aarch64":
                runtime_contexts["crossforge_qemu_validated"] = (
                    "target:qemu-aarch64-validated"
                )
            targets[qualify_name] = cacheonly_python_target(
                "cpython-qualify-%s" % arch,
                row,
                runtime_contexts,
                dict(target_args, CPYTHON_QUALIFICATION_COMPONENT_SHA256=qualification_digest),
            )
            qualification_names.append(qualify_name)
            final_qualification[arch] = qualify_name

        row_qualification_component = "python/%s-qualification" % row_name
        try:
            row_qualification_digest = component_arguments[component_argument_name(row_qualification_component)]
        except KeyError as error:
            raise ValueError("missing Python row qualification component digest: %s" % row_qualification_component) from error
        targets[export_name] = cacheonly_python_target(
            "cpython-row-export",
            row,
            {
                "crossforge_host_python": "target:host-python-build-locked",
                "crossforge_cpython_build": "target:%s" % build_export_name,
                "crossforge_cpython_x86_64": (
                    "target:%s" % final_qualification["x86_64"]
                ),
                "crossforge_cpython_aarch64": (
                    "target:%s" % final_qualification["aarch64"]
                ),
            },
            {"CPYTHON_ROW_QUALIFICATION_COMPONENT_SHA256": row_qualification_digest},
        )
        targets[dev_name] = cacheonly_python_target(
            "python-sdk-append",
            row,
            {
                "crossforge_sdk_base": "target:sdk-toolchains-dev",
                "crossforge_python_row": "target:%s" % export_name,
            },
            {"CPYTHON_ROW_QUALIFICATION_COMPONENT_SHA256": row_qualification_digest},
        )
        groups["python-%s" % row_name] = {
            "targets": [prepared_name, build_name]
            + qualification_names
            + [export_name, dev_name]
        }

    targets["sdk-toolchains-dev"] = {
        "inherits": ["_python_common"],
        "target": "sdk-toolchains-dev",
        "contexts": {
            "crossforge_host_runtime": "target:host-runtime-qualified",
            "crossforge_toolchain_x86_64": "target:toolchain-x86_64-dev",
            "crossforge_toolchain_aarch64": "target:toolchain-aarch64-dev",
        },
        "output": ["type=cacheonly"],
    }

    rows_by_name = {row["row"]: row for row in rows}
    aggregate_base = "sdk-toolchains-dev"
    append_targets = {}
    for row in rows:
        append_name = "python-dev-append-%s" % row["row"]
        targets[append_name] = cacheonly_python_target(
            "python-sdk-append",
            row,
            {
                "crossforge_sdk_base": "target:%s" % aggregate_base,
                "crossforge_python_row": "target:python-row-%s" % row["row"],
            },
            {"CPYTHON_ROW_QUALIFICATION_COMPONENT_SHA256": component_arguments[
                component_argument_name("python/%s-qualification" % row["row"])]},
        )
        aggregate_base = append_name
        append_targets[row["row"]] = append_name

    introduced_phases = sorted(
        {row["introduced_phase"] for row in rows}
    )
    for phase in introduced_phases:
        phase_row_names = rows_for_phase(phase)
        if not phase_row_names:
            raise ValueError("Python phase %d has no rows" % phase)
        try:
            phase_rows = [rows_by_name[name] for name in phase_row_names]
        except KeyError as error:
            raise ValueError(
                "Python phase %d references an unknown row: %s"
                % (phase, error.args[0])
            ) from error
        native_targets = [
            "cpython-build-%s" % row["row"] for row in phase_rows
        ]
        groups["python-native-phase%d" % phase] = {
            "targets": native_targets
        }
        snapshot_name = "python-phase%d-dev" % phase
        snapshot_base = append_targets[phase_rows[-1]["row"]]
        targets[snapshot_name] = {
            "inherits": ["_python_common"],
            "target": "python-sdk-final",
            "args": {
                "CROSSFORGE_PYTHON_ROWS": " ".join(
                    row["row"] for row in phase_rows
                )
            },
            "contexts": {
                "crossforge_sdk_base": "target:%s" % snapshot_base,
                "crossforge_qemu_validated": "target:qemu-aarch64-validated",
            },
            "output": ["type=cacheonly"],
        }
        qualification_targets = [
            "cpython-%s-%s-qualify" % (row["row"], arch)
            for row in phase_rows
            for arch in PYTHON_TARGETS
        ]
        groups["phase%d" % phase] = {
            "targets": [
                "validate",
                "platform-python-check",
                "host-python-build-locked",
                *native_targets,
                "python-runtime-clean-x86_64",
                "python-runtime-clean-aarch64",
                *qualification_targets,
                snapshot_name,
            ]
        }

    latest_row_names = rows_for_phase(LATEST_PHASE)
    if tuple(row["row"] for row in rows) != latest_row_names:
        raise ValueError("latest Python phase differs from implemented row order")
    groups["python-native-latest"] = {
        "targets": ["cpython-build-%s" % row for row in latest_row_names]
    }
    targets["python-dev"] = {
        "inherits": ["_python_common"],
        "target": "python-sdk-final",
        "args": {
            "CROSSFORGE_PYTHON_ROWS": " ".join(latest_row_names)
        },
        "contexts": {
            "crossforge_sdk_base": "target:%s" % aggregate_base,
            "crossforge_qemu_validated": "target:qemu-aarch64-validated",
        },
        "output": ["type=cacheonly"],
    }
    groups["python-matrix"] = {"targets": ["python-dev"]}
    return groups


def render_qt_graph(
    config, qt_plan, targets, component_arguments, rocky_amd64_image
):
    component_argument = component_argument_name("sources/qt")
    ffmpeg_component_argument = component_argument_name("sources/ffmpeg")
    xcb_cursor_component_argument = component_argument_name(
        "sources/xcb-util-cursor"
    )
    try:
        component_sha256 = component_arguments[component_argument]
        ffmpeg_component_sha256 = component_arguments[ffmpeg_component_argument]
        xcb_cursor_component_sha256 = component_arguments[
            xcb_cursor_component_argument
        ]
        qualification_component_argument = component_argument_name(
            "future/qt-qualification"
        )
        qualification_component_sha256 = component_arguments[
            qualification_component_argument
        ]
        runtime_qualification_component_argument = component_argument_name(
            "future/qt-runtime-qualification"
        )
        runtime_qualification_component_sha256 = component_arguments[
            runtime_qualification_component_argument
        ]
    except KeyError as error:
        raise ValueError("missing Qt source dependency component digest") from error
    if len(qt_plan["patches"]) != 1:
        raise ValueError("Qt target patch contract differs")
    qt_target_patch = qt_plan["patches"][0]
    targets["qt-source"] = {
        "inherits": ["_qt_common"],
        "target": "qt-source-export",
        "args": {
            "QT_VERSION": config["qt"]["version"],
            "QT_SOURCE_URL": config["qt"]["source"]["url"],
            component_argument: component_sha256,
        },
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image,
        },
        "output": ["type=cacheonly"],
    }
    ffmpeg = config["qt"]["dependencies"]["ffmpeg"]
    targets["ffmpeg-source"] = {
        "inherits": ["_qt_common"],
        "target": "ffmpeg-source-export",
        "args": {
            "FFMPEG_VERSION": ffmpeg["version"],
            "FFMPEG_SOURCE_URL": ffmpeg["source"]["url"],
            ffmpeg_component_argument: ffmpeg_component_sha256,
        },
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image,
        },
        "output": ["type=cacheonly"],
    }
    xcb_cursor = config["qt"]["dependencies"]["xcb_util_cursor"]
    targets["xcb-util-cursor-source"] = {
        "inherits": ["_qt_common"],
        "target": "xcb-util-cursor-source-export",
        "args": {
            "XCB_UTIL_CURSOR_VERSION": xcb_cursor["version"],
            "XCB_UTIL_CURSOR_SOURCE_URL": xcb_cursor["source"]["url"],
            xcb_cursor_component_argument: xcb_cursor_component_sha256,
        },
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image,
        },
        "output": ["type=cacheonly"],
    }
    xcb_build_arguments = {
        xcb_cursor_component_argument: xcb_cursor_component_sha256,
        qualification_component_argument: qualification_component_sha256,
    }
    targets["xcb-util-cursor-host-build"] = {
        "inherits": ["_qt_common"],
        "target": "xcb-util-cursor-host-build",
        "args": xcb_build_arguments,
        "contexts": {
            "crossforge_host_qt": "target:host-qt-build-locked",
            "crossforge_xcb_util_cursor_source": (
                "target:xcb-util-cursor-source"
            ),
        },
        "output": ["type=cacheonly"],
    }
    xcb_builds = ["xcb-util-cursor-host-build"]
    for target in config["targets"]:
        arch = target["arch"]
        triple = target["triple"]
        name = "xcb-util-cursor-%s-build" % arch
        targets[name] = {
            "inherits": ["_qt_common"],
            "target": "xcb-util-cursor-target-build",
            "args": dict(
                xcb_build_arguments,
                XCB_UTIL_CURSOR_TARGET_ARCH=arch,
                XCB_UTIL_CURSOR_TARGET_TRIPLE=triple,
                XCB_UTIL_CURSOR_RPM_LOCK=(
                    "locks/qt-target-el8-%s.json" % arch
                ),
                XCB_UTIL_CURSOR_RPM_TRANSACTION=(
                    "locks/transactions/qt-target-el8-%s.json" % arch
                ),
            ),
            "contexts": {
                "crossforge_host_qt": "target:host-qt-build-locked",
                "crossforge_qt_target": "target:qt-target-%s-locked" % arch,
                "crossforge_toolchain": "target:toolchain-%s-dev" % arch,
                "crossforge_xcb_util_cursor_source": (
                    "target:xcb-util-cursor-source"
                ),
            },
            "output": ["type=cacheonly"],
        }
        xcb_builds.append(name)
    ffmpeg_build_arguments = {
        ffmpeg_component_argument: ffmpeg_component_sha256,
        qualification_component_argument: qualification_component_sha256,
    }
    targets["ffmpeg-host-build"] = {
        "inherits": ["_qt_common"],
        "target": "ffmpeg-host-build",
        "args": dict(
            ffmpeg_build_arguments,
            FFMPEG_VERSION=config["qt"]["dependencies"]["ffmpeg"]["version"],
        ),
        "contexts": {
            "crossforge_ffmpeg_source": "target:ffmpeg-source",
            "crossforge_xcb_host": "target:xcb-util-cursor-host-build",
        },
        "output": ["type=cacheonly"],
    }
    targets["ffmpeg-host-build-observation"] = {
        "inherits": ["_qt_common"],
        "target": "ffmpeg-host-build-observation",
        "args": dict(targets["ffmpeg-host-build"]["args"]),
        "contexts": dict(targets["ffmpeg-host-build"]["contexts"]),
        "output": ["type=cacheonly"],
    }
    ffmpeg_builds = ["ffmpeg-host-build"]
    for target in config["targets"]:
        arch = target["arch"]
        triple = target["triple"]
        name = "ffmpeg-%s-build" % arch
        targets[name] = {
            "inherits": ["_qt_common"],
            "target": "ffmpeg-target-build",
            "args": dict(
                ffmpeg_build_arguments,
                FFMPEG_VERSION=config["qt"]["dependencies"]["ffmpeg"][
                    "version"
                ],
                FFMPEG_TARGET_ARCH=arch,
                FFMPEG_TARGET_TRIPLE=triple,
                FFMPEG_RPM_LOCK="locks/qt-target-el8-%s.json" % arch,
                FFMPEG_RPM_TRANSACTION=(
                    "locks/transactions/qt-target-el8-%s.json" % arch
                ),
            ),
            "contexts": {
                "crossforge_ffmpeg_source": "target:ffmpeg-source",
                "crossforge_host_qt": "target:host-qt-build-locked",
                "crossforge_qt_target": "target:qt-target-%s-locked" % arch,
                "crossforge_toolchain": "target:toolchain-%s-dev" % arch,
            },
            "output": ["type=cacheonly"],
        }
        ffmpeg_builds.append(name)
    targets["qt-host-configure-qualified"] = {
        "inherits": ["_qt_common"],
        "target": "qt-host-configure-qualified",
        "args": {
            "QT_VERSION": config["qt"]["version"],
            qualification_component_argument: qualification_component_sha256,
        },
        "contexts": {
            "crossforge_cmake": "target:cmake-host-tool",
            "crossforge_ffmpeg_host": "target:ffmpeg-host-build",
            "crossforge_ninja": "target:ninja-host-tool",
            "crossforge_qt_source": "target:qt-source",
        },
        "output": ["type=cacheonly"],
    }
    targets["qt-host-configure-evidence"] = {
        "inherits": ["_qt_common"],
        "target": "qt-host-configure-evidence",
        "args": dict(targets["qt-host-configure-qualified"]["args"]),
        "contexts": dict(targets["qt-host-configure-qualified"]["contexts"]),
        "output": ["type=cacheonly"],
    }
    targets["qt-host-webengine-build"] = {
        "inherits": ["_qt_common"],
        "target": "qt-host-webengine-build",
        "args": dict(targets["qt-host-configure-qualified"]["args"]),
        "contexts": dict(targets["qt-host-configure-qualified"]["contexts"]),
        "output": ["type=cacheonly"],
    }
    targets["qt-host-build"] = {
        "inherits": ["_qt_common"],
        "target": "qt-host-install-checked",
        "args": dict(targets["qt-host-configure-qualified"]["args"]),
        "contexts": dict(targets["qt-host-configure-qualified"]["contexts"]),
        "output": ["type=cacheonly"],
    }
    targets["qt-host-qualified"] = {
        "inherits": ["_qt_common"],
        "target": "qt-host-qualified",
        "args": dict(targets["qt-host-configure-qualified"]["args"]),
        "contexts": dict(targets["qt-host-configure-qualified"]["contexts"]),
        "output": ["type=cacheonly"],
    }
    targets["qt-host-qualification-evidence"] = {
        "inherits": ["_qt_common"],
        "target": "qt-host-qualification-evidence",
        "args": dict(targets["qt-host-qualified"]["args"]),
        "contexts": dict(targets["qt-host-qualified"]["contexts"]),
        "output": ["type=cacheonly"],
    }
    qt_target_configures = []
    qt_target_configure_qualifications = []
    qt_target_webengine_builds = []
    qt_target_builds = []
    qt_target_qualifications = []
    qt_runtime_overlay_qualifications = []
    qt_target_runtime_qualifications = []
    for target in config["targets"]:
        arch = target["arch"]
        triple = target["triple"]
        name = "qt-%s-configure-observation" % arch
        targets[name] = {
            "inherits": ["_qt_common"],
            "target": "qt-target-configure-observation",
            "args": {
                "QT_VERSION": config["qt"]["version"],
                "QT_TARGET_ARCH": arch,
                "QT_TARGET_TRIPLE": triple,
                "QT_XNNPACK_PATCH_SHA256": qt_target_patch["sha256"],
                qualification_component_argument: qualification_component_sha256,
            },
            "contexts": {
                "crossforge_cmake": "target:cmake-host-tool",
                "crossforge_ffmpeg_target": "target:ffmpeg-%s-build" % arch,
                "crossforge_host_qt": "target:host-qt-build-locked",
                "crossforge_ninja": "target:ninja-host-tool",
                "crossforge_qt_host": "target:qt-host-qualified",
                "crossforge_qt_source": "target:qt-source",
                "crossforge_toolchain": "target:toolchain-%s-dev" % arch,
                "crossforge_xcb_target": (
                    "target:xcb-util-cursor-%s-build" % arch
                ),
            },
            "output": ["type=cacheonly"],
        }
        qt_target_configures.append(name)
        configure_qualification_name = "qt-%s-configure-qualified" % arch
        targets[configure_qualification_name] = {
            "inherits": ["_qt_common"],
            "target": "qt-target-configure-evidence",
            "args": dict(targets[name]["args"]),
            "contexts": dict(targets[name]["contexts"]),
            "output": ["type=cacheonly"],
        }
        qt_target_configure_qualifications.append(
            configure_qualification_name
        )
        webengine_build_name = "qt-%s-webengine-build" % arch
        targets[webengine_build_name] = {
            "inherits": ["_qt_common"],
            "target": "qt-target-webengine-build",
            "args": dict(targets[name]["args"]),
            "contexts": dict(targets[name]["contexts"]),
            "output": ["type=cacheonly"],
        }
        qt_target_webengine_builds.append(webengine_build_name)
        build_name = "qt-%s-build" % arch
        targets[build_name] = {
            "inherits": ["_qt_common"],
            "target": "qt-target-build-observation",
            "args": dict(targets[name]["args"]),
            "contexts": dict(targets[name]["contexts"]),
            "output": ["type=cacheonly"],
        }
        qt_target_builds.append(build_name)
        qualification_name = "qt-%s-qualified" % arch
        targets[qualification_name] = {
            "inherits": ["_qt_common"],
            "target": "qt-target-qualification-evidence",
            "args": dict(targets[name]["args"]),
            "contexts": dict(targets[name]["contexts"]),
            "output": ["type=cacheonly"],
        }
        qt_target_qualifications.append(qualification_name)
        qualified_root_name = "qt-%s-build-qualified-root" % arch
        targets[qualified_root_name] = {
            "inherits": ["_qt_common"],
            "target": "qt-target-build-qualified",
            "args": dict(targets[name]["args"]),
            "contexts": dict(targets[name]["contexts"]),
            "output": ["type=cacheonly"],
        }
        runtime_overlay_name = "qt-%s-runtime-overlay-qualified" % arch
        oci_arch = "amd64" if arch == "x86_64" else "arm64"
        rocky = config["base_image"]
        rocky_target_image = "%s:%s@%s" % (
            rocky["repository"],
            rocky["tag"],
            rocky["manifests"][oci_arch],
        )
        runtime_overlay_arguments = {
            "QT_TARGET_ARCH": arch,
            "ROCKY_TARGET_MANIFEST_DIGEST": rocky["manifests"][oci_arch],
            runtime_qualification_component_argument: (
                runtime_qualification_component_sha256
            ),
        }
        runtime_overlay_contexts = {
            "crossforge_host_qt": "target:host-qt-build-locked",
            "crossforge_qt_runtime_rpms": (
                "target:qt-runtime-rpms-%s" % arch
            ),
            "crossforge_rocky_target": (
                "docker-image://%s" % rocky_target_image
            ),
        }
        targets[runtime_overlay_name] = {
            "inherits": ["_qt_common"],
            "dockerfile": "docker/qt-runtime.Dockerfile",
            "target": "qt-runtime-overlay-evidence",
            "args": runtime_overlay_arguments,
            "contexts": runtime_overlay_contexts,
            "output": ["type=cacheonly"],
        }
        qt_runtime_overlay_qualifications.append(runtime_overlay_name)
        runtime_name = "qt-%s-runtime-qualified" % arch
        qemu = config["qemu"]["executor"]
        runtime_arguments = {
            "QT_TARGET_ARCH": arch,
            "QT_TARGET_TRIPLE": triple,
        }
        runtime_arguments.update(runtime_overlay_arguments)
        runtime_contexts = dict(runtime_overlay_contexts)
        runtime_contexts["crossforge_qt_target"] = (
            "target:%s" % qualified_root_name
        )
        if arch == "aarch64":
            runtime_arguments.update(
                {
                    "QEMU_EXECUTOR_CPU": qemu["cpu"],
                    "QEMU_EXECUTOR_UNAME_RELEASE": qemu["uname_release"],
                }
            )
            runtime_contexts["crossforge_qemu_validated"] = (
                "target:qemu-aarch64-validated"
            )
        targets[runtime_name] = {
            "inherits": ["_qt_common"],
            "dockerfile": "docker/qt-runtime.Dockerfile",
            "target": "qt-target-runtime-%s-evidence" % arch,
            "args": runtime_arguments,
            "contexts": runtime_contexts,
            "output": ["type=cacheonly"],
        }
        qt_target_runtime_qualifications.append(runtime_name)
        if arch == "aarch64":
            targets["qt-aarch64-native-runtime-root"] = {
                "inherits": ["_qt_common"],
                "dockerfile": "docker/qt-runtime.Dockerfile",
                "target": "qt-native-runtime-root",
                "args": {
                    key: value
                    for key, value in runtime_arguments.items()
                    if not key.startswith("QEMU_")
                },
                "contexts": {
                    key: value
                    for key, value in runtime_contexts.items()
                    if key != "crossforge_qemu_validated"
                },
                "output": ["type=cacheonly"],
            }
    return {
        "qt-source-qualified": {
            "targets": ["qt-source", "ffmpeg-source", "xcb-util-cursor-source"]
        },
        "xcb-util-cursor-qualified": {"targets": xcb_builds},
        "ffmpeg-qualified": {"targets": ffmpeg_builds},
        "qt-host-configure-qualified": {
            "targets": ["qt-host-configure-evidence"]
        },
        "qt-host-built": {"targets": ["qt-host-build"]},
        "qt-host-qualified": {
            "targets": ["qt-host-qualification-evidence"]
        },
        "qt-target-configure-observed": {"targets": qt_target_configures},
        "qt-target-configure-qualified": {
            "targets": qt_target_configure_qualifications
        },
        "qt-target-webengine-built": {"targets": qt_target_webengine_builds},
        "qt-target-built": {"targets": qt_target_builds},
        "qt-target-build-qualified": {
            "targets": qt_target_qualifications
        },
        "qt-runtime-overlay-qualified": {
            "targets": qt_runtime_overlay_qualifications
        },
        "qt-target-runtime-qualified": {
            "targets": qt_target_runtime_qualifications
        },
        "qt-native-runtime-root": {
            "targets": ["qt-aarch64-native-runtime-root"]
        },
    }


def render(repository):
    validator = runpy.run_path(str(repository / "scripts/validate-release.py"))
    load_json = validator["load_json"]
    validate = validator["validate"]
    validate_schema_subset = validator["validate_schema_subset"]

    config_path = repository / "config/release.json"
    schema_path = repository / "config/schemas/release.schema.json"
    config = load_json(config_path)
    schema = load_json(schema_path)
    validate_schema_subset(schema)
    validate(config, schema, schema, "$")
    qt_validator = runpy.run_path(
        str(repository / "scripts/validate-qt-qualification.py")
    )
    qt_plan = qt_validator["load_and_validate"](
        repository / "config/qt-qualification.json",
        repository / "config/schemas/qt-qualification-plan.schema.json",
    )
    qt_validator["validate_plan"](qt_plan, require_locked=True)

    base = config["base_image"]
    rocky_amd64_image = "%s:%s@%s" % (
        base["repository"],
        base["tag"],
        base["manifests"]["amd64"],
    )
    rocky_arm64_image = "%s:%s@%s" % (
        base["repository"],
        base["tag"],
        base["manifests"]["arm64"],
    )
    qemu = config["qemu"]
    qemu_executor = qemu["executor"]
    qemu_image = "%s:%s@%s" % (
        qemu_executor["repository"],
        qemu_executor["tag"],
        qemu_executor["manifest_digest"],
    )
    platform = config["platforms"]["image"]
    targets = {}
    plan_names = []
    for target in config["targets"]:
        name = "toolchain-plan-%s" % target["arch"]
        plan_names.append(name)
        targets[name] = {
            "inherits": ["_common"],
            "target": "toolchain-plan",
            "args": {"CROSSFORGE_TARGET_ARCH": target["arch"]},
            "output": ["type=cacheonly"],
        }

    common = {
        "contexts": {
            "crossforge_rocky_amd64": "docker-image://%s" % rocky_amd64_image,
            "crossforge_rocky_arm64": "docker-image://%s" % rocky_arm64_image,
        },
        "platforms": [platform],
    }
    component_arguments = component_digest_arguments(repository, config)
    arguments = {
        "ROCKY_RPM_TRUST_FINGERPRINT": config["trust"]["rocky_rpm_key"][
            "fingerprint"
        ],
        "ROCKY_RPM_TRUST_SHA256": config["trust"]["rocky_rpm_key"]["sha256"],
        "ROCKY_AMD64_MANIFEST_DIGEST": base["manifests"]["amd64"],
        "ROCKY_ARM64_MANIFEST_DIGEST": base["manifests"]["arm64"],
        "QEMU_EXECUTOR_VERSION": qemu["version"],
        "QEMU_EXECUTOR_BINARY_SHA256": qemu_executor["binary_sha256"],
        "QEMU_EXECUTOR_CPU": qemu_executor["cpu"],
        "QEMU_EXECUTOR_UNAME_RELEASE": qemu_executor["uname_release"],
    }
    if (
        config["gts"]["source"]["status"] == "locked"
        and config["binutils"]["source"]["status"] == "locked"
    ):
        arguments.update({
            "GTS_BINUTILS_HEADER_ARCH": config["binutils"]["source"]["header_arch"],
            "GTS_BINUTILS_REPOSITORY_NEVRA": config["binutils"]["source"][
                "repository_nevra"
            ],
            "GTS_BINUTILS_SHA256": config["binutils"]["source"]["sha256"],
            "GTS_BINUTILS_SPEC_SHA256": config["binutils"]["source"][
                "spec_sha256"
            ],
            "GTS_GCC_HEADER_ARCH": config["gts"]["source"]["header_arch"],
            "GTS_GCC_REPOSITORY_NEVRA": config["gts"]["source"][
                "repository_nevra"
            ],
            "GTS_GCC_SHA256": config["gts"]["source"]["sha256"],
            "GTS_GCC_SPEC_SHA256": config["gts"]["source"]["spec_sha256"],
        })
    common["args"] = arguments
    targets["_common"] = common
    for name in (
        "qemu-aarch64-validated",
        "runtime-smoke-aarch64",
        "toolchain-aarch64-dev",
        "gcc-testsuite-aarch64-smoke",
        "gcc-testsuite-smoke-evidence",
    ):
        targets[name] = {
            "contexts": {"crossforge_qemu": "docker-image://%s" % qemu_image}
        }
    targets["sbom-generator-source"] = {
        "args": {
            "SBOM_GENERATOR_SOURCE_COMPONENT_SHA256": component_arguments[
                component_argument_name("sources/sbom-generator")
            ]
        }
    }
    render_zstd_graph(config, targets, component_arguments, rocky_amd64_image)
    qt_groups = render_qt_graph(
        config, qt_plan, targets, component_arguments, rocky_amd64_image
    )
    ninja_groups = render_ninja_graph(config, targets, component_arguments)
    component_renderer = runpy.run_path(
        str(repository / "scripts/render-release-components.py")
    )
    vcpkg_groups = render_vcpkg_graph(
        config,
        targets,
        component_arguments,
        component_renderer["VCPKG_CONTRACT_POLICY"],
    )
    packaging_groups = render_packaging_graph(
        config,
        targets,
        component_arguments,
        component_renderer["CROSSPACK_QUALIFICATION_POLICY"],
    )
    python_groups = render_python_graph(config, targets, component_arguments)
    for name, contexts in scoped_main_toolchain_contexts(repository).items():
        existing = targets.setdefault(name, {}).setdefault("contexts", {})
        if set(existing) & set(contexts):
            raise ValueError("duplicate toolchain context binding: %s" % name)
        existing.update(contexts)
    for name, scoped_arguments in scoped_main_component_arguments(
        repository, component_arguments
    ).items():
        target = targets.setdefault(name, {})
        existing = target.setdefault("args", {})
        overlap = set(existing) & set(scoped_arguments)
        if overlap:
            raise ValueError(
                "%s repeats scoped component arguments: %s"
                % (name, ", ".join(sorted(overlap)))
            )
        existing.update(scoped_arguments)
    document = {
        "group": {
            "toolchain-plan": {"targets": plan_names},
            **qt_groups,
            **ninja_groups,
            **vcpkg_groups,
            **packaging_groups,
            **python_groups,
        },
        "target": targets,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=repository / "docker-bake.override.json",
    )
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = render(repository)

    if arguments.check:
        try:
            actual = arguments.output.read_text(encoding="utf-8")
        except OSError as error:
            print("error: %s" % error, file=sys.stderr)
            return 1
        if actual != expected:
            print(
                "error: %s is stale; run scripts/render-bake.py" % arguments.output,
                file=sys.stderr,
            )
            return 1
        print("valid: %s is generated from config/release.json" % arguments.output)
        return 0

    arguments.output.write_text(expected, encoding="utf-8")
    print("wrote: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
