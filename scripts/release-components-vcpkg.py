#!/usr/bin/env python3
"""vcpkg and Ninja extension for the canonical release-component graph."""

import copy


NINJA_HOST_TOOL_POLICY = {
    "schema_version": 1,
    "install_prefix": "/opt/crossforge/host-tools/ninja",
    "binary_relative_path": "bin/ninja",
    "license_relative_path": "share/licenses/ninja/COPYING",
    "path_precedence": "before-system",
    "system_binary": "/usr/bin/ninja",
    "consumers": ["cmake", "meson", "vcpkg"],
}
VCPKG_INTEGRATION_POLICY = {
    "schema_version": 1,
    "cmake_root": "/opt/crossforge/cmake",
    "crt_linkage": "dynamic",
    "execution_adapters": {
        "cmake_variable": "CMAKE_CROSSCOMPILING_EMULATOR",
        "environment_variable": "HOSTRUNNER",
    },
    "find_root_modes": {
        "include": "ONLY",
        "library": "ONLY",
        "package": "ONLY",
        "program": "NEVER",
    },
    "host": {
        "architecture": "x64",
        "compiler_root": "/opt/rh/gcc-toolset-15/root/usr/bin",
        "cross_compiling": False,
        "library_linkage": "static",
        "toolchain": "host-gts15.cmake",
        "triplet": "crossforge-host-x64-el8",
    },
    "pic_flag": "-fPIC",
    "position_independent_code": True,
    "system_name": "Linux",
    "system_version": "4.18.0",
    "targets": {
        "aarch64": {
            "architecture": "arm64",
            "compiler_root": "/opt/crossforge/targets/aarch64-unknown-linux-gnu/bin",
            "dynamic_triplet": "crossforge-arm64-el8-dynamic",
            "processor": "aarch64",
            "static_triplet": "crossforge-arm64-el8",
            "sysroot": "/opt/crossforge/sysroots/el8/aarch64",
            "toolchain": "aarch64-unknown-linux-gnu.cmake",
            "triple": "aarch64-unknown-linux-gnu",
        },
        "x86_64": {
            "architecture": "x64",
            "compiler_root": "/opt/crossforge/targets/x86_64-unknown-linux-gnu/bin",
            "dynamic_triplet": "crossforge-x64-el8-dynamic",
            "processor": "x86_64",
            "static_triplet": "crossforge-x64-el8",
            "sysroot": "/opt/crossforge/sysroots/el8/x86_64",
            "toolchain": "x86_64-unknown-linux-gnu.cmake",
            "triple": "x86_64-unknown-linux-gnu",
        },
    },
    "triplet_root": "/opt/crossforge/vcpkg/triplets",
    "vcpkg_root": "/opt/crossforge/vcpkg/root",
}
VCPKG_CONTRACT_POLICY = {
    "schema_version": 1,
    "assets": {
        "patchelf": {
            "filename": "patchelf-0.19.0-x86_64.tar.gz",
            "url": "https://github.com/NixOS/patchelf/releases/download/0.19.0/patchelf-0.19.0-x86_64.tar.gz",
            "sha256": "a493df96abeecee55d539071e9bace94d32458a3baf54d9495da94f44c647d86",
            "sha512": "2a65c9cbdddcc7952cdbd6e98a2cf3da01386cf0f0b927a6bbcfe8131ecf0bfb17c534246635b5e6a090652ee54c903f9f9c4f3f1d2412dba59f287ae2ae8070",
            "size": 569003,
        }
    },
    "binary_sources": "clear",
    "downloads": "forbidden",
    "host_triplet": "crossforge-host-x64-el8",
    "triplets": [
        "crossforge-host-x64-el8",
        "crossforge-x64-el8",
        "crossforge-x64-el8-dynamic",
        "crossforge-arm64-el8",
        "crossforge-arm64-el8-dynamic",
    ],
    "files": [
        {
            "path": "manifest/vcpkg.json",
            "sha256": "82c7339ff0f5a6deb9341ab6956fbde95dd6609a3ae503c6e1660e7c9db0df15",
        },
        {
            "path": "ports/crossforge-host-probe/CMakeLists.txt",
            "sha256": "42a4b4d5c76c8b10ec13c9e2b469958a7e7a727648f5981f344e1c741bed2ae7",
        },
        {
            "path": "ports/crossforge-host-probe/copyright",
            "sha256": "bee6e7ffd2d81f6f009ecda87f141e55744e54b7f73d480a866d051aa9c6076c",
        },
        {
            "path": "ports/crossforge-host-probe/portfile.cmake",
            "sha256": "b78122b52c685f36462a44fa1c8ca36f67584f40b056e7be1e18f7a8eb083105",
        },
        {
            "path": "ports/crossforge-host-probe/probe.c",
            "sha256": "87323fff3acc938d97274e2aa723a4c8d868c1522d7c4844a515759b2709234b",
        },
        {
            "path": "ports/crossforge-host-probe/vcpkg.json",
            "sha256": "dae78d63985682b0702f0c5cabe4a9fb62f3032fc818c6adbb57c383fe76942d",
        },
        {
            "path": "ports/crossforge-target-probe/CMakeLists.txt",
            "sha256": "4e5c9ab9fc33cf42d9c98535157ab9ce4962f1a63a3887740e6eff4856625bf3",
        },
        {
            "path": "ports/crossforge-target-probe/copyright",
            "sha256": "bee6e7ffd2d81f6f009ecda87f141e55744e54b7f73d480a866d051aa9c6076c",
        },
        {
            "path": "ports/crossforge-target-probe/portfile.cmake",
            "sha256": "edde722f6730c494b8697a039766a45e31df0b3406e5a6e9a7e2d82f5dd2c076",
        },
        {
            "path": "ports/crossforge-target-probe/probe.c",
            "sha256": "eab43a212383f9e262783909ca2362e99124ec49f114942e3634c8171a368f3c",
        },
        {
            "path": "ports/crossforge-target-probe/probe.h",
            "sha256": "1ac333a679f98b4dd79adc3cfa25878614a178b667224b0338f364d49288f5c5",
        },
        {
            "path": "ports/crossforge-target-probe/vcpkg.json",
            "sha256": "77a5fd45be846d49c40345a418d6d15df3e6fb8ae2412d7435e2c6759798c586",
        },
    ],
}


