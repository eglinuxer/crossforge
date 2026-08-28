# Qt 6 cross build

The acceptance test for the cross build environment: what it means for the
host companion to exist at all.

Qt cannot be cross-compiled in one pass. Its build runs tools it must build
first — `moc`, `rcc`, `uic`, `syncqt`, `qmltyperegistrar` — and those run on
the *build* machine, so they need a native compiler, while the libraries they
generate code for need the cross one. That is the whole argument for shipping
both toolchains in one environment, and this sample is the proof it works.

```
stage 0   environment  from the image     published toolchain + qt6 sysroot
stage 1   host Qt      native x86_64      moc / rcc / uic / qt-cmake
stage 2   target Qt    cross aarch64      -DQT_HOST_PATH=<stage 1>
stage 3   application  cross aarch64      built with target Qt, run under qemu
```

## Running it

```console
$ ./examples/qt6-cross/build.sh
```

Everything it needs it fetches: `crossenv:el8-aarch64` for the toolchain and
the host build tools, and the Qt sources. Nothing is compiled that the image
already ships.

The one thing it assembles is the sysroot. Qt's configure probes for
freetype, fontconfig, X11, wayland, xkbcommon and EGL, which the published
image's `minimal` profile does not carry, so stage 0 generates the `qt6`
profile from `sysroot-locks/` and swaps it into the extracted prefix. That
depth belongs to this test rather than to the product — shipping it would
add ~230MB to an image for a dependency set only Qt-shaped builds want — and
the swap is the same operation `crossforge build --sysroot-profile` performs.
It needs no overrides: `toolchain.cmake`, pkg-config and GCC's built-in
sysroot all resolve `<prefix>/<triple>/sysroot` relative to the prefix, so
they follow it.

An image already present locally is used as-is, so `IMAGE=` can point at one
you composed yourself. `WORK`, `TARGET_ID`, `QT_VERSION`, `BASELINE`, `ARCH`
and `JOBS` are overridable too.

Only qtbase is built. Further modules repeat stages 1 and 2 unchanged, via
`qt-configure-module` against the two prefixes this leaves behind.

Each stage records a marker and is skipped when it is already satisfied, so
a failure part way through resumes rather than rebuilding Qt.

## What it actually proves

- The compiler it exercises is the published one, taken out of the image
  and used unchanged, so a pass says something about what downstreams get
  rather than about a toolchain rebuilt for the occasion.
- The native companion builds and *runs* host tools during a cross build.
- `toolchain.cmake` drives a real CMake project, not a hello world:
  `CMAKE_SYSROOT` and the pkg-config variables are what let Qt's configure
  find its target dependencies instead of the host's.
- The qt6 sysroot profile is deep enough for Qt's dependency probing.
- The result runs on the target: the sample executes under qemu against the
  baseline sysroot, and reports the Qt version it linked against.
- `crossforge audit` accepts the result — every Qt library and the
  application stay inside the el8 baseline, which is the property the
  nonshared design exists to provide.

It earned its place the first time it ran, by failing: the host `rcc` died
on `libgcc_s.so.1: version GCC_12.0.0 not found`. Qt's `qfloat16` reaches
conversion helpers that GCC 14's libgcc_s versions above anything el8
provides, and `-shared-libgcc` — the g++ default — left nothing but the
shared object to resolve them. A hello world cannot find that; a real
project can.
