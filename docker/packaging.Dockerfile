# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

FROM crossforge_host_runtime AS nfpm-downloads
ARG NFPM_BINARY_URL
ARG NFPM_SOURCE_URL
ARG NFPM_CHECKSUMS_URL
ARG NFPM_SIGSTORE_URL
ARG NFPM_SOURCE_COMPONENT_SHA256
WORKDIR /work
RUN test -n "$NFPM_SOURCE_COMPONENT_SHA256" \
    && curl --fail --location --retry 3 "$NFPM_BINARY_URL" \
      --output /work/nfpm-binary.tar.gz \
    && curl --fail --location --retry 3 "$NFPM_SOURCE_URL" \
      --output /work/nfpm-source.tar.gz \
    && curl --fail --location --retry 3 "$NFPM_CHECKSUMS_URL" \
      --output /work/checksums.txt \
    && curl --fail --location --retry 3 "$NFPM_SIGSTORE_URL" \
      --output /work/checksums.txt.sigstore.json

FROM nfpm-downloads AS nfpm-tool
ARG NFPM_SOURCE_COMPONENT_SHA256
COPY config/generated/components/sources/nfpm.json \
  /work/config/sources-nfpm.json
COPY licenses/nfpm/LICENSE.md /work/input/LICENSE.md
COPY --chmod=0755 scripts/release_component.py \
  scripts/prepare-nfpm-tool.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/prepare-nfpm-tool.py \
      --component /work/config/sources-nfpm.json \
      --component-sha256 "$NFPM_SOURCE_COMPONENT_SHA256" \
      --binary-archive /work/nfpm-binary.tar.gz \
      --source-archive /work/nfpm-source.tar.gz \
      --checksums /work/checksums.txt \
      --sigstore /work/checksums.txt.sigstore.json \
      --license /work/input/LICENSE.md \
      --output-root /out

FROM scratch AS nfpm-tool-export
COPY --from=nfpm-tool /out/ /

FROM crossforge_sdk_base AS packaging-sdk
ARG NFPM_VERSION
ARG NFPM_BINARY_SHA256
ARG NFPM_SOURCE_COMPONENT_SHA256
ARG CROSSPACK_IMPLEMENTATION_COMPONENT_SHA256
ARG CROSSFORGE_LAUNCHER_COMPONENT_SHA256
ARG CROSSPACK_SDK_COMPONENT_SHA256
COPY --from=crossforge_nfpm_tool /root/ /
COPY --from=crossforge_nfpm_tool /source.json \
  /opt/crossforge/qualification/packaging/nfpm-source.json
COPY config/schemas/crosspack.schema.json \
  config/schemas/crosspack-plan.schema.json \
  config/schemas/crosspack-result.schema.json \
  config/schemas/crosspack-staging.schema.json \
  /opt/crossforge/schemas/
COPY integration/meson/ /opt/crossforge/meson/
COPY tools/crossforge/ /opt/crossforge/lib/crossforge/
COPY --chmod=0755 tools/crossforge/launcher /usr/local/bin/crossforge
COPY config/generated/components/sources/nfpm.json \
  /work/config/sources-nfpm.json
COPY config/generated/components/implementation/crosspack.json \
  /work/config/crosspack-implementation.json
COPY config/generated/components/implementation/launcher.json \
  /work/config/crossforge-launcher.json
COPY config/generated/components/packaging/sdk-build.json \
  /work/config/crosspack-sdk.json
COPY --chmod=0755 scripts/release_component.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/release_component.py validate \
      /work/config/sources-nfpm.json \
      --expected-component sources/nfpm \
      --expected-scope build \
      --expected-sha256 "$NFPM_SOURCE_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/crosspack-implementation.json \
      --expected-component implementation/crosspack \
      --expected-scope build \
      --expected-sha256 "$CROSSPACK_IMPLEMENTATION_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/crossforge-launcher.json \
      --expected-component implementation/launcher \
      --expected-scope build \
      --expected-sha256 "$CROSSFORGE_LAUNCHER_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/crosspack-sdk.json \
      --expected-component packaging/sdk-build \
      --expected-scope build \
      --expected-sha256 "$CROSSPACK_SDK_COMPONENT_SHA256" \
    && printf '%s  %s\n' "$NFPM_BINARY_SHA256" \
      "/opt/crossforge/host-tools/nfpm/$NFPM_VERSION/bin/nfpm" \
      | sha256sum --check - \
    && "/opt/crossforge/host-tools/nfpm/$NFPM_VERSION/bin/nfpm" \
      --version | grep -F "GitVersion:    $NFPM_VERSION" \
    && crossforge info --json \
      | grep -F '"kind": "crossforge-info"' \
    && rm -rf /work
