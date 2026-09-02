# crossforge

Crossforge is being rewritten as a Docker-first, GTS-derived cross SDK. The
planned public artifact is one `linux/amd64` image containing a native GTS15
host compiler, EL8-targeting cross compilers for x86_64 and aarch64, CPython
3.9–3.14 cross SDKs, common build tools, pinned vcpkg integration, and
build-system-independent DEB/RPM packaging.

> **Not yet publishable as the complete SDK.** Both cross-toolchain slices now
> build from locked host and sysroot transactions. x86_64 passes native smoke;
> aarch64 passes the same compile/ABI gates and explicit QEMU smoke against a
> locked sysroot and clean Rocky arm64 root. CPython 3.10 legacy, 3.11
> transition and 3.12–3.14 modern rows have completed their amd64 build-Python,
> true dual-target SDK, locked-sysroot and clean-Rocky runtime gates through
> Phase 9. CPython 3.14.7 additionally passes compile
> qualification with a private static zstd 1.5.7. Python 3.9, vcpkg,
> packaging, frozen ABI sets, the full GCC/Qt suites and the independent
> minimal host-runtime lock remain pending.
> Every implemented target is cache-only; no user-facing image is emitted.

The accepted implementation contract is in
[`docs/architecture.md`](docs/architecture.md). The original Rust prototype is
preserved at tag `prototype-rust-2026-08-28` and has been removed from the
rewrite branch.

## Phase 1

Validate the canonical release configuration:

```console
$ ./scripts/validate-release.py
$ ./scripts/validate-supply-chain-evidence.py
```

Unverified source pins are represented explicitly as `pending`, never with
invented URLs or hashes. Candidate and release builds will require:

```console
$ ./scripts/validate-release.py --require-locked
```

Check that the cache-scoped release projections and Bake override match
`config/release.json`:

```console
$ ./scripts/render-release-components.py --check
$ ./scripts/render-bake.py --check
```

Files under `config/generated/` are deterministic build, qualification,
supply, and future-policy projections. Do not edit them by hand.

Normal GTS source, sysroot, and host-RPM stages consume only their exact
projection and canonical digest. Full `release.json` input is confined to
validation, lock maintenance, and qualification boundaries; changing unrelated
future metadata therefore requalifies without recompiling GCC or binutils.

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
intentional: frozen ABI sets, full GCC/Qt qualification and the complete
release supply chain are not implemented yet.

The compiler and dual-target Python gates remain heavy candidate checks.
Regular PR CI validates their graph, locked inputs, clean runtime overlays and
native build Python without rebuilding both GCC toolchains from scratch.

## Phase 3: reproducible RPM foundation

Host preparation is split deliberately: common tools contain the exact
qualified binutils environment; `bison`, `flex`, `libzstd-devel` and `m4` form
a GCC-only additive lock; CPython development libraries form a third additive
lock. Validate and install them offline:

```console
$ ./scripts/validate-rpm-lock.py locks/host-build-common-el8-x86_64.json --require-lock
$ ./scripts/validate-rpm-lock.py locks/host-gcc-build-el8-x86_64.json --require-lock
$ ./scripts/validate-rpm-lock.py locks/host-python-build-el8-x86_64.json --require-lock
$ docker buildx bake host-build-common-locked host-gcc-build-locked \
    host-python-build-locked
```

Maintenance targets run the canonical resolver in the pinned Rocky image and
emit reviewable transaction, RPM and metadata evidence. They never participate
in normal builds:

```console
$ docker buildx bake rpm-lock-host-build-common \
    --set rpm-lock-host-build-common.output=type=local,dest=work/lock-refresh/common
```

Equivalent targets exist for both `rpm-lock-sysroot-*` targets and
`rpm-lock-host-gcc-build` and `rpm-lock-host-python-build`; refresh common
before either delta. Bake forces
these maintenance targets to bypass cache.

These are build-environment locks, not the final image runtime. That runtime is
resolved independently from a clean Rocky base after its user-facing tool set
is frozen.

Every transaction records canonical install/upgrade/remove actions, exact DNF
reasons, base/remove/result RPM inventories, repository metadata and detached
signatures. Normal builds use only committed locks and perform package
installation under `--network=none`.

## Phase 4: aarch64 vertical slice

Validate and assemble the independently locked aarch64 sysroot:

