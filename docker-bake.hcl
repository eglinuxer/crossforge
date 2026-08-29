# Canonical Buildx Bake entry point for the Docker-first rewrite.
#
# Target matrices, platforms, source identities and the base image are generated
# from config/release.json into docker-bake.override.json; run render-bake.py
# after changing the canonical release configuration.

target "_common" {
  context    = "."
  dockerfile = "docker/Dockerfile"
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

target "gcc-x86_64" {
  inherits = ["_common"]
  target   = "gcc-x86_64"
  output   = ["type=cacheonly"]
}

target "toolchain-x86_64-dev" {
  inherits = ["_common"]
  target   = "toolchain-x86_64-dev"
  output   = ["type=cacheonly"]
}

target "runtime-smoke-x86_64" {
  inherits = ["_common"]
  target   = "runtime-smoke-x86_64"
  output   = ["type=cacheonly"]
}

group "phase1" {
  targets = ["validate", "toolchain-plan", "sdk-skeleton"]
}

group "phase2" {
  targets = ["validate", "platform-python-check", "sysroot-x86_64", "toolchain-x86_64-dev"]
}
