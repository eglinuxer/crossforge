# Qt 6 cross build

The acceptance test for the cross build environment: what it means for the
host companion to exist at all.

Qt cannot be cross-compiled in one pass. Its build runs tools it must build
first — `moc`, `rcc`, `uic`, `syncqt`, `qmltyperegistrar` — and those run on
the *build* machine, so they need a native compiler, while the libraries they
generate code for need the cross one. That is the whole argument for shipping
both toolchains in one environment, and this sample is the proof it works.

```
stage 1   host Qt      native x86_64      moc / rcc / uic / qt-cmake
stage 2   target Qt    cross aarch64      -DQT_HOST_PATH=<stage 1>
stage 3   application  cross aarch64      built with target Qt, run under qemu
```

## Running it

```console
$ ./examples/qt6-cross/build.sh
```

It pulls `crossenv:el8-aarch64-qt6`, the published environment on the `qt6`
sysroot profile. The unmarked `el8-aarch64` tag is the `minimal` profile and
will not get through Qt's configure: Qt probes for freetype, fontconfig,
X11, wayland, xkbcommon and EGL, which minimal does not carry. The deeper
sysroot is what the `-qt6` suffix buys, for about 230MB.

To build the environment yourself instead — a different baseline, a newer
Qt's dependencies — `crossforge build --sysroot-profile qt6 --target
aarch64` produces the toolchain (a clone of the base one with the sysroot
swapped, so it costs a copy rather than another GCC build), and
`docker/crossenv.Dockerfile` composes it with the x86_64 companion. Then set
`IMAGE`.

`IMAGE`, `WORK`, `TARGET_ID`, `QT_VERSION` and `JOBS` are all overridable.

Only qtbase is built. Further modules repeat stages 1 and 2 unchanged, via
`qt-configure-module` against the two prefixes this leaves behind.

Each stage records a marker and is skipped when it is already satisfied, so
a failure part way through resumes rather than rebuilding Qt.

## What it actually proves

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
