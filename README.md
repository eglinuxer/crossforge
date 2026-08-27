# crossforge

Build once, run on old Linux. crossforge is an open-source tool that **builds
cross-compilation toolchains** solving the classic glibc / libstdc++
compatibility problem: binaries compiled with a modern GCC that run unmodified
on distributions as old as RHEL 8 / Rocky 8 (glibc 2.28).

- **glibc** is pinned by linking against a binary **sysroot** assembled from an
  old distro's packages — no from-source glibc builds, no multi-stage bootstrap.
- **libstdc++** is pinned by **nonshared hybrid linking** (Red Hat devtoolset's
  mechanism, cross-compiled): only symbols newer than the baseline
  `libstdc++.so.6` are statically linked, keeping full C++ ABI interop with
  system-compiler-built code. The default toolchain sources are the RH
  gcc-toolset-14 SRPM (GCC 14.2.1 + Red Hat's hand-tuned compat patches).
- An **audit gate** verifies every produced ELF stays within its declared
  baseline (symbol versions, DT_NEEDED whitelist, DT_RELR, interpreter, arch).

Verified end to end: C++20 (`std::format`, ranges) binaries built by these
toolchains run on stock AlmaLinux/Rocky 8, Ubuntu 20.04 and Debian 11.

## Use a prebuilt toolchain (Docker)

```console
$ docker pull ghcr.io/eglinuxer/crossforge/toolchain:el8-x86_64
$ docker run --rm -v "$PWD:/src" -w /src \
    ghcr.io/eglinuxer/crossforge/toolchain:el8-x86_64 \
    x86_64-unknown-linux-gnu-g++ -std=c++20 hello.cpp -o hello
```

Every commit to `main` publishes all supported combinations
(gcc 14.2.1 + 11.2.1 × el8 × x86_64/aarch64) via the `toolchain-images`
workflow. Tags: `<baseline>-<target>` (default gcc),
`<baseline>-<target>-gcc<version>`, and a `-<sha>` suffix for pinning exact
builds. el8 = glibc 2.28 / GLIBCXX 3.4.25.

The whole supply chain is Rocky Linux — container bases, sysroot packages
and the gcc-toolset SRPMs alike. An el7 baseline was dropped for that
reason: Rocky starts at 8, so it could only have come from the EOL CentOS 7
vault. The baseline registry is a TOML table, so a downstream that still
needs el7 can register it (with its own source) without touching code.

## Build a toolchain yourself

```console
$ cargo build --release
$ docker build -t crossforge-buildenv:el8 -f docker/buildenv.Dockerfile docker
$ ./target/release/crossforge build --baseline el8 --target aarch64 \
    --image crossforge-buildenv:el8
$ ./target/release/crossforge audit --sysroot work/sysroots/el8-aarch64 \
    --arch aarch64 your-binary.so
$ ./target/release/crossforge verify ./your-app --images almalinux:8,ubuntu:20.04
$ ./target/release/crossforge check --baseline el8 --target x86_64   # GCC testsuite
```

`check` runs the upstream GCC DejaGnu testsuites (`check-gcc`, `check-c++`,
`check-target-libstdc++-v3`) against a built toolchain, with generated board
files (direct execution for x86_64, user-mode qemu for aarch64). The default
el8/x86_64 toolchain scores 466,138 passes with ZERO unexpected failures in
the gcc and c++ suites; the three remaining libstdc++ FAILs are RH-documented
baseline-semantics differences (old-baseline string::reserve behavior -- the
nonshared design goal) plus one no-network DNS test.

Each prefix ships a `toolchain.cmake` for CMake projects:

```console
$ cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=/opt/crossforge/<id>/toolchain.cmake
```

Always use it when cross-compiling: without CMAKE_SYSTEM_NAME/PROCESSOR,
CMake treats the cross compiler as native and host-arch-sensitive project
logic misfires (vcpkg-tool, for example, then selects x86-era libcurl
headers for an aarch64 build). `crossforge smoke` runs the built-in
dlopen + cross-DSO-exception health check for any built toolchain.

The result is a fully relocatable prefix — tar it up, unpack anywhere, no
configuration needed. `--pack` produces a distributable `tar.zst` + TOML
manifest. Building inside the el8 container keeps the toolchain's own host
requirement at glibc ≥ 2.28. See `docker/buildenv.Dockerfile` for the build
environment's package rationale (notably `glibc-gconv-extra`: without the full
gconv module set, GCC's configure probe disables iconv and the built cc1
silently loses `-fexec-charset` support).

