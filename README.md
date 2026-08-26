# crossforge

Build once, run on old Linux. crossforge is an open-source tool that **builds
cross-compilation toolchains** solving the classic glibc / libstdc++
compatibility problem: binaries compiled with a modern GCC that run unmodified
on distributions as old as CentOS 7.

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
toolchains run on stock CentOS 7, AlmaLinux/Rocky 8, Ubuntu 20.04 and
Debian 11.

## Use a prebuilt toolchain (Docker)

```console
$ docker pull ghcr.io/eglinuxer/crossforge/toolchain:el8-x86_64
$ docker run --rm -v "$PWD:/src" -w /src \
    ghcr.io/eglinuxer/crossforge/toolchain:el8-x86_64 \
    x86_64-unknown-linux-gnu-g++ -std=c++20 hello.cpp -o hello
```

Every commit to `main` publishes all supported combinations
(gcc 14.2.1 + 11.2.1 × el7/el8 × x86_64/aarch64) via the `toolchain-images`
workflow. Tags: `<baseline>-<target>` (default gcc),
`<baseline>-<target>-gcc<version>`, and a `-<sha>` suffix for pinning exact
builds. el8 = glibc 2.28 / GLIBCXX 3.4.25 baseline; el7 = glibc 2.17 /
GLIBCXX 3.4.19 with the old `std::string` ABI forced.

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

The result is a fully relocatable prefix — tar it up, unpack anywhere, no
configuration needed. `--pack` produces a distributable `tar.zst` + TOML
manifest. Building inside the el8 container keeps the toolchain's own host
requirement at glibc ≥ 2.28. See `docker/buildenv.Dockerfile` for the build
environment's package rationale (notably `glibc-gconv-extra`: without the full
gconv module set, GCC's configure probe disables iconv and the built cc1
silently loses `-fexec-charset` support).

Available compilers (`--gcc`): `14.2.1` (default, RH gcc-toolset-14) and
`11.2.1` (RH gcc-toolset-11 — its compat patches also provide the RH-tuned
nonshared48 for the el7 baseline). More versions are a TOML registry entry
away (`src/registry/toolchain-sources.toml`).

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
