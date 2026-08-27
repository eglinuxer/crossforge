# crossforge build environment: el8 base so produced toolchains only require
# glibc >= 2.28 on the host, plus everything GCC's build needs.
# - flex/bison: RH snapshot tarballs ship without pre-generated scanner sources
# - findutils: Rocky images ship without `find`; libtool silently collects an
#   empty object list when merging convenience archives (libsframe never makes
#   it into libbfd.a and gas/ld fail with undefined sframe_* references)
# - glibc-gconv-extra: without the full gconv module set, GCC's working-iconv
#   configure probe fails and cc1 loses -fexec-charset support entirely
# - dejagnu (PowerTools): `crossforge check` (GCC upstream testsuite)
# - pkgconf-pkg-config: CPython >= 3.11 configure detects target libraries
#   (openssl, sqlite, ...) via pkg-config against the toolchain sysroot
# - which: CPython <= 3.10 cross configure probes build-python candidates
#   with `which ... || continue`; with no `which` binary every candidate is
#   skipped and the loop's leftover variable silently selects bare `python`
# - cmake + ninja-build (PowerTools): CMake-backend wheels
#   (scikit-build-core / nanobind)
# - expat/gmp/mpfr-devel: cross gdb needs all three
# - *-devel: native (x86_64) CPython builds probe the build host's system
#   dirs (not the sysroot) for optional stdlib modules; the set matches the
#   sysroot package list exactly (same el8 NVRs) so native and cross packs
#   get a symmetric stdlib
# The quay.io image is Rocky's continuously updated one; the docker.io
# `rockylinux:8` tag is a stale 8.9 snapshot.
# Parent images are pinned by digest: a tag is a moving target, and a
# toolchain's whole claim is that its inputs are known. Refresh with
# `docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'`.
FROM quay.io/rockylinux/rockylinux:8@sha256:e8a49c5403b687db05d4d67333fa45808fbe74f36e683cec7abb1f7d0f2338c6

RUN dnf install -y \
        gcc gcc-c++ make tar xz bzip2 file diffutils patch flex bison \
        findutils \
        glibc-gconv-extra \
        pkgconf-pkg-config \
        which \
        cmake \
        expat-devel gmp-devel mpfr-devel \
        zlib-devel bzip2-devel xz-devel libffi-devel openssl-devel \
        sqlite-devel libuuid-devel \
    && dnf install -y --enablerepo=powertools dejagnu ninja-build \
    && dnf clean all

# Static user-mode qemu for running aarch64 testsuites (EPEL8 has no
# qemu-user-static; extracted from the multiarch/qemu-user-static image).
COPY --from=multiarch/qemu-user-static@sha256:fe60359c92e86a43cc87b3d906006245f77bfc0565676b80004cc666e4feb9f0 /usr/bin/qemu-aarch64-static /usr/bin/qemu-aarch64
