# Canonical Buildx Bake entry point for the Docker-first rewrite.
#
# Target matrices, platforms, source identities and the base image are generated
# from config/release.json into docker-bake.override.json; run render-bake.py
# after changing the canonical release configuration.
# Python phase groups and frozen `python-phaseN-dev` snapshots are generated
# from scripts/python_row_contract.py; this HCL file owns only Phases 1-4.

target "_common" {
  context    = "."
  dockerfile = "docker/Dockerfile"
  platforms  = ["linux/amd64"]
}

# CPython rows use a separate, parameterized Dockerfile. Concrete row/target
# names and target-context edges are generated from config/release.json.
target "_python_common" {
  context    = "."
  dockerfile = "docker/python.Dockerfile"
  platforms  = ["linux/amd64"]
}

target "_zstd_common" {
  context    = "."
  dockerfile = "docker/zstd.Dockerfile"
  platforms  = ["linux/amd64"]
}

target "_vcpkg_common" {
  context    = "."
  dockerfile = "docker/vcpkg.Dockerfile"
  platforms  = ["linux/amd64"]
}

target "_host_tools_common" {
  context    = "."
  dockerfile = "docker/host-tools.Dockerfile"
  platforms  = ["linux/amd64"]
}

target "_packaging_common" {
  context    = "."
  dockerfile = "docker/packaging.Dockerfile"
  platforms  = ["linux/amd64"]
}

target "_qt_common" {
  context    = "."
  dockerfile = "docker/qt.Dockerfile"
  platforms  = ["linux/amd64"]
}

variable "CROSSFORGE_SOURCE_COMMIT" {
  default = "0000000000000000000000000000000000000000"
}

target "source-bundle" {
  context    = "."
  dockerfile = "docker/source-bundle.Dockerfile"
  platforms  = ["linux/amd64"]
  target     = "source-bundle-output"
  args = {
    CROSSFORGE_SOURCE_COMMIT = CROSSFORGE_SOURCE_COMMIT
  }
  contexts = {
    crossforge_host_common    = "target:host-build-common-locked"
    crossforge_rpm_sources    = "target:rpm-source-bundle"
    crossforge_qemu_source    = "target:qemu-source-qualified"
    crossforge_cmake_source   = "target:cmake-source"
    crossforge_ninja_source   = "target:ninja-source"
    crossforge_vcpkg_source   = "target:vcpkg-source"
    crossforge_nfpm_tool      = "target:nfpm-tool"
    crossforge_sbom_generator_source = "target:sbom-generator-source"
    crossforge_zstd_source    = "target:zstd-source"
    crossforge_qt_source      = "target:qt-source"
    crossforge_ffmpeg_source  = "target:ffmpeg-source"
    crossforge_xcb_source     = "target:xcb-util-cursor-source"
    crossforge_cpython_cp39   = "target:cpython-source-cp39"
    crossforge_cpython_cp310  = "target:cpython-source-cp310"
    crossforge_cpython_cp311  = "target:cpython-source-cp311"
    crossforge_cpython_cp312  = "target:cpython-source-cp312"
    crossforge_cpython_cp313  = "target:cpython-source-cp313"
    crossforge_cpython_cp314  = "target:cpython-source-cp314"
  }
  output = ["type=cacheonly"]
}

target "sbom-generator-source" {
  context    = "."
  dockerfile = "docker/sbom.Dockerfile"
  target     = "sbom-generator-source-output"
  contexts = {
    crossforge_host_common = "target:host-build-common-locked"
  }
  args = {
    SBOM_GENERATOR_SOURCE_COMPONENT_SHA256 = ""
  }
  output = ["type=cacheonly"]
}

target "source-bundle-identity" {
  inherits = ["source-bundle"]
  target   = "source-bundle-identity-output"
  output   = ["type=cacheonly"]
}

group "default" {
  targets = ["validate"]
}

