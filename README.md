# crossforge

Crossforge is being rewritten as a Docker-first, GTS-derived cross SDK. The
planned public artifact is one `linux/amd64` image containing a native GTS15
host compiler, EL8-targeting cross compilers for x86_64 and aarch64, CPython
3.9–3.14 cross SDKs, common build tools, pinned vcpkg integration, and
build-system-independent DEB/RPM packaging.

> **Not yet publishable as the complete SDK.** Both cross-toolchain slices now
> build from locked host and sysroot transactions. x86_64 passes native smoke;
> aarch64 passes the same compile/ABI gates and explicit QEMU smoke against a
> locked sysroot and clean Rocky arm64 root. CPython 3.9–3.10 legacy, 3.11
> transition and 3.12–3.14 modern rows have completed their amd64 build-Python,
> true dual-target SDK, locked-sysroot and clean-Rocky runtime gates through
> Phase 10. CPython 3.14.7 additionally passes compile qualification with a
> private static zstd 1.5.7. Frozen EL8 ABI sets and Python's complete
> provider-ownership/ELF gates are implemented for both targets. The final SDK
> now rebases those qualified artifacts onto its independently locked host
> runtime and passes the complete offline integration gate. The pinned vcpkg
> registry/tool and five generated host/target triplets are installed and
> qualified offline with locked CMake 4.4.0 and Ninja 1.13.2 host-tool overlays. A real
> no-download overlay-port contract now covers every static/dynamic triplet;
> curated zlib/fmt, TLS, host-generator and large-graph port tiers pass the same
> five-triplet gate from explicit asset closures. The crosspack Phase 14 gate
> emits byte-reproducible split DEB/RPM packages for both targets and installs
> them with pinned real `dpkg` and Rocky `rpm`. The single `crossforge`
> launcher, detached debug packages, dynamic ELF audit, and the complete
> Python/vcpkg/packaging SDK composition are qualified. Full GCC/Qt suites and
> the release supply chain remain pending.
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

`release.json` also carries the intended Crossforge product version. Once a
candidate image has been pushed, create its digest-only identity without
trusting a mutable tag:

```console
$ ./scripts/candidate_manifest.py create \
    --source-commit "$(git rev-parse HEAD)" \
    --digest "$CANDIDATE_DIGEST" \
    --platform-manifest-digest "$CANDIDATE_AMD64_DIGEST" \
    --output candidate.json
$ ./scripts/candidate_manifest.py validate candidate.json \
    --expected-source-commit "$(git rev-parse HEAD)"
```

The manifest binds the product version, canonical release digest, source
commit, public repository, OCI index and `linux/amd64` child manifest. It does
not carry a candidate tag; later qualification and promotion use only the
recorded digests.

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
cross-DSO exceptions, link traces and the frozen ABI contract. The `-dev`
suffix is intentional: full GCC/Qt qualification and the complete release
supply chain are not implemented yet.

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
Compile and final reports bind both the qualification-policy and aggregate
component digests. Runtime preflight re-derives them before touching target
artifacts, and each row manifest requires the two target reports to agree.

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
Python qualification identities. `python-phase9-dev` remains the fixed
cp313+cp311+cp312+cp314+cp310 snapshot. The complete Phase 9 gate has passed
locally; CI repeats the latest matrix on main.

## Phase 10: complete CPython 3.9–3.14 matrix

Build and qualify the final legacy row and six-row aggregate:

```console
$ docker buildx bake python-native-phase10
$ docker buildx bake cpython-cp39-x86_64-qualify-build \
    cpython-cp39-aarch64-qualify-build
$ docker buildx bake cpython-cp39-x86_64-qualify \
    cpython-cp39-aarch64-qualify
$ docker buildx bake python-cp39-dev python-phase10-dev
$ docker buildx bake python-matrix
$ docker buildx bake phase10
```

CPython 3.9.25 is EOL and does not support `--disable-test-modules`, so its
upstream test modules remain installed; no support promise is inferred. Its
`setup.py` also uses an independent `distutils.sysconfig`. The 3.9-specific
gh-115382 backport therefore delegates that loader transactionally to the
source-only stdlib loader. Before `sharedmods`, the build independently proves
stdlib and distutils agree on compiler, linker, ABI and `CONFIG_ARGS` metadata
while the target extension directory remains absent from build-Python
`sys.path`. This prevents both the loud aarch64 lookup failure and the more
dangerous same-SOABI x86_64 host-config fallback.

`python-phase10-dev`, `python-dev` and `python-matrix` select
cp313+cp311+cp312+cp314+cp310+cp39; Phases 5–9 retain their original members.
Adding cp39 preserves all five earlier row-local build component identities
and deliberately rebinds the shared qualification identities. The complete
Phase 10 dual-target, dual-runtime gate has passed locally.

