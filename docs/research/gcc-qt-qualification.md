# GCC and Qt Qualification Research

This note records the source-backed decisions for the next qualification stages. The canonical product contract remains `docs/architecture.md`.

## GCC testsuite

GCC's official testing guide requires DejaGNU, Tcl, and Expect; supports `check-gcc`, `check-g++`, library-subdirectory checks, `RUNTESTFLAGS`, and `--target_board`; and emits `.sum` and `.log` evidence. GCC 15.2's test driver honors `GCC_UNDER_TEST`, which Crossforge must set to the final installed compiler rather than build-tree `xgcc`. [GCC testing guide](https://gcc.gnu.org/install/test.html), [GCC 15.2 `gcc.exp`](https://github.com/gcc-mirror/gcc/blob/releases/gcc-15.2.0/gcc/testsuite/lib/gcc.exp)

DejaGNU's unix board supports an `exec_shell`. Crossforge will use that supported hook for pinned QEMU with either the locked sysroot or clean Rocky runtime. The generic DejaGNU QEMU board is unsuitable because its aarch64 GNU branch forces static linking. [DejaGNU board manual](https://www.gnu.org/software/dejagnu/manual/Board-configuration-file.html), [official DejaGNU source](https://git.savannah.gnu.org/cgit/dejagnu.git/)

Required work: build libgomp; add a test-only DejaGNU/Expect RPM lock; preserve GCC source/object trees only in qualification stages; normalize exact test identities; and reject every unbaselined `FAIL`, `ERROR`, or `UNRESOLVED` result. Rocky 8.10 publishes `expect` in BaseOS and `dejagnu` in PowerTools, so the test-only lock must admit exactly that additional PowerTools package. [Rocky BaseOS packages](https://download.rockylinux.org/pub/rocky/8.10/BaseOS/x86_64/os/Packages/e/), [Rocky PowerTools packages](https://download.rockylinux.org/pub/rocky/8.10/PowerTools/x86_64/os/Packages/d/)

Phase 16 proves the installed-compiler path with 16 PASS records and no unexpected records in each of x86_64 host-direct, aarch64 locked-sysroot, and aarch64 clean-Rocky execution. The four-suite x86_64 full profile is now release-bound and independently reproduced; AArch64 full-suite baselining remains separate from the native candidate smoke gate.

GitHub's standard public-repository runner matrix now exposes the 4-core `ubuntu-24.04-arm` ARM64 label. Crossforge uses that host only as native execution infrastructure: the runtime under test remains the release-pinned Rocky Linux 8.10 arm64 child manifest, while probes come from the exact anonymously readable candidate digest. [GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

## Qt 6.8.4

The official archive identifies `qt-everywhere-opensource-src-6.8.4.tar.xz` as 994,798,840 bytes with SHA256 `1da37a32a583e7856d6fc13357c8ff6ad3ef7b877b8d276713b85026426d5246`. Its 108-byte SHA256 sidecar hashes to `f208721e3239cba3d21312295e7d991f378e83e79e51e55fe2ffb6c05726bb0a`. [Qt 6.8.4 source index](https://download.qt.io/archive/qt/6.8/6.8.4/single/)

Qt 6.8.4 requires a target toolchain/sysroot and same-version host Qt; configuration output must prove required features were not skipped. QtWebEngine additionally requires C++20, CMake 3.19+, Python with html5lib, Bison, Flex, GPerf, Node.js 14+, GCC 10+, pkg-config and reviewed Linux/XCB dependencies. [Qt 6.8.4 cross-build source](https://github.com/qt/qtdoc/blob/v6.8.4-lts-lgpl/doc/src/platforms/configure-linux-device.qdoc), [QtWebEngine 6.8.4 requirements](https://github.com/qt/qtwebengine/blob/v6.8.4-lts-lgpl/src/core/doc/src/qtwebengine-platform-notes.qdoc)

Qt therefore follows GCC as a separate test-only supply-chain slice: lock source/checksum, host tools, WebEngine tools, and dual-architecture dependency overlays before downloading or building the 949 MiB archive. Qt build outputs never enter the SDK.
