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

FROM crossforge_host_runtime AS vcpkg-contract-assets-fetch
ARG VCPKG_CONTRACT_POLICY_COMPONENT_SHA256
ARG VCPKG_PATCHELF_URL
WORKDIR /work
RUN test -n "$VCPKG_CONTRACT_POLICY_COMPONENT_SHA256" \
    && test -n "$VCPKG_PATCHELF_URL" \
    && curl --fail --location --retry 3 "$VCPKG_PATCHELF_URL" \
      --output /work/patchelf-0.19.0-x86_64.tar.gz

FROM vcpkg-contract-assets-fetch AS vcpkg-contract-assets
ARG VCPKG_CONTRACT_POLICY_COMPONENT_SHA256
ARG VCPKG_PATCHELF_SHA256
ARG VCPKG_PATCHELF_SHA512
ARG VCPKG_PATCHELF_SIZE
COPY config/generated/components/implementation/vcpkg-contract-qualification.json \
  /work/config/vcpkg-contract-policy.json
COPY --chmod=0755 scripts/release_component.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/release_component.py validate \
      /work/config/vcpkg-contract-policy.json \
      --expected-component implementation/vcpkg-contract-qualification \
      --expected-scope qualification \
      --expected-sha256 "$VCPKG_CONTRACT_POLICY_COMPONENT_SHA256" \
    && printf '%s  %s\n' "$VCPKG_PATCHELF_SHA256" \
      /work/patchelf-0.19.0-x86_64.tar.gz | sha256sum -c - \
    && printf '%s  %s\n' "$VCPKG_PATCHELF_SHA512" \
      /work/patchelf-0.19.0-x86_64.tar.gz | sha512sum -c - \
    && test "$(stat -c %s /work/patchelf-0.19.0-x86_64.tar.gz)" \
      = "$VCPKG_PATCHELF_SIZE" \
    && install -D -m 0644 /work/patchelf-0.19.0-x86_64.tar.gz \
      /out/patchelf-0.19.0-x86_64.tar.gz

FROM scratch AS vcpkg-contract-assets-export
COPY --from=vcpkg-contract-assets /out/ /

FROM crossforge_host_runtime AS vcpkg-upstream-tier1-assets-fetch
ARG VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256
COPY config/generated/components/implementation/vcpkg-upstream-tier1-qualification.json \
  /work/config/vcpkg-upstream-tier1-policy.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-vcpkg-assets.py /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/fetch-vcpkg-assets.py fetch \
      --component /work/config/vcpkg-upstream-tier1-policy.json \
      --expected-component \
        implementation/vcpkg-upstream-tier1-qualification \
      --component-sha256 \
        "$VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256" \
      --output /work/assets

FROM vcpkg-upstream-tier1-assets-fetch AS vcpkg-upstream-tier1-assets
ARG VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/fetch-vcpkg-assets.py verify \
      --component /work/config/vcpkg-upstream-tier1-policy.json \
      --expected-component \
        implementation/vcpkg-upstream-tier1-qualification \
      --component-sha256 \
        "$VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256" \
      --output /work/assets

FROM scratch AS vcpkg-upstream-tier1-assets-export
COPY --from=vcpkg-upstream-tier1-assets /work/assets/ /

FROM crossforge_host_runtime AS vcpkg-upstream-tier2-assets-fetch
ARG VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256
COPY config/generated/components/implementation/vcpkg-upstream-tier2-qualification.json \
  /work/config/vcpkg-upstream-tier2-policy.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-vcpkg-assets.py /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/fetch-vcpkg-assets.py fetch \
      --component /work/config/vcpkg-upstream-tier2-policy.json \
      --expected-component \
        implementation/vcpkg-upstream-tier2-qualification \
      --component-sha256 \
        "$VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256" \
      --output /work/assets

FROM vcpkg-upstream-tier2-assets-fetch AS vcpkg-upstream-tier2-assets
ARG VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/fetch-vcpkg-assets.py verify \
      --component /work/config/vcpkg-upstream-tier2-policy.json \
      --expected-component \
        implementation/vcpkg-upstream-tier2-qualification \
      --component-sha256 \
        "$VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256" \
      --output /work/assets

FROM scratch AS vcpkg-upstream-tier2-assets-export
COPY --from=vcpkg-upstream-tier2-assets /work/assets/ /

FROM crossforge_host_runtime AS vcpkg-upstream-tier3-assets-fetch
ARG VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256
COPY config/generated/components/implementation/vcpkg-upstream-tier3-qualification.json \
  /work/config/vcpkg-upstream-tier3-policy.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-vcpkg-assets.py /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/fetch-vcpkg-assets.py fetch \
      --component /work/config/vcpkg-upstream-tier3-policy.json \
      --expected-component \
        implementation/vcpkg-upstream-tier3-qualification \
      --component-sha256 \
        "$VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256" \
      --output /work/assets