## Phase 11: frozen ABI and Python ELF ownership

Validate the reviewed EL8 provider sets and Python's additional runtime DSO
owners:

```console
$ ./scripts/validate-frozen-abi.py
$ ./scripts/validate-python-runtime-providers.py
$ docker buildx bake phase10
```

The frozen baselines contain 15 x86_64 and 14 aarch64 core providers. Python
adds eight exact SONAMEs owned by seven locked RPMs. Each target has a tracked,
canonical provider catalog; qualification reconstructs it from actual runtime
bytes and compares it exactly. Every Python executable and `lib-dynload` ELF
is re-read at compile finalization, row aggregation and cumulative SDK append.
The audit binds ELF class, endianness, role, hardening, dependency closure,
versioned imports, COPY relocations and deterministic strong/weak symbol
ownership. The locked tier byte-checks all providers; clean Rocky permits core
errata byte differences only when the full reviewed ELF catalog is unchanged,
while Python's eight external providers remain byte-exact in both tiers.
`config/release.json` pins the eight canonical ABI inputs by logical path and
digest. They project into two target-baseline components and one shared Python
provider component; only toolchain/Python qualification identities change,
while every row-local build identity remains stable.

## Phase 12: independent host runtime and final SDK rebase

The user-facing amd64 runtime is now an independent 43-root Rocky transaction,
not a delta from any compiler or CPython build image:

```console
$ ./scripts/validate-rpm-lock.py \
    locks/host-runtime-el8-x86_64.json --require-lock
$ docker buildx bake host-runtime-qualified
$ docker buildx bake python-phase10-dev
```

Maintainers refresh the evidence with the cache-only
`rpm-lock-host-runtime` target and review its local output before replacing the
tracked transaction, lock, and signed `repomd.xml` files.

The lock contains 152 verified RPM payloads and replays 131 installs plus 21
upgrades and matching removals offline. It includes native GTS15, CMake, Meson, distro Ninja,
Autotools,
Git core, pkg-config and common archive/text tools. PowerTools may supply only
Meson and Ninja. Crossforge build-only RPM tooling and CPython dependency
development roots are rejected from the resulting closure. Replacing the
old SDK ancestor is complete: the cumulative image inherits only this runtime,
then copies the two qualified toolchains, six Python rows and static QEMU.
Its networkless final gate rehashes the RPMDB/marker, prior toolchain reports,
sysroot locks, every build/target Python tree and QEMU; it also builds and runs
C, C++ and LTO probes for both targets. Build-only roots and markers are absent
from the resulting cache-only image.

## Phase 13: vcpkg source and SDK integration

Crossforge pins the immutable vcpkg `2026.07.29` release at commit
`9e593bb18ea69cc5095e012465dcd675a822ed0d`. Its matching vcpkg-tool
`2026-07-27` binary is independently bound by SHA256, the upstream SHA512,
Microsoft's detached PGP signature and exact LICENSE/NOTICE files:

```console
$ ./scripts/validate-supply-chain-evidence.py
$ docker buildx bake ninja-host-tool
$ docker buildx bake cmake-host-tool
$ docker buildx bake vcpkg-source
$ ./scripts/render-vcpkg-integration.py --check
$ docker buildx bake sdk-phase13-base
$ docker buildx bake vcpkg-contract-qualified
$ docker buildx bake vcpkg-upstream-tier3-qualified
```

The host-tool target installs Ninja 1.13.2 at
`/opt/crossforge/host-tools/ninja/1.13.2` without replacing RPM-owned
`/usr/bin/ninja`. Its lightweight tag mapping, commit object, mutable GitHub
release response, binary ZIP, vcpkg SHA512, extracted ELF and Apache-2.0
license are independently bound. The offline gate verifies the loader and
dependencies, then runs direct Ninja, CMake/Ninja and Meson/Ninja builds.
CMake 4.4.0 is the exact Linux tool selected by the pinned vcpkg tool database;
its archive, `cmake`/`ctest`/`cpack` ELF payloads and license are independently
bound. Its offline gate enforces a glibc 2.17 ceiling and runs CMake/Ninja,
CTest and CPack without replacing the RPM-owned CMake.

