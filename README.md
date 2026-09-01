# crossforge

Crossforge is being rewritten as a Docker-first, GTS-derived cross SDK. The
planned public artifact is one `linux/amd64` image containing a native GTS15
host compiler, EL8-targeting cross compilers for x86_64 and aarch64, CPython
3.9–3.14 cross SDKs, common build tools, pinned vcpkg integration, and
build-system-independent DEB/RPM packaging.

> **Not yet publishable as the complete SDK.** The first real x86_64
> cross-toolchain slice now builds from locked host and sysroot transactions
> and passes smoke qualification. aarch64, Python, vcpkg and packaging remain
> pending, as does the independent minimal host-runtime lock required for the
> final user image.
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

The Rocky 8.10 sysroot is a canonical 78-RPM DNF transaction. Validate its
plan and signed content lock:

```console
$ ./scripts/validate-rpm-lock.py config/rpm/sysroot-el8-x86_64.plan.json
$ ./scripts/validate-rpm-lock.py locks/sysroot-el8-x86_64.json --require-lock
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
intentional: the second target, frozen ABI sets, full GCC/Qt qualification and
release supply chain are not implemented yet.

The compiler gate is currently a documented manual/heavy check; regular CI
builds both locked host layers and the sysroot, but not the full compiler.

## Phase 3: reproducible RPM foundation

Host preparation is split deliberately: common tools contain the exact
qualified binutils environment, while `bison`, `flex`, `libzstd-devel` and `m4`
form a GCC-only additive lock. Validate and install both offline:

```console
$ ./scripts/validate-rpm-lock.py locks/host-build-common-el8-x86_64.json --require-lock
$ ./scripts/validate-rpm-lock.py locks/host-gcc-build-el8-x86_64.json --require-lock
$ docker buildx bake host-build-common-locked host-gcc-build-locked
```

Maintenance targets run the canonical resolver in the pinned Rocky image and
emit reviewable transaction, RPM and metadata evidence. They never participate
in normal builds:

```console
$ docker buildx bake rpm-lock-host-build-common \
    --set rpm-lock-host-build-common.output=type=local,dest=work/lock-refresh/common
```

Equivalent targets exist for `rpm-lock-sysroot-x86_64` and
`rpm-lock-host-gcc-build`; refresh common before its GCC delta. Bake forces
these maintenance targets to bypass cache.

These are build-environment locks, not the final image runtime. That runtime is
resolved independently from a clean Rocky base after its user-facing tool set
is frozen.

Every transaction records canonical install/upgrade/remove actions, exact DNF reasons,
base/remove/result RPM
inventories, repository metadata and detached signatures. Normal builds use
only committed locks and perform package installation under `--network=none`.

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