Sanitizers are built in (ASan, UBSan, TSan, LSan, HWASan). The baseline's
own `libasan.so.5` predates GCC 14's `libasan.so.8`, so link them statically
to stay inside the baseline — `-fsanitize=address -static-libasan` runs and
passes the audit, while the dynamic form is correctly rejected as needing a
library the target does not have.

Hardening is opt-in rather than baked in, because silently changing what a
compiler emits is how build systems acquire mysteries:

```console
$ gcc -specs=<prefix>/share/crossforge/hardened.specs -O2 ...
```

That adds `-fstack-protector-strong`, `-D_FORTIFY_SOURCE=2` (only while
optimizing, never overriding a level you set) and full RELRO. PIE is left
out on purpose: it is the one option that breaks projects with non-PIC
static libraries, so add `-fPIE -pie` deliberately.

What is *not* a policy question the audit now rejects outright: an
executable stack (`PT_GNU_STACK` with the execute bit — hardened kernels
refuse to load it, and GCC emits one for nested functions) and text
relocations.

Available compilers (`--gcc`): `14.2.1` (default, RH gcc-toolset-14) and
`11.2.1` (RH gcc-toolset-11) for projects that need the older compiler.
More versions are a TOML registry entry away
(`src/registry/toolchain-sources.toml`).

## Python packs (cross wheel building, M6)

`crossforge python` builds relocatable CPython installations for wheel
cross-compilation — the arch-specific material (target `pyconfig.h`,
`_sysconfigdata_*.py`, and an interpreter for import smoke tests) for every
supported version:

```console
$ ./target/release/crossforge python --image crossforge-buildenv:el8
```

For each of CPython 3.9/3.10/3.11/3.12/3.13 this produces an x86_64 pack
(built natively in the el8 container with the crossforge toolchain; it doubles
as the build-python) and an aarch64 pack (cross-compiled; 3.11+ via the
official `--with-build-python`, 3.9/3.10 via the legacy PYTHON_FOR_BUILD
path). Configure options track the official manylinux_2_28 image builds
(`--disable-shared --with-ensurepip=no`, same `/opt/_internal/cpython-<v>`
prefix) so both trees diff cleanly against the official images as a
supply-chain cross-check. Every pack must pass an import smoke test covering
all external-library modules (`zlib, bz2, lzma, ctypes, ssl, hashlib,
sqlite3, uuid`) — natively for x86_64, under user-mode qemu against the
toolchain sysroot for aarch64. Packs are production-trimmed like the official
images (stripped interpreter and extensions, no static libpython, no test
suite: ~64MB per pack, matching the official 66MB). Wheels target
`manylinux_2_28` only, so packs build for the el8 baseline only.

`scripts/compare-manylinux-python.py` is the supply-chain cross-check gate:
it diffs every pack's `pyconfig.h` + `_sysconfigdata_*.py` against the
official manylinux_2_28 images and fails on any ABI-relevant difference.

### Prebuilt packs

Every commit publishes each pack as its own image, so wheel builds need no
local CPython build:

```console
$ ./target/release/crossforge python --pull --image crossforge-buildenv:el8
```

This materializes `work/python-packs/<tag>-<arch>/` from
`ghcr.io/eglinuxer/crossforge/python:<tag>-<arch>` (add `--image-ref <sha>`
to pin a commit) and import-smokes each pulled pack. The images are also
directly usable interpreters on their own architecture, since a pack is
installed at the prefix it was configured for:

```console
$ docker run --rm ghcr.io/eglinuxer/crossforge/python:cp312-aarch64 \
    python3.12 -c 'import ssl; print(ssl.OPENSSL_VERSION)'
```

Tagged releases additionally carry every pack as a `tar.zst` asset with a
TOML sidecar (`crossforge python --pack --out dist` produces the same
locally). Note that packs target the el8 baseline: running one outside a
baseline container needs baseline-era runtime libraries, which is why the
smoke runs under `--image`.

## Wheels (M7): a cross cibuildwheel

`crossforge wheel` takes a project directory to compliant `manylinux_2_28`
wheels across the full CPython × arch matrix in one command:

```console
$ ./target/release/crossforge wheel path/to/project \
    --image crossforge-buildenv:el8 --verify-manylinux
```

Per (version, arch) it assembles a build environment from the python packs
(venv + pinned pip; for cross builds the conda-forge-proven
`_PYTHON_SYSCONFIGDATA_NAME` / `_PYTHON_HOST_PLATFORM` mechanism plus
`CMAKE_TOOLCHAIN_FILE`/FindPython hints and `PYO3_CROSS_LIB_DIR`), drives
the project's PEP 517 backend through pip, then gates the result:

