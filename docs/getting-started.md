# Getting started with Crossforge

Crossforge is delivered as one `linux/amd64` container image. The container is
the tool host; it produces EL8-compatible x86_64 or AArch64 binaries and does
not execute target programs during cross compilation.

The commands below use the stable channel. They will become usable after the
first public release; until then, the repository README explicitly reports the
project as unpublished.

## Inspect the SDK

```console
$ docker pull ghcr.io/eglinuxer/crossforge:gts15-el8
$ docker run --rm ghcr.io/eglinuxer/crossforge:gts15-el8 \
    crossforge info
$ docker run --rm ghcr.io/eglinuxer/crossforge:gts15-el8 \
    crossforge env --target aarch64 --python 3.14 --vcpkg --json
```

For a reproducible automation input, replace the channel tag with the digest
recorded in the corresponding GitHub Release `candidate.json`.

## Build a CMake project

Run the container as your host UID/GID so bind-mounted outputs remain owned by
you. Crossforge supplies CMake 4.4.0, Ninja 1.13.2, the selected compiler,
sysroot and toolchain file through `crossforge run`:

```console
$ mkdir -p .crossforge-cache
$ docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$PWD:/workspace" \
    --volume "$PWD/.crossforge-cache:/cache" \
    --env CROSSFORGE_CACHE_ROOT=/cache \
    --workdir /workspace \
    ghcr.io/eglinuxer/crossforge:gts15-el8 \
    crossforge run --target aarch64 -- \
    cmake -S . -B build/aarch64 -G Ninja -DCMAKE_BUILD_TYPE=Release
$ docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$PWD:/workspace" \
    --volume "$PWD/.crossforge-cache:/cache" \
    --env CROSSFORGE_CACHE_ROOT=/cache \
    --workdir /workspace \
    ghcr.io/eglinuxer/crossforge:gts15-el8 \
    crossforge run --target aarch64 -- \
    cmake --build build/aarch64 --verbose
```

Use `--target x86_64` for the EL8 x86_64 target. The generated binary is a
target artifact, not a container-host program. Test it on the intended EL8
runtime; do not depend on accidental host execution or implicit binfmt/QEMU.

Meson consumers receive `MESON_CROSS_FILE`. Projects that invoke Make or
Autotools can inspect the exact `CC`, `CXX`, binutils, sysroot and pkg-config
selection with `crossforge env --target <target>`.

## Select Python and vcpkg variants

`--python` requires a target and accepts `3.9` through `3.14`. It selects the
matching native build Python and target sysconfig without placing target code
on the host import path:

```console
$ docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$PWD:/workspace" \
    --workdir /workspace \
    ghcr.io/eglinuxer/crossforge:gts15-el8 \
    crossforge run --target x86_64 --python 3.13 -- \
    cmake -S . -B build/python-x86_64 -G Ninja
```

Enable vcpkg explicitly with `--vcpkg`. Static linkage is the default;
`--linkage dynamic` selects the separately qualified dynamic triplet. vcpkg
downloads and binary caches go to `CROSSFORGE_CACHE_ROOT`, never into the
root-owned SDK:

```console
$ docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$PWD:/workspace" \
    --volume "$PWD/.crossforge-cache:/cache" \
    --env CROSSFORGE_CACHE_ROOT=/cache \
    --workdir /workspace \
    ghcr.io/eglinuxer/crossforge:gts15-el8 \
    crossforge run --target aarch64 --vcpkg --linkage dynamic -- \
    vcpkg install
```

The curated qualification tiers are evidence for the shipped integration, not
a promise that every upstream vcpkg port supports every Crossforge triplet.

## Retrieve source and release evidence

The SDK contains a digest-bound source locator and human-readable offer:

```console
$ docker run --rm ghcr.io/eglinuxer/crossforge:gts15-el8 \
    cat /opt/crossforge/SOURCES.json
$ docker run --rm ghcr.io/eglinuxer/crossforge:gts15-el8 \
    cat /opt/crossforge/SOURCE-OFFER
```

Each immutable GitHub Release attaches `candidate.json`,
`release-promotion.json`, a deterministic release-evidence tar and its SHA256
sidecar. After downloading both archive files, verify them before extraction:

```console
$ sha256sum -c crossforge-v0.1.0-release-evidence.tar.sha256
$ tar -tf crossforge-v0.1.0-release-evidence.tar
```

From a checkout of the matching release tag, the semantic verifier reruns the
candidate, source, native AArch64, Qt and Sigstore evidence checks:

```console
$ ./scripts/release_evidence.py validate \
    crossforge-v0.1.0-release-evidence.tar \
    --sha256 crossforge-v0.1.0-release-evidence.tar.sha256
```

## Writable paths and troubleshooting

The public image runs as UID/GID 1000 by default. `/opt/crossforge` is
root-owned and immutable. Only your mounted workspace, selected cache, home and
`/tmp` should be writable. When an arbitrary `--user` cannot write the image's
default home, the launcher creates a UID-specific fallback below `/tmp`.

Start diagnosis with `crossforge info --json` and `crossforge env ... --json`.
They expose only Crossforge-managed variables and do not echo inherited tokens
or credentials. Common failures are:

- a bind mount not writable by the selected container UID;
- selecting `--python` without `--target`;
- selecting `--linkage dynamic` without `--vcpkg`;
- trying to execute a target binary in the amd64 tool-host container;
- relying on host `PKG_CONFIG_PATH` or `PYTHONPATH`, which Crossforge clears at
  target/Python boundaries intentionally.

Use public GitHub issues for reproducible, non-sensitive defects. Do not place
credentials, private source or undisclosed vulnerability details in an issue.
