# Canonical Buildx Bake entry point for the Docker-first rewrite.
#
# Phase 1 exposes validation and planning targets only. Target matrices,
# platforms and the base image are generated from config/release.json into
# docker-bake.override.json; run scripts/render-bake.py after changing config.
# A later vertical slice will add real compiler and qualification stages.

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

target "sdk-skeleton" {
  inherits = ["_common"]
  target   = "sdk-skeleton"
  output   = ["type=cacheonly"]
}

group "phase1" {
  targets = ["validate", "toolchain-plan", "sdk-skeleton"]
}
