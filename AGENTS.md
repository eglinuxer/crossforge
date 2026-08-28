# Repository Guidelines

## Project Structure & Module Organization

The Docker/Bake rewrite defined in `docs/architecture.md` is canonical. `config/release.json` and its Schema hold release inputs; `docker/Dockerfile` and `docker-bake.hcl` define the build graph; `scripts/` contains small upstream-build and validation tools. Future integration files belong under `integration/`, locks under `locks/`, frozen ABI data under `abi/`, and acceptance fixtures under `tests/`.

The Rust 2024 crate in `src/` is a preserved prototype until the first qualified vertical slice replaces it. Do not extend its wheel, registry, or runner abstractions. Generated `target/`, `work/`, build output, package staging trees, and local caches must remain uncommitted.

## Build, Test, and Development Commands

- `./scripts/validate-release.py` validates strict JSON and prints its canonical digest.
- `docker buildx bake --print phase1` checks Bake expansion without building.
- `docker buildx bake phase1` runs config validation and honest NOT-BUILT planning/layout stages.
- `cargo fmt --check && cargo test --features cli && cargo doc --no-deps` protects the legacy baseline during migration.

Never publish `sdk-skeleton`; only a future fully qualified candidate stage may receive user-facing tags.

## Coding Style & Naming Conventions

Use strict JSON plus JSON Schema; reject duplicate keys, unknown fields, and unknown schema versions. Keep Bash scripts structured, quoted, and under `set -Eeuo pipefail`. Python infrastructure should prefer the standard library and remain compatible with the documented build-stage interpreter. Delegate package, ELF, RPM, and dependency semantics to upstream tools instead of recreating their parsers.

## Testing Guidelines

Add a focused regression test for every defect. Build changes must name the exact image digest, target, sysroot/config digest, and validation tier. x86_64 and aarch64 are distinct compiler targets even though the SDK image runs on amd64. Pending source pins are allowed only in planning stages; release candidates must use `--require-locked`.

## Commit & Pull Request Guidelines

History uses sentence-case imperative subjects without a trailing period, such as `Retry transient download failures`; focused prefixes like `Audit:` are acceptable. Conventional Commit prefixes are not required. Explain motivation and evidence when behavior is non-obvious.

Keep pull requests focused, link applicable issues, describe contract or supply-chain effects, and list validation commands and results. Do not mix architectural migration with unrelated prototype cleanup. Contributions are dual-licensed under MIT or Apache-2.0 unless explicitly stated otherwise.
