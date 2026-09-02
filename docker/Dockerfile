# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

ARG CROSSFORGE_ARM64_PLATFORM=linux/arm64

# Canonical Docker-first build graph for the rewrite.
#
# Phase 1 defines the layout, Phase 2 adds x86_64, Phase 3 locks every RPM
# transaction, and Phase 4 adds the aarch64 compiler plus explicit QEMU smoke.

# The generated Bake override binds this named context to the digest in
# config/release.json. A direct Docker build has no implicit fallback image.
FROM crossforge_rocky_amd64 AS rocky-base
WORKDIR /src

# Source-only foreign rootfs: no arm64 RUN and therefore no binfmt dependency.
FROM --platform=${CROSSFORGE_ARM64_PLATFORM} crossforge_rocky_arm64 AS rocky-runtime-aarch64-source

# The amd64 QEMU executor is supplied by a manifest-pinned named context.
FROM crossforge_qemu AS qemu-executor-source

# Unmodified roots used by the CPython clean-Rocky qualification tier. Runtime
# libraries are added later from exact RPMs already present in each sysroot
# content lock; no repository is consulted during qualification.
FROM crossforge_rocky_amd64 AS rocky-runtime-x86_64-source

FROM rocky-base AS config-validate
COPY config/ ./config/
COPY evidence/ ./evidence/
COPY keys/ZSTD-RELEASE-KEY.asc ./keys/ZSTD-RELEASE-KEY.asc
COPY patches/ ./patches/
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/validate-supply-chain-evidence.py \
  ./scripts/validate-supply-chain-evidence.py
RUN /usr/libexec/platform-python ./scripts/validate-release.py \
      ./config/release.json \
      --schema ./config/schemas/release.schema.json
RUN /usr/libexec/platform-python ./scripts/validate-supply-chain-evidence.py \
      ./config/release.json

