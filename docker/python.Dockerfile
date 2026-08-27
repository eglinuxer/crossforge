# Distributable python pack image (design doc §9). Build context must be the
# directory holding the packs (work/python-packs/).
#
#   docker build -f docker/python.Dockerfile \
#     --build-arg PACK_ID=cp312-x86_64 --build-arg PYTHON_VERSION=3.12.14 \
#     -t ghcr.io/eglinuxer/crossforge/python:cp312-x86_64 work/python-packs
#
# The pack is installed at the prefix it was configured for, so the image is
# a directly usable CPython on the matching architecture:
#
#   docker run --rm ghcr.io/eglinuxer/crossforge/python:cp312-x86_64 \
#     python3.12 -c 'import ssl; print(ssl.OPENSSL_VERSION)'
#
# `crossforge python --pull` extracts /opt/_internal back into a local pack
# tree, so wheel builds need no CPython build of their own.
#
# COPY-only by design: no RUN steps, so the aarch64 variant can be built on
# an x86_64 host without emulation.
FROM quay.io/rockylinux/rockylinux:8

ARG PACK_ID
ARG PYTHON_VERSION

COPY ${PACK_ID}/opt/_internal /opt/_internal

ENV PATH=/opt/_internal/cpython-${PYTHON_VERSION}/bin:${PATH}
