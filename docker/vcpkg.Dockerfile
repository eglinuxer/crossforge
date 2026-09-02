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