FROM vcpkg-upstream-tier3-assets-fetch AS vcpkg-upstream-tier3-assets
ARG VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/fetch-vcpkg-assets.py verify \
      --component /work/config/vcpkg-upstream-tier3-policy.json \
      --expected-component \
        implementation/vcpkg-upstream-tier3-qualification \
      --component-sha256 \
        "$VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256" \
      --output /work/assets

FROM scratch AS vcpkg-upstream-tier3-assets-export
COPY --from=vcpkg-upstream-tier3-assets /work/assets/ /

# Phase 13 integrates only the authenticated registry/tool and generated
# configuration. Port build trees and installed packages are qualification
# artifacts and never enter this user-facing base.
FROM crossforge_sdk_base AS vcpkg-sdk-base
ARG VCPKG_SOURCE_COMPONENT_SHA256
ARG VCPKG_INTEGRATION_COMPONENT_SHA256
ARG VCPKG_SDK_COMPONENT_SHA256
ARG NINJA_TOOL_COMPONENT_SHA256
ARG CMAKE_TOOL_COMPONENT_SHA256
COPY --from=crossforge_qemu_validated \
  /usr/local/libexec/crossforge/qemu-aarch64 \
  /usr/local/libexec/crossforge/qemu-aarch64
COPY config/release.json /opt/crossforge/release.json
COPY --from=crossforge_cmake_host_tool \
  /opt/crossforge/host-tools/cmake/ \
  /opt/crossforge/host-tools/cmake/
COPY --from=crossforge_cmake_host_tool \
  /opt/crossforge/qualification/host-tools/cmake.json \
  /opt/crossforge/qualification/host-tools/cmake.json
COPY --from=crossforge_ninja_host_tool \
  /opt/crossforge/host-tools/ninja/ \
  /opt/crossforge/host-tools/ninja/
COPY --from=crossforge_ninja_host_tool \
  /opt/crossforge/qualification/host-tools/ninja.json \
  /opt/crossforge/qualification/host-tools/ninja.json
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
COPY config/generated/components/host-tools/ninja.json \
  /work/config/ninja-host-tool.json
COPY config/generated/components/host-tools/cmake.json \
  /work/config/cmake-host-tool.json
COPY --chmod=0755 scripts/fetch-vcpkg-history.py \
  scripts/release_component.py scripts/qualify-vcpkg-sdk.py /work/scripts/
ENV NINJA_ROOT=/opt/crossforge/host-tools/ninja/1.13.2 \
    CROSSFORGE_CMAKE_ROOT=/opt/crossforge/host-tools/cmake/4.4.0 \
    VCPKG_ROOT=/opt/crossforge/vcpkg/root \
    VCPKG_OVERLAY_TRIPLETS=/opt/crossforge/vcpkg/triplets \
    VCPKG_DEFAULT_HOST_TRIPLET=crossforge-host-x64-el8 \
    VCPKG_DISABLE_METRICS=1 \
    VCPKG_FORCE_SYSTEM_BINARIES=1 \
    PATH=/opt/crossforge/host-tools/cmake/4.4.0/bin:/opt/crossforge/host-tools/ninja/1.13.2/bin:/opt/crossforge/vcpkg/root:${PATH}
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
      --ninja-component /work/config/ninja-host-tool.json \
      --ninja-component-sha256 "$NINJA_TOOL_COMPONENT_SHA256" \
      --ninja-report \
        /opt/crossforge/qualification/host-tools/ninja.json \
      --cmake-component /work/config/cmake-host-tool.json \
      --cmake-component-sha256 "$CMAKE_TOOL_COMPONENT_SHA256" \
      --cmake-report \
        /opt/crossforge/qualification/host-tools/cmake.json \
      --output /opt/crossforge/qualification/vcpkg/sdk.json \
    && test "$(command -v cmake)" = "$CROSSFORGE_CMAKE_ROOT/bin/cmake" \
    && test "$(cmake --version | head -n 1)" = "cmake version 4.4.0" \
    && test "$(command -v ninja)" = "$NINJA_ROOT/bin/ninja" \
    && test "$(ninja --version)" = 1.13.2 \
    && rm -rf /work \
    && test ! -e /opt/crossforge/vcpkg/root/downloads \
    && test ! -e /opt/crossforge/vcpkg/root/buildtrees \
    && test ! -e /opt/crossforge/vcpkg/root/packages \
    && test ! -e /opt/crossforge/vcpkg/root/installed \
    && test ! -e /opt/crossforge/vcpkg/root/vcpkg_installed
WORKDIR /workspace