FROM config-validate AS platform-python-check
COPY scripts/ ./scripts/
COPY docker/*.py ./docker/
RUN /usr/libexec/platform-python -m py_compile ./scripts/*.py ./docker/*.py
RUN /usr/libexec/platform-python ./scripts/render-release-components.py --check

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

# Download each source RPM from its authenticated component projection.  The
# branches deliberately carry no full release/schema input, so Python metadata
# cannot invalidate either source cache.
FROM rocky-base AS gts-gcc-source
ARG CROSSFORGE_COMPONENT_SOURCES_GCC_SHA256
COPY config/generated/components/sources/gcc.json ./config/components/sources-gcc.json
COPY scripts/release_component.py ./scripts/release_component.py
COPY scripts/fetch-release-source.py ./scripts/fetch-release-source.py
RUN mkdir -p /sources \
    && /usr/libexec/platform-python ./scripts/fetch-release-source.py gcc \
         --component-file ./config/components/sources-gcc.json \
         --expected-component sources/gcc \
         --expected-scope build \
         --expected-sha256 "$CROSSFORGE_COMPONENT_SOURCES_GCC_SHA256" \
         --output /sources/gcc-toolset-15-gcc.src.rpm

FROM rocky-base AS gts-binutils-source
ARG CROSSFORGE_COMPONENT_SOURCES_BINUTILS_SHA256
COPY config/generated/components/sources/binutils.json ./config/components/sources-binutils.json
COPY scripts/release_component.py ./scripts/release_component.py
COPY scripts/fetch-release-source.py ./scripts/fetch-release-source.py
RUN mkdir -p /sources \
    && /usr/libexec/platform-python ./scripts/fetch-release-source.py binutils \
         --component-file ./config/components/sources-binutils.json \
         --expected-component sources/binutils \
         --expected-scope build \
         --expected-sha256 "$CROSSFORGE_COMPONENT_SOURCES_BINUTILS_SHA256" \
         --output /sources/gcc-toolset-15-binutils.src.rpm

# Normal locked builds start from a component-only base.  It contains schemas
# and validators but no release.json, release schema, evidence, or config tree.
FROM rocky-base AS rpm-locked-input-base
COPY config/schemas/rpm-transaction.schema.json ./config/schemas/rpm-transaction.schema.json
COPY config/schemas/rpm-lock.schema.json ./config/schemas/rpm-lock.schema.json
COPY keys/RPM-GPG-KEY-rockyofficial ./keys/RPM-GPG-KEY-rockyofficial
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/release_component.py ./scripts/release_component.py
COPY scripts/validate-rpm-lock.py ./scripts/validate-rpm-lock.py
COPY scripts/materialize-sysroot.py ./scripts/materialize-sysroot.py

# Maintenance-only resolver base retains the full release input.  Normal
# sysroot/host stages below must never inherit this branch.
FROM rocky-base AS rpm-input-base
COPY config/release.json ./config/release.json
COPY config/schemas/release.schema.json ./config/schemas/release.schema.json
COPY config/schemas/rpm-plan.schema.json ./config/schemas/rpm-plan.schema.json
COPY config/schemas/rpm-transaction.schema.json ./config/schemas/rpm-transaction.schema.json
COPY config/schemas/rpm-lock.schema.json ./config/schemas/rpm-lock.schema.json
COPY config/rpm/ ./config/rpm/
COPY keys/RPM-GPG-KEY-rockyofficial ./keys/RPM-GPG-KEY-rockyofficial
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/validate-rpm-lock.py ./scripts/validate-rpm-lock.py
COPY scripts/materialize-sysroot.py ./scripts/materialize-sysroot.py

# Fetch all RPM payloads named by the canonical sysroot transaction, then
# assemble the target root with scripts/triggers disabled and no network.
FROM rpm-locked-input-base AS sysroot-rpms-x86_64
ARG CROSSFORGE_COMPONENT_RPM_SYSROOT_X86_64_SHA256
COPY config/generated/components/rpm/sysroot-x86_64.json ./config/components/rpm-sysroot-x86_64.json
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY locks/transactions/sysroot-el8-x86_64.json ./locks/transactions/sysroot-el8-x86_64.json
COPY locks/metadata/sysroot-el8-x86_64/ ./locks/metadata/sysroot-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/sysroot-el8-x86_64.json --require-lock \
      --release-component ./config/components/rpm-sysroot-x86_64.json \
      --release-component-name rpm/sysroot-x86_64 \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_SYSROOT_X86_64_SHA256" \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-x86_64.json fetch --output /rpm-bundle --jobs 8 \
      --release-component ./config/components/rpm-sysroot-x86_64.json \
      --release-component-name rpm/sysroot-x86_64 \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_SYSROOT_X86_64_SHA256"

FROM rpm-locked-input-base AS sysroot-x86_64
ARG CROSSFORGE_COMPONENT_RPM_SYSROOT_X86_64_SHA256
COPY config/generated/components/rpm/sysroot-x86_64.json ./config/components/rpm-sysroot-x86_64.json
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY locks/transactions/sysroot-el8-x86_64.json ./locks/transactions/sysroot-el8-x86_64.json
COPY locks/metadata/sysroot-el8-x86_64/ ./locks/metadata/sysroot-el8-x86_64/
COPY --from=sysroot-rpms-x86_64 /rpm-bundle/ /rpm-bundle/
RUN --network=none /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-x86_64.json install \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --destination /opt/crossforge/sysroots/el8/x86_64 \
      --release-component ./config/components/rpm-sysroot-x86_64.json \
      --release-component-name rpm/sysroot-x86_64 \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_SYSROOT_X86_64_SHA256"

FROM rpm-locked-input-base AS sysroot-rpms-aarch64
ARG CROSSFORGE_COMPONENT_RPM_SYSROOT_AARCH64_SHA256
COPY config/generated/components/rpm/sysroot-aarch64.json ./config/components/rpm-sysroot-aarch64.json
COPY locks/sysroot-el8-aarch64.json ./locks/sysroot-el8-aarch64.json
COPY locks/transactions/sysroot-el8-aarch64.json ./locks/transactions/sysroot-el8-aarch64.json
COPY locks/metadata/sysroot-el8-aarch64/ ./locks/metadata/sysroot-el8-aarch64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/sysroot-el8-aarch64.json --require-lock \
      --release-component ./config/components/rpm-sysroot-aarch64.json \
      --release-component-name rpm/sysroot-aarch64 \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_SYSROOT_AARCH64_SHA256" \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-aarch64.json fetch --output /rpm-bundle --jobs 8 \
      --release-component ./config/components/rpm-sysroot-aarch64.json \
      --release-component-name rpm/sysroot-aarch64 \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_SYSROOT_AARCH64_SHA256"

FROM rpm-locked-input-base AS sysroot-aarch64
ARG CROSSFORGE_COMPONENT_RPM_SYSROOT_AARCH64_SHA256
COPY config/generated/components/rpm/sysroot-aarch64.json ./config/components/rpm-sysroot-aarch64.json
COPY locks/sysroot-el8-aarch64.json ./locks/sysroot-el8-aarch64.json
COPY locks/transactions/sysroot-el8-aarch64.json ./locks/transactions/sysroot-el8-aarch64.json
COPY locks/metadata/sysroot-el8-aarch64/ ./locks/metadata/sysroot-el8-aarch64/
COPY --from=sysroot-rpms-aarch64 /rpm-bundle/ /rpm-bundle/
RUN --network=none /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-aarch64.json install \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --destination /opt/crossforge/sysroots/el8/aarch64 \
      --release-component ./config/components/rpm-sysroot-aarch64.json \
      --release-component-name rpm/sysroot-aarch64 \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_SYSROOT_AARCH64_SHA256"

# The common host transaction exactly reproduces the previously qualified prep
# environment. RPM scripts/triggers execute, but the package set is immutable
# and installation runs without repositories or network access.
FROM rpm-locked-input-base AS host-build-common-rpms
ARG CROSSFORGE_COMPONENT_RPM_HOST_BUILD_COMMON_SHA256
COPY config/generated/components/rpm/host-build-common.json ./config/components/rpm-host-build-common.json
COPY locks/host-build-common-el8-x86_64.json ./locks/host-build-common-el8-x86_64.json
COPY locks/transactions/host-build-common-el8-x86_64.json ./locks/transactions/host-build-common-el8-x86_64.json
COPY locks/metadata/host-build-common-el8-x86_64/ ./locks/metadata/host-build-common-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/host-build-common-el8-x86_64.json --require-lock \
      --release-component ./config/components/rpm-host-build-common.json \
      --release-component-name rpm/host-build-common \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_BUILD_COMMON_SHA256" \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/host-build-common-el8-x86_64.json fetch \
      --output /rpm-bundle --jobs 8 \
      --release-component ./config/components/rpm-host-build-common.json \
      --release-component-name rpm/host-build-common \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_BUILD_COMMON_SHA256"

FROM rpm-locked-input-base AS host-build-common-locked
ARG CROSSFORGE_COMPONENT_RPM_HOST_BUILD_COMMON_SHA256
COPY config/generated/components/rpm/host-build-common.json ./config/components/rpm-host-build-common.json
COPY locks/host-build-common-el8-x86_64.json ./locks/host-build-common-el8-x86_64.json
COPY locks/transactions/host-build-common-el8-x86_64.json ./locks/transactions/host-build-common-el8-x86_64.json
COPY locks/metadata/host-build-common-el8-x86_64/ ./locks/metadata/host-build-common-el8-x86_64/
COPY scripts/install-host-rpm-lock.py ./scripts/install-host-rpm-lock.py
RUN --network=none \
    --mount=type=bind,from=host-build-common-rpms,source=/rpm-bundle,target=/rpm-bundle,ro \
    /usr/libexec/platform-python ./scripts/install-host-rpm-lock.py \
      ./locks/host-build-common-el8-x86_64.json \
      --transaction ./locks/transactions/host-build-common-el8-x86_64.json \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --marker /usr/share/crossforge/rpm-locks/host-build-common.json \
      --release-component ./config/components/rpm-host-build-common.json \
      --release-component-name rpm/host-build-common \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_BUILD_COMMON_SHA256"
ENV PATH="/opt/rh/gcc-toolset-15/root/usr/bin:${PATH}"

FROM host-build-common-locked AS qemu-aarch64-validated
ARG QEMU_EXECUTOR_VERSION
ARG QEMU_EXECUTOR_BINARY_SHA256
COPY --from=qemu-executor-source \
  /usr/bin/qemu-aarch64 /usr/local/libexec/crossforge/qemu-aarch64
RUN printf '%s  %s\n' \
      "$QEMU_EXECUTOR_BINARY_SHA256" \
      /usr/local/libexec/crossforge/qemu-aarch64 \
      | sha256sum -c - \
    && file /usr/local/libexec/crossforge/qemu-aarch64 \
      | grep -F 'x86-64' \
    && file /usr/local/libexec/crossforge/qemu-aarch64 \
      | grep -F 'static-pie linked' \
    && ! readelf -l /usr/local/libexec/crossforge/qemu-aarch64 \
      | grep -q 'Requesting program interpreter' \
    && /usr/local/libexec/crossforge/qemu-aarch64 --version \
      | head -n 1 \
      | grep -F "qemu-aarch64 version $QEMU_EXECUTOR_VERSION"

# GCC-only prerequisites are a separate additive lock. Keeping them out of the
# common layer prevents libzstd-devel from changing binutils auto-detection.
FROM rpm-locked-input-base AS host-gcc-build-rpms
ARG CROSSFORGE_COMPONENT_RPM_HOST_GCC_BUILD_SHA256
COPY config/generated/components/rpm/host-gcc-build.json ./config/components/rpm-host-gcc-build.json
COPY locks/host-build-common-el8-x86_64.json ./locks/host-build-common-el8-x86_64.json
COPY locks/transactions/host-build-common-el8-x86_64.json ./locks/transactions/host-build-common-el8-x86_64.json
COPY locks/host-gcc-build-el8-x86_64.json ./locks/host-gcc-build-el8-x86_64.json
COPY locks/transactions/host-gcc-build-el8-x86_64.json ./locks/transactions/host-gcc-build-el8-x86_64.json
COPY locks/metadata/host-gcc-build-el8-x86_64/ ./locks/metadata/host-gcc-build-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/host-gcc-build-el8-x86_64.json --require-lock \
      --release-component ./config/components/rpm-host-gcc-build.json \
      --release-component-name rpm/host-gcc-build \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_GCC_BUILD_SHA256" \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/host-gcc-build-el8-x86_64.json fetch \
      --output /rpm-bundle --jobs 4 \
      --release-component ./config/components/rpm-host-gcc-build.json \
      --release-component-name rpm/host-gcc-build \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_GCC_BUILD_SHA256"

FROM host-build-common-locked AS host-gcc-build-locked
ARG CROSSFORGE_COMPONENT_RPM_HOST_GCC_BUILD_SHA256
COPY config/generated/components/rpm/host-gcc-build.json ./config/components/rpm-host-gcc-build.json
COPY locks/host-gcc-build-el8-x86_64.json ./locks/host-gcc-build-el8-x86_64.json
COPY locks/transactions/host-gcc-build-el8-x86_64.json ./locks/transactions/host-gcc-build-el8-x86_64.json
COPY locks/metadata/host-gcc-build-el8-x86_64/ ./locks/metadata/host-gcc-build-el8-x86_64/
COPY scripts/install-host-rpm-lock.py ./scripts/install-host-rpm-lock.py
RUN --network=none \
    --mount=type=bind,from=host-gcc-build-rpms,source=/rpm-bundle,target=/rpm-bundle-gcc,ro \
    /usr/libexec/platform-python ./scripts/install-host-rpm-lock.py \
      ./locks/host-gcc-build-el8-x86_64.json \
      --transaction ./locks/transactions/host-gcc-build-el8-x86_64.json \
      --bundle /rpm-bundle-gcc \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --marker /usr/share/crossforge/rpm-locks/host-gcc-build.json \
      --release-component ./config/components/rpm-host-gcc-build.json \
      --release-component-name rpm/host-gcc-build \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_GCC_BUILD_SHA256"

# CPython's native build interpreter has its own additive development closure;
# it must not inherit GCC-only bison/flex or couple Python to that lock.
FROM rpm-locked-input-base AS host-python-build-rpms
ARG CROSSFORGE_COMPONENT_RPM_HOST_PYTHON_BUILD_SHA256
COPY config/generated/components/rpm/host-python-build.json ./config/components/rpm-host-python-build.json
COPY locks/host-build-common-el8-x86_64.json ./locks/host-build-common-el8-x86_64.json
COPY locks/transactions/host-build-common-el8-x86_64.json ./locks/transactions/host-build-common-el8-x86_64.json
COPY locks/host-python-build-el8-x86_64.json ./locks/host-python-build-el8-x86_64.json
COPY locks/transactions/host-python-build-el8-x86_64.json ./locks/transactions/host-python-build-el8-x86_64.json
COPY locks/metadata/host-python-build-el8-x86_64/ ./locks/metadata/host-python-build-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/host-python-build-el8-x86_64.json --require-lock \
      --release-component ./config/components/rpm-host-python-build.json \
      --release-component-name rpm/host-python-build \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_PYTHON_BUILD_SHA256" \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/host-python-build-el8-x86_64.json fetch \
      --output /rpm-bundle --jobs 8 \
      --release-component ./config/components/rpm-host-python-build.json \
      --release-component-name rpm/host-python-build \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_PYTHON_BUILD_SHA256"

FROM host-build-common-locked AS host-python-build-locked
ARG CROSSFORGE_COMPONENT_RPM_HOST_PYTHON_BUILD_SHA256
COPY config/generated/components/rpm/host-python-build.json ./config/components/rpm-host-python-build.json
COPY locks/host-python-build-el8-x86_64.json ./locks/host-python-build-el8-x86_64.json
COPY locks/transactions/host-python-build-el8-x86_64.json ./locks/transactions/host-python-build-el8-x86_64.json
COPY locks/metadata/host-python-build-el8-x86_64/ ./locks/metadata/host-python-build-el8-x86_64/
COPY scripts/install-host-rpm-lock.py ./scripts/install-host-rpm-lock.py
RUN --network=none \
    --mount=type=bind,from=host-python-build-rpms,source=/rpm-bundle,target=/rpm-bundle-python,ro \
    /usr/libexec/platform-python ./scripts/install-host-rpm-lock.py \
      ./locks/host-python-build-el8-x86_64.json \
      --transaction ./locks/transactions/host-python-build-el8-x86_64.json \
      --bundle /rpm-bundle-python \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --marker /usr/share/crossforge/rpm-locks/host-python-build.json \
      --release-component ./config/components/rpm-host-python-build.json \
      --release-component-name rpm/host-python-build \
      --release-component-sha256 "$CROSSFORGE_COMPONENT_RPM_HOST_PYTHON_BUILD_SHA256"

# Qualification-only clean Rocky roots. Each starts from the exact OCI child
# and receives only the seven target runtime RPMs needed by the CPython module
# contract. The complete sysroot bundle is nevertheless re-hashed and
# re-verified before those RPMs are selected.
FROM host-build-common-locked AS python-runtime-clean-x86_64
ARG ROCKY_AMD64_MANIFEST_DIGEST
COPY --from=config-validate /src/config/release.json /src/config/release.json
COPY --from=config-validate /src/config/schemas/release.schema.json /src/config/schemas/release.schema.json
COPY --from=config-validate /src/config/schemas/rpm-plan.schema.json /src/config/schemas/rpm-plan.schema.json
COPY --from=config-validate /src/config/rpm/sysroot-el8-x86_64.plan.json /src/config/rpm/sysroot-el8-x86_64.plan.json
COPY --from=rocky-runtime-x86_64-source / /runtime-root/
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY locks/transactions/sysroot-el8-x86_64.json ./locks/transactions/sysroot-el8-x86_64.json
COPY locks/metadata/sysroot-el8-x86_64/ ./locks/metadata/sysroot-el8-x86_64/
COPY scripts/assemble-python-runtime.py ./scripts/assemble-python-runtime.py
RUN --network=none \
    --mount=type=bind,from=sysroot-rpms-x86_64,source=/rpm-bundle,target=/rpm-bundle,ro \
    /usr/libexec/platform-python ./scripts/assemble-python-runtime.py \
      ./locks/sysroot-el8-x86_64.json \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --runtime-root /runtime-root \
      --base-image-manifest-digest "$ROCKY_AMD64_MANIFEST_DIGEST" \
      --evidence /work/qualification/python/runtime-clean-x86_64.json

FROM host-build-common-locked AS python-runtime-clean-aarch64
ARG ROCKY_ARM64_MANIFEST_DIGEST
COPY --from=config-validate /src/config/release.json /src/config/release.json
COPY --from=config-validate /src/config/schemas/release.schema.json /src/config/schemas/release.schema.json
COPY --from=config-validate /src/config/schemas/rpm-plan.schema.json /src/config/schemas/rpm-plan.schema.json
COPY --from=config-validate /src/config/rpm/sysroot-el8-aarch64.plan.json /src/config/rpm/sysroot-el8-aarch64.plan.json
COPY --from=rocky-runtime-aarch64-source / /runtime-root/
COPY locks/sysroot-el8-aarch64.json ./locks/sysroot-el8-aarch64.json
COPY locks/transactions/sysroot-el8-aarch64.json ./locks/transactions/sysroot-el8-aarch64.json
COPY locks/metadata/sysroot-el8-aarch64/ ./locks/metadata/sysroot-el8-aarch64/
COPY scripts/assemble-python-runtime.py ./scripts/assemble-python-runtime.py
RUN --network=none \
    --mount=type=bind,from=sysroot-rpms-aarch64,source=/rpm-bundle,target=/rpm-bundle,ro \
    /usr/libexec/platform-python ./scripts/assemble-python-runtime.py \
      ./locks/sysroot-el8-aarch64.json \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --runtime-root /runtime-root \
      --base-image-manifest-digest "$ROCKY_ARM64_MANIFEST_DIGEST" \
      --evidence /work/qualification/python/runtime-clean-aarch64.json

# Maintenance-only canonical resolvers. They are not dependencies of normal
# builds: release stages consume checked-in manifests/locks exclusively. Export
# one with `--set <target>.output=type=local,dest=...` for an audited lock update.
FROM rpm-input-base AS rpm-resolve-sysroot-x86_64
COPY scripts/resolve-rpm-transaction.py ./scripts/resolve-rpm-transaction.py
COPY scripts/render-rpm-lock.py ./scripts/render-rpm-lock.py
RUN /usr/libexec/platform-python ./scripts/resolve-rpm-transaction.py \
      --plan ./config/rpm/sysroot-el8-x86_64.plan.json \
      --plan-schema ./config/schemas/rpm-plan.schema.json \
      --release-config ./config/release.json \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/transaction.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata
RUN --network=none /usr/libexec/platform-python ./scripts/render-rpm-lock.py \
      --transaction /resolved/transaction.json \
      --transaction-reference locks/transactions/sysroot-el8-x86_64.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/lock.json

FROM scratch AS rpm-lock-sysroot-x86_64
COPY --from=rpm-resolve-sysroot-x86_64 /resolved/ /

FROM rpm-input-base AS rpm-resolve-sysroot-aarch64
COPY scripts/resolve-rpm-transaction.py ./scripts/resolve-rpm-transaction.py
COPY scripts/render-rpm-lock.py ./scripts/render-rpm-lock.py
RUN /usr/libexec/platform-python ./scripts/resolve-rpm-transaction.py \
      --plan ./config/rpm/sysroot-el8-aarch64.plan.json \
      --plan-schema ./config/schemas/rpm-plan.schema.json \
      --release-config ./config/release.json \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/transaction.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata
RUN --network=none /usr/libexec/platform-python ./scripts/render-rpm-lock.py \
      --transaction /resolved/transaction.json \
      --transaction-reference locks/transactions/sysroot-el8-aarch64.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/lock.json

FROM scratch AS rpm-lock-sysroot-aarch64
COPY --from=rpm-resolve-sysroot-aarch64 /resolved/ /

FROM rpm-input-base AS rpm-resolve-host-build-common
COPY scripts/resolve-rpm-transaction.py ./scripts/resolve-rpm-transaction.py
COPY scripts/render-rpm-lock.py ./scripts/render-rpm-lock.py
RUN /usr/libexec/platform-python ./scripts/resolve-rpm-transaction.py \
      --plan ./config/rpm/host-build-common-el8-x86_64.plan.json \
      --plan-schema ./config/schemas/rpm-plan.schema.json \
      --release-config ./config/release.json \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/transaction.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata
RUN --network=none /usr/libexec/platform-python ./scripts/render-rpm-lock.py \
      --transaction /resolved/transaction.json \
      --transaction-reference locks/transactions/host-build-common-el8-x86_64.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/lock.json

FROM scratch AS rpm-lock-host-build-common
COPY --from=rpm-resolve-host-build-common /resolved/ /

FROM host-build-common-locked AS rpm-resolve-host-gcc-build
COPY --from=rpm-input-base /src/config/ ./config/
COPY scripts/resolve-rpm-transaction.py ./scripts/resolve-rpm-transaction.py
COPY scripts/render-rpm-lock.py ./scripts/render-rpm-lock.py
RUN /usr/libexec/platform-python ./scripts/resolve-rpm-transaction.py \
      --plan ./config/rpm/host-gcc-build-el8-x86_64.plan.json \
      --plan-schema ./config/schemas/rpm-plan.schema.json \
      --release-config ./config/release.json \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/transaction.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata
RUN --network=none /usr/libexec/platform-python ./scripts/render-rpm-lock.py \
      --transaction /resolved/transaction.json \
      --transaction-reference locks/transactions/host-gcc-build-el8-x86_64.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/lock.json

FROM scratch AS rpm-lock-host-gcc-build
COPY --from=rpm-resolve-host-gcc-build /resolved/ /

FROM host-build-common-locked AS rpm-resolve-host-python-build
COPY --from=rpm-input-base /src/config/ ./config/
COPY scripts/resolve-rpm-transaction.py ./scripts/resolve-rpm-transaction.py
COPY scripts/render-rpm-lock.py ./scripts/render-rpm-lock.py
RUN /usr/libexec/platform-python ./scripts/resolve-rpm-transaction.py \
      --plan ./config/rpm/host-python-build-el8-x86_64.plan.json \
      --plan-schema ./config/schemas/rpm-plan.schema.json \
      --release-config ./config/release.json \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/transaction.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata
RUN --network=none /usr/libexec/platform-python ./scripts/render-rpm-lock.py \
      --transaction /resolved/transaction.json \
      --transaction-reference locks/transactions/host-python-build-el8-x86_64.json \
      --rpm-dir /resolved/rpms \
      --metadata-dir /resolved/metadata \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --output /resolved/lock.json

FROM scratch AS rpm-lock-host-python-build
COPY --from=rpm-resolve-host-python-build /resolved/ /

FROM host-build-common-locked AS binutils-prep-input
ARG GTS_BINUTILS_SHA256
ARG GTS_BINUTILS_SPEC_SHA256
ARG GTS_BINUTILS_REPOSITORY_NEVRA
ARG GTS_BINUTILS_HEADER_ARCH
ARG ROCKY_RPM_TRUST_SHA256
ARG ROCKY_RPM_TRUST_FINGERPRINT
COPY --from=gts-binutils-source \
  /sources/gcc-toolset-15-binutils.src.rpm \
  /sources/gcc-toolset-15-binutils.src.rpm
COPY keys/RPM-GPG-KEY-rockyofficial /work/keys/RPM-GPG-KEY-rockyofficial
COPY scripts/prepare-srpm.sh /work/scripts/prepare-srpm.sh

FROM binutils-prep-input AS binutils-prepared-x86_64
RUN --network=none /work/scripts/prepare-srpm.sh \
      /sources/gcc-toolset-15-binutils.src.rpm \
      "$GTS_BINUTILS_SHA256" \
      "$GTS_BINUTILS_REPOSITORY_NEVRA" \
      "$GTS_BINUTILS_HEADER_ARCH" \
      binutils.spec \
      "$GTS_BINUTILS_SPEC_SHA256" \
      binutils-with-gold-2.44 \
      /work/prepared/binutils \
      /work/keys/RPM-GPG-KEY-rockyofficial \
      "$ROCKY_RPM_TRUST_SHA256" \
      "$ROCKY_RPM_TRUST_FINGERPRINT" \
      x86_64

FROM binutils-prep-input AS binutils-prepared-aarch64
RUN --network=none /work/scripts/prepare-srpm.sh \
      /sources/gcc-toolset-15-binutils.src.rpm \
      "$GTS_BINUTILS_SHA256" \
      "$GTS_BINUTILS_REPOSITORY_NEVRA" \
      "$GTS_BINUTILS_HEADER_ARCH" \
      binutils.spec \
      "$GTS_BINUTILS_SPEC_SHA256" \
      binutils-with-gold-2.44 \
      /work/prepared/binutils \
      /work/keys/RPM-GPG-KEY-rockyofficial \
      "$ROCKY_RPM_TRUST_SHA256" \
      "$ROCKY_RPM_TRUST_FINGERPRINT" \
      aarch64

# Backward-compatible maintenance alias for the first target.
FROM binutils-prepared-x86_64 AS binutils-prepared

FROM host-build-common-locked AS gcc-prep-input
ARG GTS_GCC_SHA256
ARG GTS_GCC_SPEC_SHA256
ARG GTS_GCC_REPOSITORY_NEVRA
ARG GTS_GCC_HEADER_ARCH
ARG ROCKY_RPM_TRUST_SHA256
ARG ROCKY_RPM_TRUST_FINGERPRINT
COPY --from=gts-gcc-source \
  /sources/gcc-toolset-15-gcc.src.rpm \
  /sources/gcc-toolset-15-gcc.src.rpm
COPY keys/RPM-GPG-KEY-rockyofficial /work/keys/RPM-GPG-KEY-rockyofficial
COPY scripts/prepare-srpm.sh /work/scripts/prepare-srpm.sh

FROM gcc-prep-input AS gcc-prepared-x86_64
RUN --network=none /work/scripts/prepare-srpm.sh \
      /sources/gcc-toolset-15-gcc.src.rpm \
      "$GTS_GCC_SHA256" \
      "$GTS_GCC_REPOSITORY_NEVRA" \
      "$GTS_GCC_HEADER_ARCH" \
      gcc-toolset-15-gcc.spec \
      "$GTS_GCC_SPEC_SHA256" \
      gcc-15.2.1-20260123 \
      /work/prepared/gcc \
      /work/keys/RPM-GPG-KEY-rockyofficial \
      "$ROCKY_RPM_TRUST_SHA256" \
      "$ROCKY_RPM_TRUST_FINGERPRINT" \
      x86_64

FROM gcc-prep-input AS gcc-prepared-aarch64
RUN --network=none /work/scripts/prepare-srpm.sh \
      /sources/gcc-toolset-15-gcc.src.rpm \
      "$GTS_GCC_SHA256" \
      "$GTS_GCC_REPOSITORY_NEVRA" \
      "$GTS_GCC_HEADER_ARCH" \
      gcc-toolset-15-gcc.spec \
      "$GTS_GCC_SPEC_SHA256" \
      gcc-15.2.1-20260123 \
      /work/prepared/gcc \
      /work/keys/RPM-GPG-KEY-rockyofficial \
      "$ROCKY_RPM_TRUST_SHA256" \
      "$ROCKY_RPM_TRUST_FINGERPRINT" \
      aarch64

# Backward-compatible maintenance alias for the first target.
FROM gcc-prepared-x86_64 AS gcc-prepared

FROM binutils-prepared-x86_64 AS binutils-x86_64
ARG CROSSFORGE_JOBS=4
COPY scripts/build-binutils.sh /work/scripts/build-binutils.sh
RUN --network=none mkdir -p /opt/crossforge/sysroots/el8/x86_64 \
    && /work/scripts/build-binutils.sh \
      /work/prepared/binutils \
      /work/build/binutils-x86_64 \
      /work/binutils-stage \
      /opt/crossforge/targets/x86_64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/x86_64 \
      x86_64-unknown-linux-gnu \
      "$CROSSFORGE_JOBS"

FROM host-gcc-build-locked AS gcc-x86_64-built
ARG CROSSFORGE_JOBS=4
COPY --from=gcc-prepared-x86_64 /work/prepared/gcc/ /work/prepared/gcc/
COPY --from=sysroot-x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=binutils-x86_64 /work/binutils-stage/ /
COPY scripts/build-gcc.sh /work/scripts/build-gcc.sh
RUN --network=none /work/scripts/build-gcc.sh \
      /work/prepared/gcc \
      /work/build/gcc-x86_64 \
      /work/gcc-stage \
      /opt/crossforge/targets/x86_64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/x86_64 \
      x86_64-unknown-linux-gnu \
      "$CROSSFORGE_JOBS"

FROM gcc-x86_64-built AS gcc-x86_64
COPY scripts/install-hybrid-runtime.sh /work/scripts/install-hybrid-runtime.sh
RUN --network=none /work/scripts/install-hybrid-runtime.sh \
      /work/build/gcc-x86_64/gcc-build \
      /work/gcc-stage \
      /opt/crossforge/targets/x86_64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/x86_64 \
      x86_64-unknown-linux-gnu \
      15 \
      x86_64

FROM binutils-prepared-aarch64 AS binutils-aarch64
ARG CROSSFORGE_JOBS=4
COPY scripts/build-binutils.sh /work/scripts/build-binutils.sh
RUN --network=none mkdir -p /opt/crossforge/sysroots/el8/aarch64 \
    && /work/scripts/build-binutils.sh \
      /work/prepared/binutils \
      /work/build/binutils-aarch64 \
      /work/binutils-stage \
      /opt/crossforge/targets/aarch64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/aarch64 \
      aarch64-unknown-linux-gnu \
      "$CROSSFORGE_JOBS"

FROM host-gcc-build-locked AS gcc-aarch64-built
ARG CROSSFORGE_JOBS=4
COPY --from=gcc-prepared-aarch64 /work/prepared/gcc/ /work/prepared/gcc/
COPY --from=sysroot-aarch64 \
  /opt/crossforge/sysroots/el8/aarch64/ \
  /opt/crossforge/sysroots/el8/aarch64/
COPY --from=binutils-aarch64 /work/binutils-stage/ /
COPY scripts/build-gcc.sh /work/scripts/build-gcc.sh
RUN --network=none /work/scripts/build-gcc.sh \
      /work/prepared/gcc \
      /work/build/gcc-aarch64 \
      /work/gcc-stage \
      /opt/crossforge/targets/aarch64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/aarch64 \
      aarch64-unknown-linux-gnu \
      "$CROSSFORGE_JOBS"

FROM gcc-aarch64-built AS gcc-aarch64
COPY scripts/install-hybrid-runtime.sh /work/scripts/install-hybrid-runtime.sh
RUN --network=none /work/scripts/install-hybrid-runtime.sh \
      /work/build/gcc-aarch64/gcc-build \
      /work/gcc-stage \
      /opt/crossforge/targets/aarch64-unknown-linux-gnu \
      /opt/crossforge/sysroots/el8/aarch64 \
      aarch64-unknown-linux-gnu \
      15 \
      aarch64

# Build-only toolchain boundaries for downstream cross builds. These scratch
# exports deliberately precede every qualification/runtime stage and contain
# only one target compiler tree plus its matching hybrid sysroot.
FROM scratch AS toolchain-x86_64-build-export
COPY --from=sysroot-x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=binutils-x86_64 /work/binutils-stage/opt/ /opt/
COPY --from=gcc-x86_64 /work/gcc-stage/opt/ /opt/

FROM scratch AS toolchain-aarch64-build-export
COPY --from=sysroot-aarch64 \
  /opt/crossforge/sysroots/el8/aarch64/ \
  /opt/crossforge/sysroots/el8/aarch64/
COPY --from=binutils-aarch64 /work/binutils-stage/opt/ /opt/
COPY --from=gcc-aarch64 /work/gcc-stage/opt/ /opt/

# Real compiler smoke gate. This target remains non-publishable until frozen
# ABI sets, full tests and the release supply chain exist.
FROM host-build-common-locked AS toolchain-x86_64-qualify-build
COPY --from=config-validate /src/config/release.json /src/config/release.json
COPY --from=sysroot-x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=binutils-x86_64 /work/binutils-stage/ /
COPY --from=gcc-x86_64 /work/gcc-stage/ /
COPY scripts/qualify-toolchain.py /work/scripts/qualify-toolchain.py
COPY scripts/loader_evidence.py /work/scripts/loader_evidence.py
COPY tests/smoke/ /work/tests/smoke/
RUN --network=none /usr/libexec/platform-python /work/scripts/qualify-toolchain.py \
      --prefix /opt/crossforge/targets/x86_64-unknown-linux-gnu \
      --sysroot /opt/crossforge/sysroots/el8/x86_64 \
      --target x86_64-unknown-linux-gnu \
      --work /work/qualification/x86_64 \
      --report /work/qualification/x86_64.json \
      --release /src/config/release.json

FROM host-build-common-locked AS toolchain-aarch64-qualify-build
COPY --from=config-validate /src/config/release.json /src/config/release.json
COPY --from=sysroot-aarch64 \
  /opt/crossforge/sysroots/el8/aarch64/ \
  /opt/crossforge/sysroots/el8/aarch64/
COPY --from=binutils-aarch64 /work/binutils-stage/ /
COPY --from=gcc-aarch64 /work/gcc-stage/ /
COPY scripts/qualify-toolchain.py /work/scripts/qualify-toolchain.py
COPY scripts/loader_evidence.py /work/scripts/loader_evidence.py
COPY tests/smoke/ /work/tests/smoke/
RUN --network=none /usr/libexec/platform-python /work/scripts/qualify-toolchain.py \
      --prefix /opt/crossforge/targets/aarch64-unknown-linux-gnu \
      --sysroot /opt/crossforge/sysroots/el8/aarch64 \
      --target aarch64-unknown-linux-gnu \
      --work /work/qualification/aarch64 \
      --report /work/qualification/aarch64-compile.json \
      --release /src/config/release.json \
      --skip-sysroot-execution

# Run the aarch64 artifacts in both the locked sysroot and a clean arm64 Rocky
# root through an explicit, content-locked static QEMU binary. No binfmt
# registration is used. The finalizer binds both observations into JSON.
FROM host-build-common-locked AS runtime-smoke-aarch64
ARG QEMU_EXECUTOR_CPU
ARG QEMU_EXECUTOR_UNAME_RELEASE
COPY --from=config-validate /src/config/release.json /src/config/release.json
COPY --from=rocky-runtime-aarch64-source / /runtime-root/
COPY --from=sysroot-aarch64 \
  /opt/crossforge/sysroots/el8/aarch64/ /sysroot/
COPY --from=toolchain-aarch64-qualify-build \
  /work/qualification/aarch64/ /work/qualification/aarch64/
COPY --from=toolchain-aarch64-qualify-build \
  /work/qualification/aarch64-compile.json \
  /work/qualification/aarch64-compile.json
COPY scripts/run-aarch64-smoke.sh /work/scripts/run-aarch64-smoke.sh
COPY scripts/loader_evidence.py /work/scripts/loader_evidence.py
COPY scripts/finalize-aarch64-qualification.py \
  /work/scripts/finalize-aarch64-qualification.py
RUN mkdir -p \
      /sysroot/.crossforge \
      /sysroot/opt/crossforge-qualification \
      /runtime-root/.crossforge \
      /runtime-root/opt/crossforge-qualification \
    && cp -a /work/qualification/aarch64/. \
      /sysroot/opt/crossforge-qualification/ \
    && cp -a /work/qualification/aarch64/. \
      /runtime-root/opt/crossforge-qualification/
RUN --network=none \
    --mount=type=bind,from=qemu-aarch64-validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/sysroot/.crossforge/qemu-aarch64,ro \
    /work/scripts/run-aarch64-smoke.sh \
        /sysroot \
        /.crossforge/qemu-aarch64 \
        "$QEMU_EXECUTOR_CPU" \
        "$QEMU_EXECUTOR_UNAME_RELEASE" \
        locked-sysroot \
        /work/qualification/locked-sysroot-aarch64.result
RUN --network=none \
    --mount=type=bind,from=qemu-aarch64-validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/runtime-root/.crossforge/qemu-aarch64,ro \
    /work/scripts/run-aarch64-smoke.sh \
        /runtime-root \
        /.crossforge/qemu-aarch64 \
        "$QEMU_EXECUTOR_CPU" \
        "$QEMU_EXECUTOR_UNAME_RELEASE" \
        clean-rocky \
        /work/qualification/clean-rocky-aarch64.result
RUN --network=none \
    --mount=type=bind,from=qemu-aarch64-validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/work/qemu-aarch64,ro \
    /usr/libexec/platform-python \
      /work/scripts/finalize-aarch64-qualification.py \
      --compile-report /work/qualification/aarch64-compile.json \
      --release /src/config/release.json \
      --artifacts /work/qualification/aarch64 \
      --locked-sysroot-result \
        /work/qualification/locked-sysroot-aarch64.result \
      --clean-runtime-result /work/qualification/clean-rocky-aarch64.result \
      --qemu /work/qemu-aarch64 \
      --output /work/qualification/aarch64.json

# Re-run every linked executable in a clean pinned Rocky base. Only test
# artifacts cross this boundary, so host build packages cannot mask runtime
# dependencies or accidentally satisfy a RUNPATH.
FROM config-validate AS runtime-smoke-x86_64
COPY --from=toolchain-x86_64-qualify-build \
  /work/qualification/x86_64/ /work/qualification/x86_64/
COPY --from=toolchain-x86_64-qualify-build \
  /work/qualification/x86_64.json /work/qualification/x86_64.json
RUN --network=none /work/qualification/x86_64/hello \
    && /work/qualification/x86_64/modern \
    && /work/qualification/x86_64/lto \
    && /work/qualification/x86_64/lto-archive \
    && /work/qualification/x86_64/libgcc-helper \
    && /work/qualification/x86_64/catch \
    && printf '%s\n' 'passed' > /work/qualification/clean-rocky.ok

# Inspectable dev toolchain that depends on both the exact-sysroot qualification
# and the clean-Rocky portability gate. It remains cache-only and unpublished.
FROM host-build-common-locked AS toolchain-x86_64-dev
COPY --from=sysroot-x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=binutils-x86_64 /work/binutils-stage/ /
COPY --from=gcc-x86_64 /work/gcc-stage/ /
COPY --from=runtime-smoke-x86_64 \
  /work/qualification/ /work/qualification/
RUN test ! -e /rpm-bundle && test ! -e /rpm-bundle-gcc

FROM host-build-common-locked AS toolchain-aarch64-dev
COPY --from=sysroot-aarch64 \
  /opt/crossforge/sysroots/el8/aarch64/ \
  /opt/crossforge/sysroots/el8/aarch64/
COPY --from=binutils-aarch64 /work/binutils-stage/ /
COPY --from=gcc-aarch64 /work/gcc-stage/ /
COPY --from=runtime-smoke-aarch64 \
  /work/qualification/ /work/qualification/
RUN test ! -e /rpm-bundle \
    && test ! -e /rpm-bundle-gcc \
    && test ! -e /usr/local/libexec/crossforge/qemu-aarch64
