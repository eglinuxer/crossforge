# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# vcpkg's registry versioning reads historical Git trees, so this stage keeps
# the complete history reachable from the immutable release tag. It is not a
# shallow source-archive substitute.
FROM crossforge_host_runtime AS vcpkg-fetch
ARG VCPKG_REPOSITORY
ARG VCPKG_RELEASE_TAG
ARG VCPKG_RELEASE_COMMIT
ARG VCPKG_TOOL_URL
ARG VCPKG_TOOL_SHA256
ARG VCPKG_TOOL_SIGNATURE_URL
WORKDIR /work
COPY --chmod=0755 scripts/fetch-vcpkg-history.py /work/scripts/
RUN test -n "$VCPKG_RELEASE_COMMIT" \
    && test -n "$VCPKG_TOOL_SHA256" \
    && GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
      GIT_TERMINAL_PROMPT=0 \
      git clone --no-tags --branch "$VCPKG_RELEASE_TAG" \
        --single-branch --no-checkout \
        "$VCPKG_REPOSITORY" /work/repository \
    && /usr/libexec/platform-python /work/scripts/fetch-vcpkg-history.py \
      --repository /work/repository \
      --tag "$VCPKG_RELEASE_TAG" \
      --commit "$VCPKG_RELEASE_COMMIT" \
    && curl --fail --location --retry 3 "$VCPKG_TOOL_URL" \
      --output /work/vcpkg-tool \
    && curl --fail --location --retry 3 "$VCPKG_TOOL_SIGNATURE_URL" \
      --output /work/vcpkg-tool.sig

FROM vcpkg-fetch AS vcpkg-source
ARG VCPKG_SOURCE_COMPONENT_SHA256
COPY config/generated/components/sources/vcpkg.json \
  /work/config/sources-vcpkg.json
COPY evidence/git/vcpkg-2026.07.29.tag.b64 \
  evidence/git/vcpkg-2026.07.29.commit.b64 \
  evidence/git/vcpkg-tool-2026-07-27.commit.b64 \
  /work/input/evidence/git/
COPY evidence/gpg/vcpkg-glibc-2026-07-27.sig.b64 \
  /work/input/evidence/gpg/vcpkg-glibc-2026-07-27.sig.b64
COPY keys/MICROSOFT-RELEASE-KEY.asc \
  /work/input/keys/MICROSOFT-RELEASE-KEY.asc
COPY licenses/vcpkg-tool/ /work/input/licenses/vcpkg-tool/
COPY --chmod=0755 scripts/release_component.py \
  scripts/prepare-vcpkg-source.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/prepare-vcpkg-source.py \
      --component /work/config/sources-vcpkg.json \
      --component-sha256 "$VCPKG_SOURCE_COMPONENT_SHA256" \
      --repository /work/repository \
      --tool /work/vcpkg-tool \
      --signature /work/vcpkg-tool.sig \
      --input-root /work/input \
      --output /out/root \
      --manifest /out/source.json

FROM scratch AS vcpkg-source-export
COPY --from=vcpkg-source /out/ /

# Phase 13 integrates only the authenticated registry/tool and generated
# configuration. Port build trees and installed packages are qualification
# artifacts and never enter this user-facing base.
FROM crossforge_sdk_base AS vcpkg-sdk-base
ARG VCPKG_SOURCE_COMPONENT_SHA256
ARG VCPKG_INTEGRATION_COMPONENT_SHA256
ARG VCPKG_SDK_COMPONENT_SHA256
COPY --from=crossforge_vcpkg_source /root/ /opt/crossforge/vcpkg/root/
COPY --from=crossforge_vcpkg_source /source.json \
  /opt/crossforge/qualification/vcpkg/source.json
COPY integration/cmake/ /opt/crossforge/cmake/
COPY integration/vcpkg/triplets/ /opt/crossforge/vcpkg/triplets/
COPY integration/vcpkg/manifest.json \
  /opt/crossforge/vcpkg/integration.json
COPY config/generated/components/sources/vcpkg.json \
  /work/config/sources-vcpkg.json
COPY config/generated/components/implementation/vcpkg-integration.json \
  /work/config/vcpkg-integration.json
COPY config/generated/components/vcpkg/sdk-build.json \
  /work/config/vcpkg-sdk-build.json
COPY --chmod=0755 scripts/fetch-vcpkg-history.py \
  scripts/release_component.py scripts/qualify-vcpkg-sdk.py /work/scripts/
ENV VCPKG_ROOT=/opt/crossforge/vcpkg/root \
    VCPKG_OVERLAY_TRIPLETS=/opt/crossforge/vcpkg/triplets \
    VCPKG_DEFAULT_HOST_TRIPLET=crossforge-host-x64-el8 \
    VCPKG_DISABLE_METRICS=1 \
    VCPKG_FORCE_SYSTEM_BINARIES=1 \
    PATH=/opt/crossforge/vcpkg/root:${PATH}
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/qualify-vcpkg-sdk.py \
      --release /opt/crossforge/release.json \
      --root /opt/crossforge/vcpkg/root \
      --source-manifest /opt/crossforge/qualification/vcpkg/source.json \
      --integration-manifest /opt/crossforge/vcpkg/integration.json \
      --cmake-root /opt/crossforge/cmake \
      --triplet-root /opt/crossforge/vcpkg/triplets \
      --qemu /usr/local/libexec/crossforge/qemu-aarch64 \
      --source-component /work/config/sources-vcpkg.json \
      --source-component-sha256 "$VCPKG_SOURCE_COMPONENT_SHA256" \
      --integration-component /work/config/vcpkg-integration.json \
      --integration-component-sha256 \
        "$VCPKG_INTEGRATION_COMPONENT_SHA256" \
      --sdk-component /work/config/vcpkg-sdk-build.json \
      --sdk-component-sha256 "$VCPKG_SDK_COMPONENT_SHA256" \
      --output /opt/crossforge/qualification/vcpkg/sdk.json \
    && rm -rf /work \
    && test ! -e /opt/crossforge/vcpkg/root/downloads \
    && test ! -e /opt/crossforge/vcpkg/root/buildtrees \
    && test ! -e /opt/crossforge/vcpkg/root/packages \
    && test ! -e /opt/crossforge/vcpkg/root/installed \
    && test ! -e /opt/crossforge/vcpkg/root/vcpkg_installed
WORKDIR /workspace