The source target clones the complete commit history and fetches the 22 fixed
port trees referenced by the version database but not reachable from the tag.
It rejects shallow repositories and batch-checks all 39,823 historical trees.
Bootstrap is never executed online. Network access is confined to fetching
content-addressed registry objects and the signed tool; checkout, signature
verification, tool execution and scratch export run offline. The five
Crossforge overlay triplets chainload explicit native GTS15 or target CMake
toolchains. No default target triplet is set: downstream builds must select
x86_64 or aarch64 and static or dynamic linkage deliberately. The cache-only
SDK gate rechecks the complete Git/tool identity, all generated file hashes,
Ninja selection, host/target separation and PIC shared linking, then runs
x86_64 directly and aarch64 only through pinned QEMU. This vcpkg base consumes
the two toolchain slices directly and does not depend on the Python matrix. The
separate contract gate executes `vcpkg install` for all five triplets with
downloads and binary caches disabled. Its target port consumes and runs a
native host dependency,
builds both library linkages, checks the exact `$ORIGIN` shared-library
RUNPATH, and executes both target consumers. The only preseeded vcpkg helper
asset is an exact hash/size-bound patchelf archive; neither it nor downloads,
build trees, packages, or installed ports enter the product root.
Tier 1 already builds the curated `zlib 1.3.2#1` and `fmt 12.2.0#1` ports in
all five triplets. Three URL/SHA256/SHA512/size-bound source assets are fetched
in a network stage, revalidated offline, and seeded into isolated downloads
roots. The gate validates installed versions, linkage, target ELF machines,
`$ORIGIN` shared-library RUNPATHs, then executes a combined zlib/fmt C++
consumer natively or through pinned QEMU. Tier 2 additionally builds
`OpenSSL 3.6.3` and
`curl 8.21.0#1` with the exact `curl[core,openssl]` feature set. It validates
static and dynamic crypto/TLS libraries and executes a combined C consumer in
all five triplets. Tier 3 pins protobuf `6.33.4#2`, Boost `1.91.0` and the
`boost-json` compiled module. Its 23 source/license assets are fetched and
rehash-verified separately. The gate builds native `protoc 33.4`, audits and
executes that host tool, generates C++ from a bound `.proto`, then links and
runs a Protobuf/Boost.JSON consumer for every triplet. The host install is
copied only between isolated qualification roots to avoid rebuilding protoc;
no binary cache or installed port enters the product image.

## Phase 14: build-system-independent DEB/RPM packaging

```console
$ python3 -m unittest discover -s tests/packaging -p 'test_*.py'
$ docker buildx bake nfpm-tool
$ docker buildx bake packaging-sdk-dev
$ docker buildx bake packaging-qualified
```

nFPM 2.47.0 is locked by release/tag/commit, binary and source archive hashes,
the upstream checksum manifest, an archived Sigstore bundle, and the selected
MIT license. Only the download stage has network access; offline preparation
revalidates the complete closure and exports a minimal host-tool root.

`crosspack` consumes a strict JSON manifest and a staged filesystem. It rejects
unowned or multiply owned files, unsafe paths/modes/symlinks, destination-tree
collisions, wrong-target ELF files, and undeclared component edges. Phase 14
cross-builds x86_64 and aarch64 probe payloads, emits runtime/development/tools/debug
DEB and RPM sets twice, proves byte identity, installs all sixteen packages into
isolated roots using real package managers, and rehashes installed payloads.
No package or installer root enters the SDK image.

The internal schema now exposes format-specific DEB/RPM relations and exact
component edges. Per-file attributes can override a single staged entry's safe
mode, owner and group, and can mark configuration files as `config` or
`noreplace`; these values are recorded in the canonical plan and verified from
the installed packages. Recursive attribute overrides, unsafe modes and unknown
relation fields fail closed.
The project version is shared, while DEB and RPM carry independent numeric
epochs and release strings; generated component dependencies bind the exact
format-specific epoch-version-release.
Each component can independently assign DEB and RPM `pre_install`,
`post_install`, `pre_remove`, and `post_remove` scripts. Paths are resolved
relative to the manifest, and only small, regular, non-symlink UTF-8 files with
an exact `#!/bin/sh` interpreter are accepted. Crosspack seals their bytes,
records source, interpreter, size, and SHA-256 in the canonical plan, and gives
nFPM only the sealed copy. Components without scripts omit the field; components
with scripts declare only the formats and hooks they use, while the plan expands
the complete matrix. Qualification executes every hook during real
install, upgrade, and removal transactions for both package formats and target
architectures.
Components default to the selected target architecture. A component explicitly
marked `independent` emits DEB `all` and RPM `noarch`, rejects ELF and common
target-generated artifact classes, and is recorded as declared-independent in
its plan. Crossforge's dual-target qualification upgrades that claim to
verified-independent only after the complete component plan and both base and
upgrade package bytes match exactly between x86_64 and aarch64.
Each component has a bounded single-line summary and a canonical long
description with optional paragraphs. Crosspack constructs the DEB synopsis/body
and explicitly maps RPM Summary/Description, while real package queries verify
the summary, long description, and RPM license metadata.
File mappings keep a single destination string when both formats use the same
path, or provide explicit `deb` and `rpm` destinations when their filesystem
conventions differ. The canonical plan expands both layouts and performs ELF,
RUNPATH, provider, collision, symlink, debug-file, install, and payload-hash
checks independently. Qualification installs shared libraries below Debian's
target multiarch libdir and RPM's `/usr/lib64`, with matching format-specific
debug paths.

