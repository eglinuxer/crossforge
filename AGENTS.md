# Repository Guidelines

## Project Structure & Module Organization

Crossforge builds one `linux/amd64` SDK for two EL8 cross targets: x86_64 and aarch64. `docs/architecture.md` is canonical; `README.md` documents qualification stages. The Rust prototype remains at tag `prototype-rust-2026-08-28`.

- `config/`: release policy, schemas, and RPM plans; `locks/`, `keys/`, and `evidence/`: pinned transactions, trust roots, and provenance; `abi/el8/`: frozen ABI sets.
- `docker/`, `docker-bake.hcl`, and `scripts/`: build graph and implementation.
- `tools/crossforge/`: CLI, environment selection, ELF audit, and DEB/RPM packaging; `integration/`: CMake, Meson, and vcpkg support.
- `tests/`: configuration, packaging, compiler, Python, vcpkg, and consumer checks; `.github/workflows/`: CI and release gates.

## Build, Test, and Development Commands

Run from the repository root:

```sh
./scripts/validate-release.py --require-locked
./scripts/validate-supply-chain-evidence.py
./scripts/validate-frozen-abi.py
./scripts/validate-python-runtime-providers.py
python3 -m unittest discover -s tests/config -p 'test_*.py'
python3 -m unittest discover -s tests/packaging -p 'test_*.py'
```

These validate locked policy/evidence and run local regression suites. Validate individual RPM locks with `./scripts/validate-rpm-lock.py <lock> --require-lock`.

Use `docker buildx bake --print <target>` to inspect dependencies; omit `--print` to build. Key gates: `toolchain-x86_64-dev`, `toolchain-aarch64-dev`, `python-matrix` (3.9–3.14), `vcpkg-upstream-tier3-qualified`, `packaging-qualified`, `sdk-complete-dev`, `gcc-testsuite-full-qualified`, and `qt-target-runtime-qualified`. Full gates are expensive; follow `.github/workflows/ci.yml` for checks relevant to the change.

## Configuration & Coding Style

Edit version pins in `config/release.json`. Regenerate in order using `scripts/render-release-components.py`, `scripts/render-vcpkg-integration.py`, then `scripts/render-bake.py`; rerun each with `--check`. Never hand-edit their outputs; only the component renderer writes `config/generated/`.

Use four-space Python and two-space JSON indentation. Prefer standard-library Python, `snake_case` functions, quoted Bash, and `set -Eeuo pipefail`. Reject duplicate JSON keys, unknown fields, and unsupported schema versions. Delegate RPM/dependency/ELF semantics to upstream tools. Do not commit caches.

## Testing & Qualification Rules

Add `unittest` regressions named `test_*.py` for defects. Keep targets independent, including x86_64. Never execute target code during cross builds or enable QEMU/`HOSTRUNNER` there. Qualification uses pinned explicit QEMU; releases additionally require native ARM evidence. Qt remains qualification-only.

Evidence must bind image, target, sysroot/config digests, and tier. Graph checks are not qualification. Preserve frozen ABI sets; GCC baselines compare exact status, suite, identity, and occurrence, rejecting added or resolved unexpected records.

## Commit & Pull Request Guidelines

Use sentence-case imperative subjects without trailing periods, e.g. `Report Qt build log growth`. Keep PRs focused; link issues, explain contract/supply-chain effects, and list validation results. Contributions are MIT OR Apache-2.0.

Never publish skeleton or `-dev` outputs. Use candidate/release workflows with locked inputs, immutable evidence, and digest-only promotion without rebuilding. Pin Actions to full commits with least privilege. Report undisclosed vulnerabilities privately per `SECURITY.md`.
