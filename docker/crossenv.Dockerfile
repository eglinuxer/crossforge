# A complete cross build environment: the target cross toolchain plus the
# x86_64 native companion, in one image.
#
# Why both: a real cross build runs host tools it must build first — Qt's
# moc/rcc/uic/qmlcachegen, protoc, flatc, bindgen. Those run on the build
# machine, so they need a native compiler; the target artifacts need the
# cross one. Composing the pair here (instead of leaving it to every
# downstream) also means one digest to pin.
#
# Both toolchains come from already-published images, so the bundle's inputs
# are digest-addressable:
#
#   docker build -f docker/crossenv.Dockerfile \
#     --build-arg HOST_IMAGE=ghcr.io/eglinuxer/crossforge/toolchain:el8-x86_64@sha256:... \
#     --build-arg TARGET_IMAGE=ghcr.io/eglinuxer/crossforge/toolchain:el8-aarch64@sha256:... \
#     --build-arg HOST_ID=gcc14.2.1-el8-x86_64 \
#     --build-arg TARGET_ID=gcc14.2.1-el8-aarch64 \
#     -t ghcr.io/eglinuxer/crossforge/crossenv:el8-aarch64 .
#
# Usage:
#   docker run --rm -v "$PWD:/src" -w /src <image> sh -c '
#     gcc --version                                   # native: host tools
#     aarch64-unknown-linux-gnu-gcc --version         # cross:  target
#     cmake -DCMAKE_TOOLCHAIN_FILE=$CROSSFORGE_TARGET_ROOT/toolchain.cmake ...'
ARG HOST_IMAGE
ARG TARGET_IMAGE

FROM ${HOST_IMAGE} AS host
FROM ${TARGET_IMAGE} AS target

FROM quay.io/rockylinux/rockylinux:8

ARG HOST_ID
ARG TARGET_ID
ARG BASELINE
ARG SYSROOT_PROFILE

# Host-side build tooling. These run on the build machine, so they are
# ordinary x86_64 packages — target dependencies live in the sysroot instead.
RUN dnf install -y \
        make cmake perl python3 pkgconf-pkg-config git patch which findutils \
        tar xz bzip2 gzip diffutils file bison flex \
    && dnf install -y --enablerepo=powertools ninja-build gperf \
    && dnf clean all

# Distinct subdirectories, so the two copies merge rather than collide.
COPY --from=host /opt/crossforge /opt/crossforge
COPY --from=target /opt/crossforge /opt/crossforge

# Host first: bare `gcc`/`g++`/`cc` must resolve to the native companion
# (the distro's own gcc is 8.5 and is not what this environment qualifies).
# The cross drivers are triple-prefixed, so nothing collides.
ENV CROSSFORGE_HOST_ROOT=/opt/crossforge/${HOST_ID} \
    CROSSFORGE_TARGET_ROOT=/opt/crossforge/${TARGET_ID} \
    PATH=/opt/crossforge/${HOST_ID}/bin:/opt/crossforge/${TARGET_ID}/bin:${PATH}

LABEL org.opencontainers.image.title="crossforge cross build environment" \
      org.crossforge.host-toolchain="${HOST_ID}" \
      org.crossforge.target-toolchain="${TARGET_ID}" \
      org.crossforge.baseline="${BASELINE}" \
      org.crossforge.sysroot-profile="${SYSROOT_PROFILE}"