1. **Policy audit** against the embedded manylinux_2_28 table (transcribed
   from auditwheel): symbol-version ceilings — including the GLIBCXX 3.4.24
   ceiling that sits one step *below* el8's own libstdc++ — DT_NEEDED
   whitelist, no-libpython rule, extension-suffix/tag consistency, RECORD
   integrity. Only audited wheels get retagged `linux_*` →
   `manylinux_2_28_*`.
2. **Import smoke** with the target pack — natively for x86_64, under qemu
   for aarch64; abi3 wheels fan out across every interpreter version.
3. `--verify-manylinux`: import check inside the official
   `quay.io/pypa/manylinux_2_28_*` container with the image's own
   interpreter (aarch64 via binfmt qemu).

Four build backends are covered by the same environment layer:
setuptools, scikit-build-core/CMake, maturin/PyO3, and anything else
driven through PEP 517. Rust extensions need the Rust build image
(`docker build -t crossforge-buildenv:el8-rust -f docker/buildenv-rust.Dockerfile docker`)
and are cross-compiled through `CARGO_BUILD_TARGET` plus a per-target
linker pointing at the crossforge cross gcc — which is also what keeps
*native* Rust wheels inside the policy's glibc ceiling, since the host gcc
would link against a much newer glibc.

C++ wheels are where the toolchain's nonshared hybrid linking pays off:
`std::from_chars`/`std::filesystem` (GLIBCXX_3.4.29+ material) get
statically carried while the wheel's dynamic requirement stays at
GLIBCXX ≤ 3.4.21 — inside a policy ceiling that even el8's system
libstdc++ exceeds. The acceptance samples are `examples/wheel-setuptools` (plain C),
`examples/wheel-nanobind` (C++, scikit-build-core/CMake),
`examples/wheel-vendored` (linking libssl), `examples/wheel-abi3`
(Limited API — one wheel per arch for every interpreter) and
`examples/wheel-pyo3` (Rust/maturin).

Wheels linking libraries outside the policy whitelist get them **vendored
automatically** (the auditwheel-repair counterpart, in pure Rust): the
library and its transitive dependencies are copied into
`<distribution>.libs/` under content-hashed names, and every ELF is
rewritten natively — DT_SONAME/DT_NEEDED renames, `.gnu.version_r` file
names, and `$ORIGIN`-relative DT_RUNPATH — by appending a relocated
`.dynamic`/`.dynstr` in a new PT_LOAD segment (no patchelf dependency).
The output is structurally equivalent to `auditwheel repair` (verified
side by side; we emit modern RUNPATH instead of its legacy RPATH).
Driver-style libraries the runtime provides stay external via
`--exclude libcuda.so.1`. `--verify-images` adds an install-layer check
across arbitrary container images (images without a matching interpreter
are skipped), and CI runs the wheel dimension per commit — including an
import check of every aarch64 wheel on a native arm64 runner.

## Pipeline

```
spec ──▶ sysroot        old-glibc baseline from distro RPMs + abilists
     ──▶ compiler       cross binutils + GCC from the RH gcc-toolset SRPM
     ──▶ compat-pack    RH-built libstdc++_nonshared.a + linker script
                        (object-level pruning fallback for non-RH sources)
     ──▶ pack           relocatable tar.zst + TOML manifest
         audit/verify   symbol-version gate + distro container matrix
```

Everything is also available as a Rust library (`--no-default-features` drops
the CLI): each stage is an independent API (`SysrootGenerator`,
`CompilerBuilder`, `CompatBuilder`, `Auditor`, `verify_in_containers`,
`pack_toolchain`), with the build environment injected via the `Runner` trait.
Baselines and package sources are TOML registries, extensible without code
changes.

## Design document

See `docs/crossforge-design.md` (Chinese) for the full design: baseline model,
the RH nonshared mechanism and its `-D_GLIBCXX_ASSERTIONS` build constraint,
artifact matrix, and milestone history.

## License

Licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or
[MIT license](LICENSE-MIT) at your option. Unless you explicitly state
otherwise, any contribution intentionally submitted for inclusion in this
work shall be dual licensed as above, without any additional terms or
conditions.

The toolchains this tool builds contain GCC, binutils, glibc and other
upstream components, each under its own license (GPL with the GCC Runtime
Library Exception for the runtime libraries, LGPL for glibc, etc.).
