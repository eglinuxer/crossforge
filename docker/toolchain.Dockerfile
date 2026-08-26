# Ready-to-use crossforge toolchain image. Build context must be the
# directory containing the toolchain prefix (work/toolchains/).
#
#   docker build -f docker/toolchain.Dockerfile \
#     --build-arg TOOLCHAIN_ID=gcc14.2.1-el8-x86_64 \
#     -t crossforge-toolchain:el8-x86_64 work/toolchains
#
# Usage:
#   docker run --rm -v "$PWD:/src" -w /src ghcr.io/eglinuxer/crossforge/toolchain:el8-x86_64 \
#     x86_64-unknown-linux-gnu-g++ -std=c++20 hello.cpp -o hello
FROM quay.io/rockylinux/rockylinux:8

ARG TOOLCHAIN_ID
RUN test -n "$TOOLCHAIN_ID"

RUN dnf install -y make && dnf clean all

COPY ${TOOLCHAIN_ID} /opt/crossforge/${TOOLCHAIN_ID}
ENV PATH=/opt/crossforge/${TOOLCHAIN_ID}/bin:${PATH}
