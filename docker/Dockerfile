# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Canonical Docker-first build graph for the rewrite.
#
# Phase 1 deliberately stops at configuration validation and filesystem layout.
# No stage in this file claims to contain a usable compiler yet. The legacy
# Dockerfiles remain beside this file until the vertical toolchain slice has
# passed its acceptance tests.

# The generated Bake override binds this named context to the digest in
# config/release.json. A direct Docker build has no implicit fallback image.
FROM crossforge_rocky AS config-validate
WORKDIR /src
COPY config/ ./config/
COPY scripts/validate-release.py ./scripts/validate-release.py
RUN /usr/libexec/platform-python ./scripts/validate-release.py \
      ./config/release.json \
      --schema ./config/schemas/release.schema.json

# Matrix-shaped placeholder used only to verify that Bake expands the two
# compiler targets correctly. It writes an explicit NOT-BUILT marker instead of
# producing fake toolchain files.
FROM config-validate AS toolchain-plan
ARG CROSSFORGE_TARGET_ARCH
RUN case "${CROSSFORGE_TARGET_ARCH}" in \
      x86_64|aarch64) ;; \
      *) echo "unsupported compiler target: ${CROSSFORGE_TARGET_ARCH}" >&2; exit 2 ;; \
    esac \
    && mkdir -p "/plans/${CROSSFORGE_TARGET_ARCH}" \
    && printf '%s\n' 'NOT BUILT - phase 1 planning stage' \
         > "/plans/${CROSSFORGE_TARGET_ARCH}/README"

# Inspectable layout prototype. This target is cache/local-output only in the
# Bake file and must never be published under a user-facing SDK tag.
FROM config-validate AS sdk-skeleton
RUN mkdir -p \
      /opt/crossforge/targets/x86_64-unknown-linux-gnu \
      /opt/crossforge/targets/aarch64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/x86_64 \
      /opt/crossforge/sysroots/el8/aarch64 \
      /opt/crossforge/python \
      /opt/crossforge/cmake \
      /opt/crossforge/meson \
      /opt/crossforge/vcpkg/triplets \
      /opt/crossforge/env \
    && printf '%s\n' 'NOT BUILT - phase 1 SDK layout only' \
         > /opt/crossforge/README.phase1
COPY config/release.json /opt/crossforge/release.json