When `debug_symbols` names an otherwise empty debug component, crosspack uses
the selected target `objcopy` on a private staging copy and adds a matching GNU
debuglink. Target `readelf` then records SONAME, `DT_NEEDED`, safe origin-only
RUNPATH and an export digest; RPATH, TEXTREL, parent traversal and providers not
present in the package set or locked sysroot fail the build. Origin-relative
RUNPATH entries are resolved from each final package destination: a canonical
`$ORIGIN/../lib` is accepted only inside one root-owned private application
prefix, every provider must be uniquely reachable, and cross-component
providers require an explicit component dependency.

The SDK exposes one launcher. Backend paths and nFPM identities are internal:

```console
$ crossforge --version
$ crossforge info --json
$ crossforge env --target aarch64 --python 3.14 --vcpkg --json
$ crossforge run --target aarch64 --vcpkg --linkage dynamic -- cmake --build build
$ crossforge shell --target x86_64
$ crossforge package seal --config crosspack.json --staging-root stage --variant-id "$VARIANT_ID" --resolution-sha256 "$RESOLUTION_SHA256" --output staging.json
$ crossforge package plan --config crosspack.json --staging-root stage --staging-manifest staging.json --format rpm --output plan.json
$ crossforge package build --config crosspack.json --staging-root stage --staging-manifest staging.json --format rpm --output-directory dist
```

`package plan` and `package build` accept `--format deb`, `--format rpm`, or
`--format both` (the default). The canonical plan records the selection and the
encoder emits only those formats; qualification checks that selective RPM bytes
equal the corresponding artifacts from a full two-format build.
`package seal` creates a new immutable manifest that binds the package config,
target, variant ID, optional dependency-resolution digest, and exact staged
path/type/mode/size/content/ELF inventory. `plan` and `build` require that
manifest and re-inventory the tree before debug preparation and before every
package encoding; replacement output and any post-seal change fail closed.

`env` prints only Crossforge-managed variables and never dumps arbitrary
inherited secrets. `run` and `shell` build a copied child environment, then
replace the launcher process so exit status and signals reach the real command;
they never mutate the container environment or infer a target from the project.
Selecting vcpkg switches CMake to its toolchain and an explicit static/dynamic
Crossforge triplet. Its downloads and binary caches live under the writable
`CROSSFORGE_CACHE_ROOT`, never inside `/opt/crossforge/vcpkg/root`. The generated
Meson cross-file path is exposed as `MESON_CROSS_FILE`. The Python selector is
fail-closed until the final aggregate image adds the qualified Python rows to
this packaging/vcpkg base. Phase 15 provides that aggregate as the cache-only,
non-publishable `sdk-complete-dev` target.

## Phase 15: complete SDK composition

```console
$ docker buildx bake sdk-complete-dev
```

The target starts from the packaging-qualified vcpkg SDK and copies only the
six already-qualified Python rows and their final report. An offline gate then
uses the installed launcher to check the native host environment plus all 24
x86_64/aarch64 × Python 3.9–3.14 × static/dynamic vcpkg selections. The target
remains `-dev`; it is not a release candidate or a user-facing tag.

The separate `sdk-candidate` target is the only registry-export boundary. It
inherits the complete SDK, revalidates the product identity, requires the full
source commit, and adds OCI version/revision annotations. Its checked-in Bake
output is still cache-only and has no tag, so local commands cannot publish it
accidentally. The manually dispatched `public candidate` workflow supplies a
unique `candidate-v<version>-g<commit>-r<run>-a<attempt>` tag, pushes with max
provenance and SBOM attestations, reconstructs `candidate.json` from the raw
OCI index, then logs out of GHCR and proves the digest is anonymously readable.
It never creates a SemVer or stable-channel tag.

The public candidate runs as `crossforge` UID/GID 1000 by default. `/opt/crossforge`
remains root-owned; only the workspace, home, cache and temporary directories are
writable. `docker run --user <uid>:<gid>` is supported: if the inherited home is
not writable, the launcher creates an isolated `/tmp/crossforge-<uid>` fallback.

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
