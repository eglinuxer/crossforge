# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Network access is confined to fetching the two content-addressed upstream
# archives. Evidence, archive structure, payloads, license, ELF behavior, and
# consumer builds are all validated in later networkless stages.
FROM crossforge_host_runtime AS ninja-fetch
ARG NINJA_BINARY_URL
ARG NINJA_SOURCE_URL
ARG NINJA_SOURCE_COMPONENT_SHA256
WORKDIR /work
RUN test -n "$NINJA_BINARY_URL" \
    && test -n "$NINJA_SOURCE_URL" \
    && test -n "$NINJA_SOURCE_COMPONENT_SHA256" \
    && mkdir -p /work/input \
    && curl --fail --location --retry 3 "$NINJA_BINARY_URL" \
      --output /work/input/ninja-linux.zip \
    && curl --fail --location --retry 3 "$NINJA_SOURCE_URL" \
      --output /work/input/ninja-source.tar.gz

FROM ninja-fetch AS ninja-source
ARG NINJA_SOURCE_COMPONENT_SHA256
COPY config/generated/components/sources/ninja.json \
  /work/config/sources-ninja.json
COPY evidence/git/ninja-v1.13.2.commit.b64 \
  /work/evidence/git/ninja-v1.13.2.commit.b64
COPY evidence/github/ninja-v1.13.2.tag-ref.json.b64 \
  evidence/github/ninja-v1.13.2.release.json.b64 \
  /work/evidence/github/
COPY --chmod=0755 scripts/release_component.py \
  scripts/prepare-ninja-tool.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/prepare-ninja-tool.py \
      --component /work/config/sources-ninja.json \
      --component-sha256 "$NINJA_SOURCE_COMPONENT_SHA256" \
      --binary-archive /work/input/ninja-linux.zip \
      --source-archive /work/input/ninja-source.tar.gz \
      --repository /work \
      --output /out

FROM scratch AS ninja-source-export
COPY --from=ninja-source /out/ /

FROM crossforge_host_runtime AS ninja-host-tool
ARG NINJA_VERSION
ARG NINJA_SOURCE_COMPONENT_SHA256
ARG NINJA_POLICY_COMPONENT_SHA256
ARG NINJA_TOOL_COMPONENT_SHA256
COPY --from=crossforge_ninja_source / /work/prepared/
COPY config/generated/components/sources/ninja.json \
  /work/config/sources-ninja.json
COPY config/generated/components/implementation/ninja-host-tool.json \
  /work/config/ninja-host-tool-policy.json
COPY config/generated/components/host-tools/ninja.json \
  /work/config/ninja-host-tool.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/prepare-ninja-tool.py \
  scripts/install-qualify-ninja-tool.py /work/scripts/
ENV NINJA_ROOT=/opt/crossforge/host-tools/ninja/${NINJA_VERSION} \
    PATH=/opt/crossforge/host-tools/ninja/${NINJA_VERSION}/bin:${PATH}
RUN --network=none test "$NINJA_VERSION" = 1.13.2 \
    && /usr/libexec/platform-python \
      /work/scripts/install-qualify-ninja-tool.py \
      --prepared-root /work/prepared \
      --source-component /work/config/sources-ninja.json \
      --source-component-sha256 "$NINJA_SOURCE_COMPONENT_SHA256" \
      --policy-component /work/config/ninja-host-tool-policy.json \
      --policy-component-sha256 "$NINJA_POLICY_COMPONENT_SHA256" \
      --tool-component /work/config/ninja-host-tool.json \
      --tool-component-sha256 "$NINJA_TOOL_COMPONENT_SHA256" \
      --destination-root "$NINJA_ROOT" \
      --output /opt/crossforge/qualification/host-tools/ninja.json \
    && test "$(command -v ninja)" = "$NINJA_ROOT/bin/ninja" \
    && test "$(ninja --version)" = "$NINJA_VERSION" \
    && rm -rf /work
WORKDIR /workspace

FROM scratch AS ninja-host-tool-export
ARG NINJA_VERSION
COPY --from=ninja-host-tool \
  /opt/crossforge/host-tools/ninja/${NINJA_VERSION}/ \
  /opt/crossforge/host-tools/ninja/${NINJA_VERSION}/
COPY --from=ninja-host-tool \
  /opt/crossforge/qualification/host-tools/ninja.json \
  /opt/crossforge/qualification/host-tools/ninja.json
