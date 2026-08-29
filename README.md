# crossforge

Crossforge is being rewritten as a Docker-first, GTS-derived cross SDK. The
planned public artifact is one `linux/amd64` image containing a native GTS15
host compiler, EL8-targeting cross compilers for x86_64 and aarch64, CPython
3.9–3.14 cross SDKs, common build tools, pinned vcpkg integration, and
build-system-independent DEB/RPM packaging.

> **Not yet publishable as the complete SDK.** The first real x86_64
> cross-toolchain slice now builds and passes smoke qualification, but host RPM
> inputs remain unlocked and aarch64, Python, vcpkg and packaging are pending.
> Every implemented target is cache-only; no user-facing image is emitted.

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

## Phase 2: x86_64 vertical slice

The Rocky 8.10 sysroot lock captures a 78-RPM DNF transaction. Validate its
planning manifest and content lock:

```console
$ ./scripts/validate-sysroot-lock.py config/sysroots/el8-x86_64.plan.json
$ ./scripts/validate-sysroot-lock.py locks/sysroot-el8-x86_64.json --require-lock
```

Build the real locked sysroot. RPM downloads are hash- and signature-checked;
assembly runs without network access:

```console
$ docker buildx bake sysroot-x86_64
```

Build and qualify the GTS15-derived cross compiler:

```console
$ docker buildx bake toolchain-x86_64-dev
```

This applies the complete Rocky GCC/binutils SRPM patch sets, builds binutils
2.44 and GCC 15.2.1 for `x86_64-unknown-linux-gnu`, installs the EL8 shared
runtime plus RH `libstdc++_nonshared80`, then exercises C, C++20, LTO,
cross-DSO exceptions, link traces and ABI ceilings. The `-dev` suffix is
intentional: the current host tool closure still comes from live Rocky
repositories. A canonical, replayable DNF resolver/transaction manifest and a
host RPM lock are both required before candidate builds are allowed.

The compiler gate is currently a documented manual/heavy check; regular CI
builds through the locked sysroot only. A locked host closure and suitable
runner capacity are prerequisites for making the toolchain gate mandatory.

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