ENV CROSSFORGE_NFPM=/opt/crossforge/host-tools/nfpm/${NFPM_VERSION}/bin/nfpm \
    PATH=/opt/crossforge/host-tools/nfpm/${NFPM_VERSION}/bin:${PATH}
WORKDIR /workspace

FROM crossforge_packaging_sdk AS crosspack-packages
ARG CROSSPACK_QUALIFICATION_POLICY_COMPONENT_SHA256
COPY tests/packaging/fixtures/basic/crosspack.json /work/crosspack.json
COPY config/generated/components/implementation/crosspack-qualification.json \
  /work/config/crosspack-qualification-policy.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/build-crosspack-qualification.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/release_component.py validate \
      /work/config/crosspack-qualification-policy.json \
      --expected-component implementation/crosspack-qualification \
      --expected-scope qualification \
      --expected-sha256 \
        "$CROSSPACK_QUALIFICATION_POLICY_COMPONENT_SHA256" \
    && /usr/libexec/platform-python \
      /work/scripts/build-crosspack-qualification.py \
      --template /work/crosspack.json \
      --release /opt/crossforge/release.json \
      --crosspack /opt/crossforge/lib/crossforge/crosspack.py \
      --crossforge /usr/local/bin/crossforge \
      --output-root /qualification/packages

FROM crossforge_debian AS crosspack-deb-qualified
COPY --from=crosspack-packages /qualification/packages/ \
  /qualification/packages/
COPY --chmod=0755 scripts/qualify-crosspack-install.sh \
  /usr/local/bin/qualify-crosspack-install
RUN --network=none qualify-crosspack-install deb x86_64 \
      /qualification/packages/x86_64 \
      /qualification/deb-x86_64.ok \
    && qualify-crosspack-install deb aarch64 \
      /qualification/packages/aarch64 \
      /qualification/deb-aarch64.ok \
    && rm -rf /qualification/packages /tmp/crosspack-install-*

FROM crossforge_packaging_sdk AS crosspack-rpm-qualified
COPY --from=crosspack-packages /qualification/packages/ \
  /qualification/packages/
COPY --chmod=0755 scripts/qualify-crosspack-install.sh \
  /usr/local/bin/qualify-crosspack-install
RUN --network=none qualify-crosspack-install rpm x86_64 \
      /qualification/packages/x86_64 \
      /qualification/rpm-x86_64.ok \
    && qualify-crosspack-install rpm aarch64 \
      /qualification/packages/aarch64 \
      /qualification/rpm-aarch64.ok \
    && rm -rf /qualification/packages /tmp/crosspack-install-*

FROM crossforge_packaging_sdk AS packaging-qualified
ARG NFPM_SOURCE_COMPONENT_SHA256
ARG CROSSPACK_IMPLEMENTATION_COMPONENT_SHA256
ARG CROSSFORGE_LAUNCHER_COMPONENT_SHA256
ARG CROSSPACK_SDK_COMPONENT_SHA256
ARG CROSSPACK_QUALIFICATION_POLICY_COMPONENT_SHA256
ARG CROSSPACK_QUALIFICATION_COMPONENT_SHA256
COPY --from=crosspack-packages /qualification/packages/qualification.json \
  /work/package-qualification.json
