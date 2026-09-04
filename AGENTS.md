# Repository Guidelines

## Project Structure & Module Organization

`docs/architecture.md` is canonical. Configuration is under `config/`; only `render-release-components.py` writes `config/generated/`. ABI policy is in `abi/el8/`, provenance in `evidence/`, DNF locks in `locks/`, scripts in `scripts/`, and tests in `tests/`.

Rust prototype: tag `prototype-rust-2026-08-28`. Do not commit caches.

## Build, Test, and Development Commands

- `./scripts/validate-release.py` validates release JSON.
- `./scripts/validate-frozen-abi.py` checks ABI evidence; `./scripts/validate-python-runtime-providers.py` checks Python's RPM-owned DSO policy.
- `./scripts/validate-rpm-lock.py <lock> --require-lock` validates a content lock.
- `./scripts/render-release-components.py --check`, `./scripts/render-vcpkg-integration.py --check`, and `./scripts/render-bake.py --check` detect generated-file drift.
- `docker buildx bake sysroot-x86_64 sysroot-aarch64` assembles both signed EL8 sysroots offline.
- `docker buildx bake host-build-common-locked host-gcc-build-locked host-python-build-locked host-runtime-qualified` verifies host closures offline.
- `docker buildx bake cmake-host-tool ninja-host-tool` locks host tools; `docker buildx bake vcpkg-upstream-tier3-qualified` runs all three curated tiers across five triplets offline.
- Packaging tests live in `tests/packaging`; `docker buildx bake packaging-qualified sdk-complete-dev` qualifies split/debug packages, ELF audits, and the composed SDK.
- `docker buildx bake toolchain-x86_64-dev toolchain-aarch64-dev` builds both cross slices; aarch64 uses pinned QEMU, never implicit binfmt.
- `docker buildx bake gcc-testsuite-smoke` runs the GCC execute slice on x86_64 and both aarch64 runtime tiers, then emits reviewable evidence.
- `docker buildx bake phase10` requalifies all Python 3.9–3.14 rows for both targets. `python-native-latest` and `python-matrix` select the same six-row contract. Graph existence or a build probe alone is not qualification evidence.

Never publish `sdk-skeleton` or `-dev` targets; tag only locked, qualified candidates.

## Coding Style & Naming Conventions

Use strict JSON plus JSON Schema; reject duplicate keys, unknown fields, and schema versions. Keep Bash quoted and under `set -Eeuo pipefail`. Prefer Python's standard library. Delegate package, ELF, RPM, and dependency semantics to upstream tools.

## Testing Guidelines

Add a regression test for every defect. Build evidence must name the image, target, sysroot/config digest, and tier. x86_64 and aarch64 remain distinct targets. Execute target code only in qualification stages; cross stages must not set `HOSTRUNNER` or QEMU. GCC baselines compare exact status, suite, test identity, and occurrence; added or resolved unexpected records fail. Candidates require `--require-locked`.

## Commit & Pull Request Guidelines

Use sentence-case imperative subjects without a trailing period, such as `Retry transient download failures`. Conventional Commit prefixes are optional. Explain non-obvious motivation and evidence.

Keep pull requests focused, link applicable issues, describe contract or supply-chain effects, and list validation commands and results. Contributions are dual-licensed under MIT or Apache-2.0 unless explicitly stated otherwise.
