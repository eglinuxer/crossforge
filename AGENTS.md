# Repository Guidelines

## Project Structure & Module Organization

`docs/architecture.md` is canonical. Release inputs and RPM plans live under `config/`; `locks/transactions/` records DNF decisions and `locks/metadata/` pins signed repositories. Docker/Bake defines the graph, `scripts/` contains its tools, and smoke fixtures live under `tests/smoke/`. Future integrations belong under `integration/` and ABI data under `abi/`.

The deleted Rust prototype is recoverable from tag `prototype-rust-2026-08-28`; do not reintroduce its wheel, registry, runner, or custom binary-parser abstractions. Generated `work/`, build output, package staging trees, and local caches must remain uncommitted.

## Build, Test, and Development Commands

- `./scripts/validate-release.py` validates strict JSON and prints its canonical digest.
- `./scripts/validate-rpm-lock.py locks/sysroot-el8-x86_64.json --require-lock` validates plan, transaction, signed metadata, and RPM content bindings.
- `./scripts/validate-rpm-lock.py locks/host-build-common-el8-x86_64.json --require-lock` validates the common host build closure; use the same command for the GCC delta lock.
- `./scripts/render-bake.py --check` detects drift in the generated Bake override.
- `docker buildx bake sysroot-x86_64` assembles the signed, locked EL8 sysroot offline.
- `docker buildx bake host-build-common-locked host-gcc-build-locked` replays both host transactions offline.
- `docker buildx bake toolchain-x86_64-dev` builds and smoke-tests the real x86_64 cross slice; it is not a release image.

Never publish `sdk-skeleton` or a target ending in `-dev`; only a fully locked and qualified future candidate may receive user-facing tags.

## Coding Style & Naming Conventions

Use strict JSON plus JSON Schema; reject duplicate keys, unknown fields, and unknown schema versions. Keep Bash scripts structured, quoted, and under `set -Eeuo pipefail`. Python infrastructure should prefer the standard library and remain compatible with the documented build-stage interpreter. Delegate package, ELF, RPM, and dependency semantics to upstream tools instead of recreating their parsers.

## Testing Guidelines

Add a regression test for every defect. Build changes must name the image digest, target, sysroot/config digest, and validation tier. x86_64 and aarch64 remain distinct targets although the image runs on amd64. Pending pins may exist only in non-candidate planning/dev targets; every consumed input must be locked. Candidates require `--require-locked`.

## Commit & Pull Request Guidelines

History uses sentence-case imperative subjects without a trailing period, such as `Retry transient download failures`; focused prefixes like `Audit:` are acceptable. Conventional Commit prefixes are not required. Explain motivation and evidence when behavior is non-obvious.

Keep pull requests focused, link applicable issues, describe contract or supply-chain effects, and list validation commands and results. Contributions are dual-licensed under MIT or Apache-2.0 unless explicitly stated otherwise.