def leaf_items(value, path=()):
    if isinstance(value, dict) and value:
        result = []
        for key in sorted(value):
            result.extend(leaf_items(value[key], path + (key,)))
        return result
    if isinstance(value, list) and value:
        result = []
        for index, item in enumerate(value):
            result.extend(leaf_items(item, path + (index,)))
        return result
    return [(path, value)]


def policy_materials(prefix, policy):
    return sorted(
        [
            {
                "path": prefix + "/".join(str(part) for part in path),
                "value": copy.deepcopy(value),
            }
            for path, value in leaf_items(policy)
        ],
        key=lambda material: material["path"],
    )


def vcpkg_sdk_scope(release, require):
    statuses = (
        release["host_locks"]["host-runtime"]["status"],
        release["host_tools"]["ninja"]["binary"]["status"],
        release["host_tools"]["ninja"]["source"]["status"],
        release["vcpkg"]["release"]["status"],
        release["vcpkg"]["tool"]["status"],
    )
    require(
        all(status in ("pending", "locked") for status in statuses),
        "vcpkg SDK input status is invalid",
    )
    return "build" if all(status == "locked" for status in statuses) else "future"


def extend_component_graph(context):
    """Add only the Ninja/vcpkg domain to a prepared core graph."""
    add = context["add"]
    release = context["release"]
    require = context["require"]
    selector = context["selector"]
    toolchain_builds = context["toolchain_builds"]
    toolchain_qualifications = context["toolchain_qualifications"]
    add(
        "implementation/vcpkg-integration",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg/", VCPKG_INTEGRATION_POLICY
        ),
    )
    add(
        "implementation/ninja-host-tool",
        "build",
        explicit_materials=policy_materials(
            "/@implementation/host-tools/ninja/", NINJA_HOST_TOOL_POLICY
        ),
    )
    add(
        "implementation/vcpkg-contract-qualification",
        "qualification",
        explicit_materials=policy_materials(
            "/@implementation/vcpkg-contract/", VCPKG_CONTRACT_POLICY
        ),
    )
    add(
        "host-tools/ninja",
        "build",
        selector(("baseline",), ("platforms",)),
        (
            "rpm/host-runtime",
            "sources/ninja",
            "implementation/ninja-host-tool",
        ),
    )
    add(
        "vcpkg/sdk-build",
        vcpkg_sdk_scope(release, require),
        selector(("baseline",), ("platforms",)),
        (
            "rpm/host-runtime",
            "sources/vcpkg",
            "host-tools/ninja",
            "implementation/vcpkg-integration",
            toolchain_builds["x86_64"],
            toolchain_builds["aarch64"],
        ),
    )
    add(
        "vcpkg/contract-qualification",
        "qualification",
        selector(("baseline",), ("platforms",)),
        (
            "vcpkg/sdk-build",
            "implementation/vcpkg-contract-qualification",
            toolchain_qualifications["x86_64"],
            toolchain_qualifications["aarch64"],
        ),
    )
