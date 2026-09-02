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

group "default" {
  targets = ["validate"]
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

target "sdk-skeleton" {
  inherits = ["_common"]
  target   = "sdk-skeleton"
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

target "rpm-lock-host-python-build" {
  inherits = ["_common"]
  target   = "rpm-lock-host-python-build"
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