group "qt-rpm-locked" {
  targets = [
    "host-qt-build-locked",
    "qt-target-x86_64-locked",
    "qt-target-aarch64-locked",
    "qt-runtime-rpms-x86_64",
    "qt-runtime-rpms-aarch64",
  ]
}

target "validate" {
  inherits = ["_common"]
  target   = "config-validate"
  output   = ["type=cacheonly"]
}

target "platform-python-check" {
  inherits = ["_common"]
  target   = "platform-python-check"
  output   = ["type=cacheonly"]
}

target "sigstore-sources-qualified" {
  inherits = ["_common"]
  target   = "sigstore-verification-evidence"
  output   = ["type=cacheonly"]
}

target "cosign-host-tool" {
  inherits = ["_common"]
  target   = "cosign-host-tool"
  output   = ["type=cacheonly"]
}

target "rocky-base-source-map" {
  inherits = ["_common"]
  target   = "rocky-base-source-map-evidence"
  output   = ["type=cacheonly"]
}

target "rpm-source-requirements" {
  inherits = ["_common"]
  target   = "rpm-source-requirements-evidence"
  contexts = {
    crossforge_rocky_source_map = "target:rocky-base-source-map"
  }
  output = ["type=cacheonly"]
}

target "rpm-source-lock-maintenance" {
  inherits = ["_common"]
  target   = "rpm-source-lock-maintenance-output"
  output   = ["type=cacheonly"]
}

target "rpm-source-lock-validated" {
  inherits = ["_common"]
  target   = "rpm-source-lock-validated-output"
  output   = ["type=cacheonly"]
}

target "rpm-source-bundle" {
  inherits = ["_common"]
  target   = "rpm-source-bundle-output"
  output   = ["type=cacheonly"]
}

target "sdk-skeleton" {
  inherits = ["_common"]
  target   = "sdk-skeleton"
  output   = ["type=cacheonly"]
}

target "host-qt-build-locked" {
  inherits = ["_common"]
  target   = "host-qt-build-locked"
  output   = ["type=cacheonly"]
}

target "qt-target-x86_64-locked" {
  inherits = ["_common"]
  target   = "qt-target-x86_64-locked"
  output   = ["type=cacheonly"]
}

target "qt-target-aarch64-locked" {
  inherits = ["_common"]
  target   = "qt-target-aarch64-locked"
  output   = ["type=cacheonly"]
}

target "qt-runtime-rpms-x86_64" {
  inherits = ["_common"]
  target   = "qt-runtime-rpms-x86_64"
  output   = ["type=cacheonly"]
}

target "qt-runtime-rpms-aarch64" {
  inherits = ["_common"]
  target   = "qt-runtime-rpms-aarch64"
  output   = ["type=cacheonly"]
}

target "gts-gcc-source" {
  inherits = ["_common"]
  target   = "gts-gcc-source"
  output   = ["type=cacheonly"]
}

target "gts-binutils-source" {
  inherits = ["_common"]
  target   = "gts-binutils-source"
  output   = ["type=cacheonly"]
}

target "sysroot-x86_64" {
  inherits = ["_common"]
  target   = "sysroot-x86_64"
  output   = ["type=cacheonly"]
}

target "sysroot-aarch64" {
  inherits = ["_common"]
  target   = "sysroot-aarch64"
  output   = ["type=cacheonly"]
}

target "host-build-common-locked" {
  inherits = ["_common"]
  target   = "host-build-common-locked"
  output   = ["type=cacheonly"]
}

target "host-runtime-locked" {
  inherits = ["_common"]
  target   = "host-runtime-locked"
  output   = ["type=cacheonly"]
}

target "host-runtime-qualified" {
  inherits = ["_common"]
  target   = "host-runtime-qualified"
  output   = ["type=cacheonly"]
}

target "host-gcc-build-locked" {
  inherits = ["_common"]
  target   = "host-gcc-build-locked"
  output   = ["type=cacheonly"]
}

target "host-gcc-test-locked" {
  inherits = ["_common"]
  target   = "host-gcc-test-locked"
  output   = ["type=cacheonly"]
}