# Exercise vcpkg's actual port pipeline without external source downloads.
# The host dependency builds and executes natively; the target library is
# built separately for every Crossforge triplet and never executes during its
# cross build.
FROM crossforge_vcpkg_sdk AS vcpkg-contract-qualified
ARG VCPKG_CONTRACT_POLICY_COMPONENT_SHA256
ARG VCPKG_CONTRACT_QUALIFICATION_COMPONENT_SHA256
COPY tests/vcpkg/contract/ /work/contract/
COPY --from=crossforge_vcpkg_contract_assets \
  /patchelf-0.19.0-x86_64.tar.gz /work/assets/patchelf-0.19.0-x86_64.tar.gz
COPY config/generated/components/implementation/vcpkg-contract-qualification.json \
  /work/config/vcpkg-contract-policy.json
COPY config/generated/components/vcpkg/contract-qualification.json \
  /work/config/vcpkg-contract-qualification.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/qualify-vcpkg-contract.py scripts/vcpkg_qualification.py \
  /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/qualify-vcpkg-contract.py \
      --release /opt/crossforge/release.json \
      --vcpkg-root /opt/crossforge/vcpkg/root \
      --fixture-root /work/contract \
      --patchelf-archive /work/assets/patchelf-0.19.0-x86_64.tar.gz \
      --qemu /usr/local/libexec/crossforge/qemu-aarch64 \
      --policy-component /work/config/vcpkg-contract-policy.json \
      --policy-component-sha256 \
        "$VCPKG_CONTRACT_POLICY_COMPONENT_SHA256" \
      --contract-component /work/config/vcpkg-contract-qualification.json \
      --contract-component-sha256 \
        "$VCPKG_CONTRACT_QUALIFICATION_COMPONENT_SHA256" \
      --output /opt/crossforge/qualification/vcpkg/contract.json \
    && rm -rf /work \
    && test ! -e /opt/crossforge/vcpkg/root/downloads \
    && test ! -e /opt/crossforge/vcpkg/root/buildtrees \
    && test ! -e /opt/crossforge/vcpkg/root/packages \
    && test ! -e /opt/crossforge/vcpkg/root/installed \
    && test ! -e /opt/crossforge/vcpkg/root/vcpkg_installed
WORKDIR /workspace

# Tier 1 exercises real curated-registry ports from an explicit three-asset
# closure. The source assets and patchelf helper are qualification inputs only.
FROM crossforge_vcpkg_contract AS vcpkg-upstream-tier1-qualified
ARG VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256
ARG VCPKG_UPSTREAM_TIER1_QUALIFICATION_COMPONENT_SHA256
COPY tests/vcpkg/upstream-tier1/ /work/upstream-tier1/
COPY --from=crossforge_vcpkg_upstream_tier1_assets \
  / /work/assets/
COPY --from=crossforge_vcpkg_contract_assets \
  /patchelf-0.19.0-x86_64.tar.gz /work/patchelf-0.19.0-x86_64.tar.gz
COPY config/generated/components/implementation/vcpkg-upstream-tier1-qualification.json \
  /work/config/vcpkg-upstream-tier1-policy.json
COPY config/generated/components/vcpkg/upstream-tier1-qualification.json \
  /work/config/vcpkg-upstream-tier1-qualification.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-vcpkg-assets.py scripts/vcpkg_qualification.py \
  scripts/qualify-vcpkg-upstream.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/qualify-vcpkg-upstream.py \
      --tier tier1 \
      --release /opt/crossforge/release.json \
      --vcpkg-root /opt/crossforge/vcpkg/root \
      --fixture-root /work/upstream-tier1 \
      --asset-root /work/assets \
      --patchelf-archive /work/patchelf-0.19.0-x86_64.tar.gz \
      --qemu /usr/local/libexec/crossforge/qemu-aarch64 \
      --policy-component /work/config/vcpkg-upstream-tier1-policy.json \
      --policy-component-sha256 \
        "$VCPKG_UPSTREAM_TIER1_POLICY_COMPONENT_SHA256" \
      --qualification-component \
        /work/config/vcpkg-upstream-tier1-qualification.json \
      --qualification-component-sha256 \
        "$VCPKG_UPSTREAM_TIER1_QUALIFICATION_COMPONENT_SHA256" \
      --output \
        /opt/crossforge/qualification/vcpkg/upstream-tier1.json \
    && rm -rf /work \
    && test ! -e /opt/crossforge/vcpkg/root/downloads \
    && test ! -e /opt/crossforge/vcpkg/root/buildtrees \
    && test ! -e /opt/crossforge/vcpkg/root/packages \
    && test ! -e /opt/crossforge/vcpkg/root/installed \
    && test ! -e /opt/crossforge/vcpkg/root/vcpkg_installed
WORKDIR /workspace

# Tier 2 adds the TLS/configure surface: curated curl with OpenSSL and zlib,
# using only the three explicitly bound source archives.
FROM crossforge_vcpkg_tier1 AS vcpkg-upstream-tier2-qualified
ARG VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256
ARG VCPKG_UPSTREAM_TIER2_QUALIFICATION_COMPONENT_SHA256
COPY tests/vcpkg/upstream-tier2/ /work/upstream-tier2/
COPY --from=crossforge_vcpkg_upstream_tier2_assets \
  / /work/assets/
