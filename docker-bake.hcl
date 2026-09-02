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