COPY --from=crosspack-deb-qualified /qualification/*.ok \
  /work/markers/
COPY --from=crosspack-rpm-qualified /qualification/*.ok \
  /work/markers/
COPY config/generated/components/sources/nfpm.json \
  /work/config/sources-nfpm.json
COPY config/generated/components/implementation/crosspack.json \
  /work/config/crosspack-implementation.json
COPY config/generated/components/implementation/launcher.json \
  /work/config/crossforge-launcher.json
COPY config/generated/components/packaging/sdk-build.json \
  /work/config/crosspack-sdk.json
COPY config/generated/components/implementation/crosspack-qualification.json \
  /work/config/crosspack-qualification-policy.json
COPY config/generated/components/packaging/qualification.json \
  /work/config/crosspack-qualification.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/finalize-crosspack-qualification.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/finalize-crosspack-qualification.py \
      --source-component /work/config/sources-nfpm.json \
      --source-component-sha256 "$NFPM_SOURCE_COMPONENT_SHA256" \
      --implementation-component \
        /work/config/crosspack-implementation.json \
      --implementation-component-sha256 \
        "$CROSSPACK_IMPLEMENTATION_COMPONENT_SHA256" \
      --launcher-component /work/config/crossforge-launcher.json \
      --launcher-component-sha256 \
        "$CROSSFORGE_LAUNCHER_COMPONENT_SHA256" \
      --sdk-component /work/config/crosspack-sdk.json \
      --sdk-component-sha256 "$CROSSPACK_SDK_COMPONENT_SHA256" \
      --policy-component /work/config/crosspack-qualification-policy.json \
      --policy-component-sha256 \
        "$CROSSPACK_QUALIFICATION_POLICY_COMPONENT_SHA256" \
      --qualification-component /work/config/crosspack-qualification.json \
      --qualification-component-sha256 \
        "$CROSSPACK_QUALIFICATION_COMPONENT_SHA256" \
      --source-report \
        /opt/crossforge/qualification/packaging/nfpm-source.json \
      --package-report /work/package-qualification.json \
      --marker-root /work/markers \
      --output /opt/crossforge/qualification/packaging/crosspack.json \
    && rm -rf /work /qualification /tmp/crosspack-install-*
WORKDIR /workspace

FROM crossforge_packaging_qualified AS sdk-complete-dev
ARG COMPLETE_SDK_POLICY_COMPONENT_SHA256
ARG CROSSFORGE_LAUNCHER_COMPONENT_SHA256
ARG CROSSPACK_QUALIFICATION_COMPONENT_SHA256
ARG PYTHON_QUALIFICATION_COMPONENT_SHA256
ARG COMPLETE_SDK_QUALIFICATION_COMPONENT_SHA256
COPY --from=crossforge_python_sdk /opt/crossforge/python/ \
  /opt/crossforge/python/
COPY --from=crossforge_python_sdk /opt/crossforge/qualification/python/ \
  /opt/crossforge/qualification/python/
COPY --from=crossforge_python_sdk \
  /opt/crossforge/qualification/final-sdk.json \
  /opt/crossforge/qualification/python-final-sdk.json
COPY config/generated/components/implementation/complete-sdk-qualification.json \
  /work/config/complete-sdk-policy.json
COPY config/generated/components/implementation/launcher.json \
  /work/config/crossforge-launcher.json
COPY config/generated/components/packaging/qualification.json \
  /work/config/crosspack-qualification.json
COPY config/generated/components/python/qualification.json \
  /work/config/python-qualification.json
COPY config/generated/components/product/sdk-qualification.json \
  /work/config/complete-sdk-qualification.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/qualify-complete-sdk.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/qualify-complete-sdk.py \
      --policy-component /work/config/complete-sdk-policy.json \
      --policy-component-sha256 "$COMPLETE_SDK_POLICY_COMPONENT_SHA256" \
      --launcher-component /work/config/crossforge-launcher.json \
      --launcher-component-sha256 "$CROSSFORGE_LAUNCHER_COMPONENT_SHA256" \
      --packaging-component /work/config/crosspack-qualification.json \
      --packaging-component-sha256 \
        "$CROSSPACK_QUALIFICATION_COMPONENT_SHA256" \
      --python-component /work/config/python-qualification.json \
      --python-component-sha256 "$PYTHON_QUALIFICATION_COMPONENT_SHA256" \
      --qualification-component /work/config/complete-sdk-qualification.json \
      --qualification-component-sha256 \
        "$COMPLETE_SDK_QUALIFICATION_COMPONENT_SHA256" \
      --packaging-report \
        /opt/crossforge/qualification/packaging/crosspack.json \
      --python-report \
        /opt/crossforge/qualification/python-final-sdk.json \
      --crossforge /usr/local/bin/crossforge \
      --output /opt/crossforge/qualification/complete-sdk.json \
    && rm -rf /work
WORKDIR /workspace

# Public-candidate boundary. The default Bake output remains cache-only; the
# release workflow may opt in to a registry output only for this target and
# must provide the exact source commit used for the build context.
FROM sdk-complete-dev AS sdk-candidate
ARG CROSSFORGE_PRODUCT_VERSION
ARG CROSSFORGE_PRODUCT_IDENTITY_SHA256
ARG CROSSFORGE_SOURCE_COMMIT
ARG CROSSFORGE_COMPONENT_TOOLCHAIN_GCC_TESTSUITE_QUALIFICATION_SHA256
COPY config/generated/components/product/identity.json \
  /work/config/product-identity.json
COPY config/generated/components/toolchain/gcc-testsuite-qualification.json \
  /work/config/gcc-testsuite-qualification.json
COPY config/gcc-testsuite-full.json \
  /work/config/gcc-testsuite-full-plan.json
COPY tests/gcc/baselines/full/x86_64-host-direct.json \
  /work/config/gcc-testsuite-full-baseline.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/verify-gcc-testsuite-report.py /work/scripts/
RUN --network=none \
    --mount=type=bind,from=crossforge_gcc_testsuite_full_qualified,source=/qualification/gcc-testsuite/x86_64-host-direct-full.json,target=/tmp/crossforge-gcc-testsuite-full.json,ro \
      /usr/libexec/platform-python /work/scripts/release_component.py validate \
        /work/config/product-identity.json \
        --expected-component product/identity \
        --expected-scope qualification \
        --expected-sha256 "$CROSSFORGE_PRODUCT_IDENTITY_SHA256" \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
        /work/config/gcc-testsuite-qualification.json \
        --expected-component toolchain/gcc-testsuite-qualification \
        --expected-scope qualification \
        --expected-sha256 \
          "$CROSSFORGE_COMPONENT_TOOLCHAIN_GCC_TESTSUITE_QUALIFICATION_SHA256" \
    && /usr/libexec/platform-python \
        /work/scripts/verify-gcc-testsuite-report.py \
        --report /tmp/crossforge-gcc-testsuite-full.json \
        --release /opt/crossforge/release.json \
        --plan /work/config/gcc-testsuite-full-plan.json \
        --baseline /work/config/gcc-testsuite-full-baseline.json \
        --component-sha256 \
          "$CROSSFORGE_COMPONENT_TOOLCHAIN_GCC_TESTSUITE_QUALIFICATION_SHA256" \
    && test -n "$CROSSFORGE_PRODUCT_VERSION" \
    && test "$(/usr/libexec/platform-python \
          /work/scripts/release_component.py get \
          /work/config/product-identity.json \
          --expected-component product/identity \
          --expected-scope qualification \
          --expected-sha256 "$CROSSFORGE_PRODUCT_IDENTITY_SHA256" \
          --path /product/version --type string)" \
        = "\"$CROSSFORGE_PRODUCT_VERSION\"" \
    && test "${#CROSSFORGE_SOURCE_COMMIT}" -eq 40 \
    && case "$CROSSFORGE_SOURCE_COMMIT" in \
         *[!0-9a-f]*) exit 1 ;; \
         *) ;; \
       esac \
    && crossforge info --json \
      | grep -F "\"version\": \"$CROSSFORGE_PRODUCT_VERSION\"" \
    && groupadd --gid 1000 crossforge \
    && useradd --uid 1000 --gid 1000 --home-dir /home/crossforge \
      --create-home --shell /bin/bash crossforge \
    && install -d -m 0755 -o crossforge -g crossforge /workspace \
    && install -d -m 0700 -o crossforge -g crossforge \
      /home/crossforge/.cache/crossforge \
    && rm -rf /work /root/.cache/crossforge
LABEL org.opencontainers.image.title="Crossforge" \
      org.opencontainers.image.description="GTS-derived cross SDK for EL8 targets" \
      org.opencontainers.image.source="https://github.com/eglinuxer/crossforge" \
      org.opencontainers.image.version="${CROSSFORGE_PRODUCT_VERSION}" \
      org.opencontainers.image.revision="${CROSSFORGE_SOURCE_COMMIT}"
ENV HOME=/home/crossforge \
    XDG_CACHE_HOME=/home/crossforge/.cache
USER 1000:1000
WORKDIR /workspace