```console
$ ./scripts/validate-rpm-lock.py locks/sysroot-el8-aarch64.json --require-lock
$ docker buildx bake sysroot-aarch64
```

Build the second cross compiler and run its qualification graph:

```console
$ docker buildx bake qemu-aarch64-validated
$ docker buildx bake toolchain-aarch64-dev
$ docker buildx bake phase4
```

GCC/binutils `%prep` runs separately for each target architecture. aarch64
smoke execution does not use host binfmt: an amd64 static QEMU 10.2.3 binary is
bound by image manifest and binary SHA256, then invoked explicitly with the
Armv8-A `cortex-a53` model against both the locked sysroot and a clean,
manifest-pinned Rocky arm64 root. The structured report records both runtime
legs, artifact hashes, normalized loader dependencies, and the observed QEMU
identity.

The release config distinguishes the pinned binfmt builder commit and SLSA
predicate from QEMU's own annotated tag and peeled source commit. QEMU remains
a development qualification tier; the upstream predicate records the QEMU
version argument but not its in-build Git checkout, so native EL8/aarch64
release execution remains mandatory.

Exact Rocky index, QEMU OCI/SLSA, and QEMU Git tag/commit bytes are checked in
under `evidence/` as base64 envelopes. The evidence validator recomputes their
content identities and verifies the index-to-manifest, attestation, provenance,
builder, checkout, signed-tag, and source-commit relationships offline.

## Phase 5: representative CPython 3.13 slice

Build and qualify the first complete Python matrix row:

```console
$ docker buildx bake cpython-cp313-x86_64-qualify
$ docker buildx bake cpython-cp313-aarch64-qualify
$ docker buildx bake python-cp313-dev
$ docker buildx bake python-phase5-dev
$ docker buildx bake phase5
```

CPython 3.13.15 is built once as an amd64 build interpreter and twice as a
real cross target. Cross stages provide no `HOSTRUNNER` or QEMU and deny/audit
the supported dynamic libc/loader execution paths. Qualification
compiles a minimal extension, audits every `lib-dynload` ELF and runs zlib,
bz2, lzma, ctypes, OpenSSL, SQLite, UUID, threading, semaphore, timezone,
resolver, wide-character and PTY probes. Each target runs against both its
exact build sysroot and the pinned Rocky 8.10 image augmented with seven exact,
signed runtime RPMs. That `--nodeps` overlay is qualification-only, not a
deployable package transaction. aarch64 uses only the locked explicit QEMU
executor.

All six CPython 3.9–3.14 source tarballs are content-locked. Their upstream
Sigstore bundles are archived and structurally bound to those digests, but are
explicitly marked `archived-unverified`: cryptographic Fulcio/Rekor verification
is a later release-supply-chain gate and is not claimed by this phase.

## Phase 6: parameterized CPython rows

Build the 3.11 transition row or the fixed-membership two-row Phase 6 snapshot:

```console
$ docker buildx bake cpython-cp311-x86_64-qualify
$ docker buildx bake cpython-cp311-aarch64-qualify
$ docker buildx bake python-cp311-dev
$ docker buildx bake python-phase6-dev
$ docker buildx bake phase6
```

`docker/python.Dockerfile` implements one row pipeline; Bake supplies exact
version, adapter and target edges from `release.json`. Rows build and qualify
independently, export through scratch, and enter phase snapshots only through an
append-only aggregation chain. `python-phase6-dev` is permanently limited to
cp313+cp311 even as the latest `python-dev` matrix grows. Rows do not inherit
each other's build state. Row-local source and build projections preserve
unrelated component identities; changes to shared implementation scripts still
invalidate the corresponding BuildKit layers. Aggregate qualification identity
changes whenever the supported matrix or its policy changes.

CPython 3.11.16 carries a hash-locked backport of upstream gh-115382 so the
amd64 build interpreter cannot discover same-SOABI target extensions through
`PYTHONPATH`. The cross stage also exercises and audits eleven dynamic libc
execution/spawn APIs plus `dlopen` and `dlmopen`. This `LD_PRELOAD` policy is
defense in depth, not a sandbox; it does not mediate direct syscalls or static
programs. Runtime qualification mounts real `tmpfs` instances at `/dev/shm`
and exercises an actual `multiprocessing.Lock()` on both targets. Each row
manifest binds its prepared source, patches, complete build/target SDK tree
identities and both self-validating qualification reports.