target "host-python-build-locked" {
  inherits = ["_common"]
  target   = "host-python-build-locked"
  output   = ["type=cacheonly"]
}

target "python-runtime-clean-x86_64" {
  inherits = ["_common"]
  target   = "python-runtime-clean-x86_64"
  output   = ["type=cacheonly"]
}

target "python-runtime-clean-aarch64" {
  inherits = ["_common"]
  target   = "python-runtime-clean-aarch64"
  output   = ["type=cacheonly"]
}

target "qemu-aarch64-validated" {
  inherits = ["_common"]
  target   = "qemu-aarch64-validated"
  output   = ["type=cacheonly"]
}

target "qemu-source-qualified" {
  inherits = ["_common"]
  target   = "qemu-source-qualified-output"
  output   = ["type=cacheonly"]
}

# Export only the library roots needed for ABI inventory maintenance. The
# default is cache-only; opt in to a review archive with
# `--set abi-export.output=type=tar,dest=...`.
target "abi-export" {
  context   = "."
  platforms = ["linux/amd64"]
  contexts = {
    clean_x86_64    = "target:python-runtime-clean-x86_64"
    clean_aarch64   = "target:python-runtime-clean-aarch64"
    sysroot_x86_64  = "target:sysroot-x86_64"
    sysroot_aarch64 = "target:sysroot-aarch64"
  }
  dockerfile-inline = <<EOF
# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM scratch
COPY --from=clean_x86_64 /runtime-root/usr/lib64/ /clean/x86_64/usr/lib64/
COPY --from=clean_aarch64 /runtime-root/usr/lib64/ /clean/aarch64/usr/lib64/
COPY --from=sysroot_x86_64 /opt/crossforge/sysroots/el8/x86_64/usr/lib64/ /sysroot/x86_64/usr/lib64/
COPY --from=sysroot_aarch64 /opt/crossforge/sysroots/el8/aarch64/usr/lib64/ /sysroot/aarch64/usr/lib64/
EOF
  output = ["type=cacheonly"]
}

