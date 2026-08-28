# crossforge

Crossforge is being rewritten as a Docker-first, GTS-derived cross SDK. The
planned public artifact is one `linux/amd64` image containing a native GTS15
host compiler, EL8-targeting cross compilers for x86_64 and aarch64, CPython
3.9–3.14 cross SDKs, common build tools, pinned vcpkg integration, and
build-system-independent DEB/RPM packaging.

> **Not yet usable as the new SDK.** Phase 1 contains configuration,
> validation, architecture documentation, and explicit `NOT BUILT` Docker
> planning stages only. It does not publish a compiler image.

The accepted implementation contract is in
[`docs/architecture.md`](docs/architecture.md). The original Rust prototype is
preserved at tag `prototype-rust-2026-08-28` and has been removed from the
rewrite branch.

## Phase 1

Validate the canonical release configuration:

```console
$ ./scripts/validate-release.py
```

Unverified source pins are represented explicitly as `pending`, never with
invented URLs or hashes. Candidate and release builds will require:

```console
$ ./scripts/validate-release.py --require-locked
```

Check that the generated Bake override matches `config/release.json`:

```console
$ ./scripts/render-bake.py --check
```

Inspect or execute the honest phase-one graph:

```console
$ docker buildx bake --print phase1
$ docker buildx bake phase1
```

The graph validates configuration inside the pinned Rocky 8 base, expands the
x86_64/aarch64 target plans, and creates an inspectable SDK directory layout.
Every output is cache-only and marked `NOT BUILT`; it cannot be confused with a
qualified release.

## Product contract

- Build platform and tool host: `linux/amd64`.
- Compiler targets: `x86_64-unknown-linux-gnu` and
  `aarch64-unknown-linux-gnu`.
- Baseline: immutable Rocky Linux 8.10 sysroots with a separately frozen EL8
  ABI floor.
- Compiler: GCC Toolset 15 prepared sources, fully cross-built with RH
  `libstdc++_nonshared80` and libgcc hybrid linking.
- Python: one amd64 build Python plus two target Pythons for each CPython
  minor from 3.9 through 3.14.
- Dependencies: pinned vcpkg with qualified static and dynamic triplets.
- Packaging: a thin `crosspack` planner backed by nFPM for split DEB/RPM
  packages; no CMake-only packaging contract.
- Delivery: build once, test the exact candidate digest, then promote tags
  without rebuilding.

Crossforge is not an official or supported Red Hat GCC Toolset. It does not
build or repair wheels, manage arbitrary third-party sysroots, guarantee every
vcpkg port, or publish APT/YUM repositories.

## License

Crossforge's own code is licensed under either Apache-2.0 or MIT. The eventual
SDK image will also contain independently licensed upstream components. Every
public release must ship the corresponding source bundle, license inventory,
SBOM, provenance, and qualification report described in the architecture
contract.