## Phase 7: fixed-membership CPython 3.12 snapshot

Build and qualify the third row and its fixed-membership aggregate graph:

```console
$ docker buildx bake python-native-phase7
$ docker buildx bake cpython-cp312-x86_64-qualify
$ docker buildx bake cpython-cp312-aarch64-qualify
$ docker buildx bake python-cp312-dev
$ docker buildx bake python-phase7-dev
$ docker buildx bake python-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase7
```

CPython 3.12.14 uses the modern Makefile extension build, but the 3.12 branch
did not receive the same-SOABI isolation fix. Crossforge therefore carries a
separate hash-locked gh-115382 backport for the 3.12 source layout. The shared
row contract defines its adapter, absent `Py_GIL_DISABLED` policy and Phase 7
introduction once; build, qualification, runtime and Bake consume that same
contract. `python-phase7-dev` remains limited to cp313+cp311+cp312 and appends
cp312 only through its qualified scratch row.

## Phase 8: CPython 3.14 with private zstd

Build locked zstd 1.5.7 once for the amd64 build interpreter and once per
target, then exercise CPython 3.14's compile and dual-runtime gates:

```console
$ docker buildx bake zstd-source zstd-host-build \
    zstd-x86_64-build zstd-aarch64-build
$ docker buildx bake python-native-phase8
$ docker buildx bake cpython-cp314-x86_64-qualify-build \
    cpython-cp314-aarch64-qualify-build
$ docker buildx bake cpython-cp314-x86_64-qualify \
    cpython-cp314-aarch64-qualify
$ docker buildx bake python-cp314-dev python-phase8-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase8
```

CPython 3.14.7 uses the modern adapter and an isolated build/target sysconfig
contract. Rocky 8's zstd is too old, so Crossforge builds private PIC static
zstd 1.5.7 prefixes for host, x86_64 and aarch64. Only `_zstd` consumes them;
the immutable sysroots are unchanged. Compile qualification verifies the exact
zstd build manifests and component identities, a unique `_zstd`, static symbol
resolution and the absence of `libzstd.so`, exported private symbols, RPATHs
and text relocations. Both locked-sysroot and clean-Rocky runtime tiers execute
one-shot, streaming, dictionary, multithreaded, tarfile and zipfile zstd probes.

Phase 5, 6 and 7 retain fixed row membership at cp313, cp313+cp311 and
cp313+cp311+cp312. `python-phase8-dev` remains limited to the four-row
cp313+cp311+cp312+cp314 set. Release or qualification-policy maintenance may
rebind those reports; these are cache-only development artifacts, not immutable
release snapshots.

## Phase 9: CPython 3.10 legacy adapter

Build and qualify CPython 3.10 without weakening the true-cross or
same-SOABI isolation contracts:

```console
$ docker buildx bake python-native-phase9
$ docker buildx bake cpython-cp310-x86_64-qualify-build \
    cpython-cp310-aarch64-qualify-build
$ docker buildx bake cpython-cp310-x86_64-qualify \
    cpython-cp310-aarch64-qualify
$ docker buildx bake python-cp310-dev python-phase9-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase9
```

CPython 3.10.21 predates `--with-build-python`, `HOSTRUNNER` and the modern
Makefile extension build. The legacy adapter therefore supplies exact
`PYTHON_FOR_BUILD` and `PYTHON_FOR_REGEN` commands, uses `setup.py` under
`PYTHONSTRICTEXTENSIONBUILD=1`, and rejects unsupported configure options.
Crossforge carries a separately hash-locked gh-115382 backport for the 3.10
source layout so the build interpreter loads target sysconfigdata as source
without exposing target extension directories through `PYTHONPATH`.

Both target triples remain real cross builds, including x86_64. Qualification
expects CPython 3.10's `siphash24` runtime contract and independently audits
required extensions because legacy `setup.py` reports some missing modules
without failing. Adding cp310 preserves every cp311–cp314 source, build-policy,
native and target build component digest; it deliberately changes the shared
Python qualification identities. `python-phase9-dev`, `python-dev` and
`python-matrix` select cp313+cp311+cp312+cp314+cp310. Python 3.9 remains the
last planned legacy row. The complete Phase 9 gate has passed locally; CI
repeats the latest matrix on main.

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
