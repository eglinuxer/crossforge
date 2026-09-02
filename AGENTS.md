# Repository Guidelines

## Project Structure & Module Organization

`docs/architecture.md` is canonical. Configuration lives under `config/`; tracked `config/generated/` projections come only from `render-release-components.py`. Raw provenance is under `evidence/`, and DNF decisions under `locks/`. `docker/Dockerfile` owns toolchains; `docker/python.Dockerfile` owns parameterized Python rows. Tools are in `scripts/`, backports in `patches/`, and tests in `tests/`.

The deleted Rust prototype is recoverable from tag `prototype-rust-2026-08-28`; do not restore its abstractions. Keep generated work, staging trees, and caches uncommitted.

## Build, Test, and Development Commands

- `./scripts/validate-release.py` validates strict JSON and prints its canonical digest.
- `./scripts/validate-rpm-lock.py locks/sysroot-el8-{x86_64,aarch64}.json --require-lock` represents per-target checks; invoke each concrete path.
- `./scripts/validate-rpm-lock.py locks/host-build-common-el8-x86_64.json --require-lock` validates a host closure; repeat for the GCC and Python delta locks.
- `./scripts/render-bake.py --check` detects drift in the generated Bake override.
- `./scripts/render-release-components.py --check` verifies cache-scoped component identities and their release binding.
- `docker buildx bake sysroot-x86_64 sysroot-aarch64` assembles both signed EL8 sysroots offline.
- `docker buildx bake host-build-common-locked host-gcc-build-locked host-python-build-locked` replays all host transactions offline.
- `docker buildx bake toolchain-x86_64-dev toolchain-aarch64-dev` builds both real cross slices; aarch64 uses explicit pinned QEMU, never implicit binfmt.
- `docker buildx bake phase5` and `phase6` reproduce the frozen cp313 and cp313+cp311 snapshots; `python-phase5-dev` and `python-phase6-dev` name those exact aggregates.
- `phase7` names the cp313+cp311+cp312 snapshot. Use `python-native-latest` and `python-matrix` for the rows selected by the shared implementation contract; graph existence alone is not qualification evidence.

Never publish `sdk-skeleton` or a target ending in `-dev`; only a fully locked and qualified future candidate may receive user-facing tags.

## Coding Style & Naming Conventions

Use strict JSON plus JSON Schema; reject duplicate keys, unknown fields, and schema versions. Keep Bash quoted and under `set -Eeuo pipefail`. Python infrastructure should prefer the standard library. Delegate package, ELF, RPM, and dependency semantics to upstream tools.

## Testing Guidelines

Add a regression test for every defect. Build changes must name the image digest, target, sysroot/config digest, and validation tier. x86_64 and aarch64 remain distinct targets although the image runs on amd64. Target execution belongs only in qualification stages; cross stages must not set `HOSTRUNNER` or QEMU and must preserve target-artifact guard evidence. Pending pins may exist only in non-candidate planning/dev targets; candidates require `--require-locked`.

## Commit & Pull Request Guidelines

History uses sentence-case imperative subjects without a trailing period, such as `Retry transient download failures`; focused prefixes like `Audit:` are acceptable. Conventional Commit prefixes are not required. Explain motivation and evidence when behavior is non-obvious.

Keep pull requests focused, link applicable issues, describe contract or supply-chain effects, and list validation commands and results. Contributions are dual-licensed under MIT or Apache-2.0 unless explicitly stated otherwise.
