# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Canonical Docker-first build graph for the rewrite.
#
# Phase 1 stops at configuration validation and filesystem layout. Phase 2
# adds the x86_64 compiler slice. Phase 3 replaces every live host DNF
# transaction with signed, content-locked common and GCC-additive layers.

# The generated Bake override binds this named context to the digest in
# config/release.json. A direct Docker build has no implicit fallback image.
FROM crossforge_rocky AS rocky-base
WORKDIR /src

FROM rocky-base AS config-validate
COPY config/ ./config/
COPY scripts/validate-release.py ./scripts/validate-release.py
RUN /usr/libexec/platform-python ./scripts/validate-release.py \
      ./config/release.json \
      --schema ./config/schemas/release.schema.json

FROM config-validate AS platform-python-check
COPY scripts/ ./scripts/
RUN /usr/libexec/platform-python -m py_compile ./scripts/*.py

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

# Download each source RPM on an independent branch using only release.json.
# A GCC source update therefore cannot invalidate the binutils build cache.
FROM rocky-base AS gts-gcc-source
COPY config/release.json ./config/release.json
COPY config/schemas/release.schema.json ./config/schemas/release.schema.json
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/fetch-release-source.py ./scripts/fetch-release-source.py
RUN mkdir -p /sources \
    && /usr/libexec/platform-python ./scripts/fetch-release-source.py gcc \
         --config ./config/release.json \
         --schema ./config/schemas/release.schema.json \
         --output /sources/gcc-toolset-15-gcc.src.rpm

FROM rocky-base AS gts-binutils-source
COPY config/release.json ./config/release.json
COPY config/schemas/release.schema.json ./config/schemas/release.schema.json
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/fetch-release-source.py ./scripts/fetch-release-source.py
RUN mkdir -p /sources \
    && /usr/libexec/platform-python ./scripts/fetch-release-source.py binutils \
         --config ./config/release.json \
         --schema ./config/schemas/release.schema.json \
         --output /sources/gcc-toolset-15-binutils.src.rpm

# Shared code and configuration for independently cached RPM bundles. Locks and
# transaction manifests stay in their component branches below.
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
FROM rpm-input-base AS sysroot-rpms-x86_64
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY locks/transactions/sysroot-el8-x86_64.json ./locks/transactions/sysroot-el8-x86_64.json
COPY locks/metadata/sysroot-el8-x86_64/ ./locks/metadata/sysroot-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/sysroot-el8-x86_64.json --require-lock \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-x86_64.json fetch --output /rpm-bundle --jobs 8

FROM rpm-input-base AS sysroot-x86_64
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY locks/transactions/sysroot-el8-x86_64.json ./locks/transactions/sysroot-el8-x86_64.json
COPY locks/metadata/sysroot-el8-x86_64/ ./locks/metadata/sysroot-el8-x86_64/
COPY --from=sysroot-rpms-x86_64 /rpm-bundle/ /rpm-bundle/
RUN --network=none /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-x86_64.json install \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --destination /opt/crossforge/sysroots/el8/x86_64

# The common host transaction exactly reproduces the previously qualified prep
# environment. RPM scripts/triggers execute, but the package set is immutable
# and installation runs without repositories or network access.
FROM rpm-input-base AS host-build-common-rpms
COPY locks/host-build-common-el8-x86_64.json ./locks/host-build-common-el8-x86_64.json
COPY locks/transactions/host-build-common-el8-x86_64.json ./locks/transactions/host-build-common-el8-x86_64.json
COPY locks/metadata/host-build-common-el8-x86_64/ ./locks/metadata/host-build-common-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/host-build-common-el8-x86_64.json --require-lock \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/host-build-common-el8-x86_64.json fetch \
      --output /rpm-bundle --jobs 8

FROM rpm-input-base AS host-build-common-locked
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
      --marker /usr/share/crossforge/rpm-locks/host-build-common.json
ENV PATH="/opt/rh/gcc-toolset-15/root/usr/bin:${PATH}"

# GCC-only prerequisites are a separate additive lock. Keeping them out of the
# common layer prevents libzstd-devel from changing binutils auto-detection.
FROM rpm-input-base AS host-gcc-build-rpms
COPY locks/host-build-common-el8-x86_64.json ./locks/host-build-common-el8-x86_64.json
COPY locks/transactions/host-build-common-el8-x86_64.json ./locks/transactions/host-build-common-el8-x86_64.json
COPY locks/host-gcc-build-el8-x86_64.json ./locks/host-gcc-build-el8-x86_64.json
COPY locks/transactions/host-gcc-build-el8-x86_64.json ./locks/transactions/host-gcc-build-el8-x86_64.json
COPY locks/metadata/host-gcc-build-el8-x86_64/ ./locks/metadata/host-gcc-build-el8-x86_64/
RUN /usr/libexec/platform-python ./scripts/validate-rpm-lock.py \
      ./locks/host-gcc-build-el8-x86_64.json --require-lock \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/host-gcc-build-el8-x86_64.json fetch \
      --output /rpm-bundle --jobs 4

FROM host-build-common-locked AS host-gcc-build-locked
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
      --marker /usr/share/crossforge/rpm-locks/host-gcc-build.json

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

FROM host-build-common-locked AS binutils-prepared
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
      "$ROCKY_RPM_TRUST_FINGERPRINT"

FROM host-build-common-locked AS gcc-prepared
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
      "$ROCKY_RPM_TRUST_FINGERPRINT"

FROM binutils-prepared AS binutils-x86_64
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
COPY --from=gcc-prepared /work/prepared/gcc/ /work/prepared/gcc/
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

# Real compiler smoke gate. This target remains non-publishable until the
# second target, frozen ABI sets, full tests and release supply chain exist.
FROM host-build-common-locked AS toolchain-x86_64-qualify-build
COPY --from=config-validate /src/config/release.json /src/config/release.json
COPY --from=sysroot-x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=binutils-x86_64 /work/binutils-stage/ /
COPY --from=gcc-x86_64 /work/gcc-stage/ /
COPY scripts/qualify-toolchain.py /work/scripts/qualify-toolchain.py
COPY tests/smoke/ /work/tests/smoke/
RUN --network=none /usr/libexec/platform-python /work/scripts/qualify-toolchain.py \
      --prefix /opt/crossforge/targets/x86_64-unknown-linux-gnu \
      --sysroot /opt/crossforge/sysroots/el8/x86_64 \
      --target x86_64-unknown-linux-gnu \
      --work /work/qualification/x86_64 \
      --report /work/qualification/x86_64.json \
      --release /src/config/release.json

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