COPY --from=crossforge_vcpkg_contract_assets \
  /patchelf-0.19.0-x86_64.tar.gz /work/patchelf-0.19.0-x86_64.tar.gz
COPY config/generated/components/implementation/vcpkg-upstream-tier2-qualification.json \
  /work/config/vcpkg-upstream-tier2-policy.json
COPY config/generated/components/vcpkg/upstream-tier2-qualification.json \
  /work/config/vcpkg-upstream-tier2-qualification.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-vcpkg-assets.py scripts/vcpkg_qualification.py \
  scripts/qualify-vcpkg-upstream.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/qualify-vcpkg-upstream.py \
      --tier tier2 \
      --release /opt/crossforge/release.json \
      --vcpkg-root /opt/crossforge/vcpkg/root \
      --fixture-root /work/upstream-tier2 \
      --asset-root /work/assets \
      --patchelf-archive /work/patchelf-0.19.0-x86_64.tar.gz \
      --qemu /usr/local/libexec/crossforge/qemu-aarch64 \
      --policy-component /work/config/vcpkg-upstream-tier2-policy.json \
      --policy-component-sha256 \
        "$VCPKG_UPSTREAM_TIER2_POLICY_COMPONENT_SHA256" \
      --qualification-component \
        /work/config/vcpkg-upstream-tier2-qualification.json \
      --qualification-component-sha256 \
        "$VCPKG_UPSTREAM_TIER2_QUALIFICATION_COMPONENT_SHA256" \
      --output \
        /opt/crossforge/qualification/vcpkg/upstream-tier2.json \
    && rm -rf /work \
    && test ! -e /opt/crossforge/vcpkg/root/downloads \
    && test ! -e /opt/crossforge/vcpkg/root/buildtrees \
    && test ! -e /opt/crossforge/vcpkg/root/packages \
    && test ! -e /opt/crossforge/vcpkg/root/installed \
    && test ! -e /opt/crossforge/vcpkg/root/vcpkg_installed
WORKDIR /workspace

# Tier 3 exercises a generated-code dependency and a representative compiled
# Boost library. protobuf must build and execute its host protoc while emitting
# libraries for each independently selected target triplet.
FROM crossforge_vcpkg_tier2 AS vcpkg-upstream-tier3-qualified
ARG VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256
ARG VCPKG_UPSTREAM_TIER3_QUALIFICATION_COMPONENT_SHA256
COPY tests/vcpkg/upstream-tier3/ /work/upstream-tier3/
COPY --from=crossforge_vcpkg_upstream_tier3_assets \
  / /work/assets/
COPY --from=crossforge_vcpkg_contract_assets \
  /patchelf-0.19.0-x86_64.tar.gz /work/patchelf-0.19.0-x86_64.tar.gz
COPY config/generated/components/implementation/vcpkg-upstream-tier3-qualification.json \
  /work/config/vcpkg-upstream-tier3-policy.json
COPY config/generated/components/vcpkg/upstream-tier3-qualification.json \
  /work/config/vcpkg-upstream-tier3-qualification.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-vcpkg-assets.py scripts/vcpkg_qualification.py \
  scripts/qualify-vcpkg-upstream.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/qualify-vcpkg-upstream.py \
      --tier tier3 \
      --release /opt/crossforge/release.json \
      --vcpkg-root /opt/crossforge/vcpkg/root \
      --fixture-root /work/upstream-tier3 \
      --asset-root /work/assets \
      --patchelf-archive /work/patchelf-0.19.0-x86_64.tar.gz \
      --qemu /usr/local/libexec/crossforge/qemu-aarch64 \
      --policy-component /work/config/vcpkg-upstream-tier3-policy.json \
      --policy-component-sha256 \
        "$VCPKG_UPSTREAM_TIER3_POLICY_COMPONENT_SHA256" \
      --qualification-component \
        /work/config/vcpkg-upstream-tier3-qualification.json \
      --qualification-component-sha256 \
        "$VCPKG_UPSTREAM_TIER3_QUALIFICATION_COMPONENT_SHA256" \
      --output \
        /opt/crossforge/qualification/vcpkg/upstream-tier3.json \
    && rm -rf /work \
    && test ! -e /opt/crossforge/vcpkg/root/downloads \
    && test ! -e /opt/crossforge/vcpkg/root/buildtrees \
    && test ! -e /opt/crossforge/vcpkg/root/packages \
    && test ! -e /opt/crossforge/vcpkg/root/installed \
    && test ! -e /opt/crossforge/vcpkg/root/vcpkg_installed
WORKDIR /workspace
