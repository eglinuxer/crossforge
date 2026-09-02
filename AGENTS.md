# Repository Guidelines

## Project Structure & Module Organization

`docs/architecture.md` is canonical. Configuration lives under `config/`; tracked `config/generated/` projections come only from `render-release-components.py`. Raw provenance is under `evidence/`, and DNF decisions under `locks/`. `docker/Dockerfile` owns toolchains, `docker/zstd.Dockerfile` the private static zstd, and `docker/python.Dockerfile` parameterized Python rows. Tools are in `scripts/`, backports in `patches/`, and tests in `tests/`.

The deleted Rust prototype remains at tag `prototype-rust-2026-08-28`. Keep work trees and caches uncommitted.

## Build, Test, and Development Commands

- `./scripts/validate-release.py` validates strict JSON and prints its canonical digest.
- `./scripts/validate-rpm-lock.py locks/sysroot-el8-x86_64.json --require-lock` validates one lock; repeat for aarch64 and the three host locks.
- `./scripts/render-bake.py --check` and `./scripts/render-release-components.py --check` detect generated-file drift.
- `docker buildx bake sysroot-x86_64 sysroot-aarch64` assembles both signed EL8 sysroots offline.
- `docker buildx bake host-build-common-locked host-gcc-build-locked host-python-build-locked` replays all host transactions offline.
- `docker buildx bake toolchain-x86_64-dev toolchain-aarch64-dev` builds both real cross slices; aarch64 uses explicit pinned QEMU, never implicit binfmt.
- `docker buildx bake phase5`, `phase6`, `phase7`, and `phase8` preserve fixed row membership from cp313 through cp313+cp311+cp312+cp314; maintenance still requalifies those rows.
- `docker buildx bake phase9` appends cp310; `phase10` appends the cp39 legacy adapter. `python-native-latest` and `python-matrix` select all six shared-contract rows. Graph existence or a build probe alone is not qualification evidence.

Never publish `sdk-skeleton` or a `-dev` target; only a locked, qualified candidate may receive user-facing tags.

## Coding Style & Naming Conventions

Use strict JSON plus JSON Schema; reject duplicate keys, unknown fields, and schema versions. Keep Bash quoted and under `set -Eeuo pipefail`. Python infrastructure should prefer the standard library. Delegate package, ELF, RPM, and dependency semantics to upstream tools.

## Testing Guidelines

Add a regression test for every defect. Build changes must name the image digest, target, sysroot/config digest, and validation tier. x86_64 and aarch64 remain distinct targets although the image runs on amd64. Target execution belongs only in qualification stages; cross stages must not set `HOSTRUNNER` or QEMU and must preserve target-artifact guard evidence. Pending pins may exist only in non-candidate planning/dev targets; candidates require `--require-locked`.

## Commit & Pull Request Guidelines

Use sentence-case imperative subjects without a trailing period, such as `Retry transient download failures`. Conventional Commit prefixes are optional. Explain non-obvious motivation and evidence.

Keep pull requests focused, link applicable issues, describe contract or supply-chain effects, and list validation commands and results. Contributions are dual-licensed under MIT or Apache-2.0 unless explicitly stated otherwise.