# Maintenance targets are cache-only unless a maintainer explicitly overrides
# output to a local directory for reviewing a lock refresh.
target "rpm-lock-sysroot-x86_64" {
  inherits = ["_common"]
  target   = "rpm-lock-sysroot-x86_64"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-sysroot-aarch64" {
  inherits = ["_common"]
  target   = "rpm-lock-sysroot-aarch64"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-host-build-common" {
  inherits = ["_common"]
  target   = "rpm-lock-host-build-common"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-host-runtime" {
  inherits = ["_common"]
  target   = "rpm-lock-host-runtime"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-host-gcc-build" {
  inherits = ["_common"]
  target   = "rpm-lock-host-gcc-build"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-host-gcc-test" {
  inherits = ["_common"]
  target   = "rpm-lock-host-gcc-test"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-host-python-build" {
  inherits = ["_common"]
  target   = "rpm-lock-host-python-build"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-host-qt-build" {
  inherits = ["_common"]
  target   = "rpm-lock-host-qt-build"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-qt-target-x86_64" {
  inherits = ["_common"]
  target   = "rpm-lock-qt-target-x86_64"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-qt-target-aarch64" {
  inherits = ["_common"]
  target   = "rpm-lock-qt-target-aarch64"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-qt-runtime-x86_64" {
  inherits = ["_common"]
  target   = "rpm-lock-qt-runtime-x86_64"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "rpm-lock-qt-runtime-aarch64" {
  inherits = ["_common"]
  target   = "rpm-lock-qt-runtime-aarch64"
  no-cache = true
  output   = ["type=cacheonly"]
}

target "gcc-prepared" {
  inherits = ["_common"]
  target   = "gcc-prepared"
  output   = ["type=cacheonly"]
}

target "binutils-prepared" {
  inherits = ["_common"]
  target   = "binutils-prepared"
  output   = ["type=cacheonly"]
}

target "binutils-x86_64" {
  inherits = ["_common"]
  target   = "binutils-x86_64"
  output   = ["type=cacheonly"]
}

target "binutils-aarch64" {
  inherits = ["_common"]
  target   = "binutils-aarch64"
  output   = ["type=cacheonly"]
}

target "gcc-x86_64" {
  inherits = ["_common"]
  target   = "gcc-x86_64"
  output   = ["type=cacheonly"]
}

target "gcc-aarch64" {
  inherits = ["_common"]
  target   = "gcc-aarch64"
  output   = ["type=cacheonly"]
}

target "toolchain-x86_64-build-export" {
  inherits = ["_common"]
  target   = "toolchain-x86_64-build-export"
  output   = ["type=cacheonly"]
}

target "toolchain-aarch64-build-export" {
  inherits = ["_common"]
  target   = "toolchain-aarch64-build-export"
  output   = ["type=cacheonly"]
}

target "gcc-x86_64-test-context-export" {
  inherits = ["_common"]
  target   = "gcc-x86_64-test-context-export"
  output   = ["type=cacheonly"]
}

target "gcc-aarch64-test-context-export" {
  inherits = ["_common"]
  target   = "gcc-aarch64-test-context-export"
  output   = ["type=cacheonly"]
}

target "toolchain-x86_64-dev" {
  inherits = ["_common"]
  target   = "toolchain-x86_64-dev"
  output   = ["type=cacheonly"]
}

target "toolchain-aarch64-dev" {
  inherits = ["_common"]
  target   = "toolchain-aarch64-dev"
  output   = ["type=cacheonly"]
}

target "runtime-smoke-x86_64" {
  inherits = ["_common"]
  target   = "runtime-smoke-x86_64"
  output   = ["type=cacheonly"]
}

target "runtime-smoke-aarch64" {
  inherits = ["_common"]
  target   = "runtime-smoke-aarch64"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-x86_64-smoke" {
  inherits = ["_common"]
  target   = "gcc-testsuite-x86_64-smoke"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-aarch64-smoke" {
  inherits = ["_common"]
  target   = "gcc-testsuite-aarch64-smoke"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-smoke-evidence" {
  inherits = ["_common"]
  target   = "gcc-testsuite-smoke-evidence"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-x86_64-full-observe" {
  inherits = ["_common"]
  target   = "gcc-testsuite-x86_64-full-observe"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-full-observation-evidence" {
  inherits = ["_common"]
  target   = "gcc-testsuite-full-observation-evidence"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-x86_64-full-qualified" {
  inherits = ["_common"]
  target   = "gcc-testsuite-x86_64-full-qualified"
  output   = ["type=cacheonly"]
}

target "gcc-testsuite-full-qualification-evidence" {
  inherits = ["_common"]
  target   = "gcc-testsuite-full-qualification-evidence"
  output   = ["type=cacheonly"]
}

group "gcc-testsuite-full-observe" {
  targets = ["gcc-testsuite-full-observation-evidence"]
}

group "gcc-testsuite-full-qualified" {
  targets = ["gcc-testsuite-full-qualification-evidence"]
}

group "gcc-testsuite-smoke" {
  targets = ["gcc-testsuite-smoke-evidence"]
}

group "phase16" {
  targets = [
    "validate",
    "gcc-testsuite-smoke-evidence"
  ]
}

group "phase1" {
  targets = ["validate", "toolchain-plan", "sdk-skeleton"]
}

group "phase2" {
  targets = ["validate", "platform-python-check", "sysroot-x86_64", "toolchain-x86_64-dev"]
}

group "phase3" {
  targets = [
    "validate",
    "platform-python-check",
    "host-build-common-locked",
    "host-gcc-build-locked",
    "sysroot-x86_64",
    "toolchain-x86_64-dev"
  ]
}

group "phase4" {
  targets = [
    "validate",
    "platform-python-check",
    "host-build-common-locked",
    "host-gcc-build-locked",
    "sysroot-x86_64",
    "sysroot-aarch64",
    "toolchain-x86_64-dev",
    "toolchain-aarch64-dev"
  ]
}
