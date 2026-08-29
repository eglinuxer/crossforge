# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Canonical Docker-first build graph for the rewrite.
#
# Phase 1 stops at configuration validation and filesystem layout. Phase 2
# adds a locked x86_64 sysroot and an explicitly non-publishable development
# compiler slice. The latter remains a dev target until host RPM inputs are
# locked and its full qualification gate passes.

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

# Fetch all RPM payloads named by the resolved lock, then assemble them with
# network access disabled. materialize-sysroot.py verifies SHA256, size,
# signature, header NEVRA, the exact locked package set and required ABI files.
FROM rocky-base AS sysroot-rpms-x86_64
COPY config/release.json ./config/release.json
COPY config/schemas/release.schema.json ./config/schemas/release.schema.json
COPY config/schemas/sysroot-lock.schema.json ./config/schemas/sysroot-lock.schema.json
COPY keys/RPM-GPG-KEY-rockyofficial ./keys/RPM-GPG-KEY-rockyofficial
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/validate-sysroot-lock.py ./scripts/validate-sysroot-lock.py
COPY scripts/materialize-sysroot.py ./scripts/materialize-sysroot.py
RUN /usr/libexec/platform-python ./scripts/validate-sysroot-lock.py \
      ./locks/sysroot-el8-x86_64.json --require-lock \
    && /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-x86_64.json fetch --output /rpm-bundle --jobs 8

FROM rocky-base AS sysroot-x86_64
COPY config/release.json ./config/release.json
COPY config/schemas/release.schema.json ./config/schemas/release.schema.json
COPY config/schemas/sysroot-lock.schema.json ./config/schemas/sysroot-lock.schema.json
COPY keys/RPM-GPG-KEY-rockyofficial ./keys/RPM-GPG-KEY-rockyofficial
COPY locks/sysroot-el8-x86_64.json ./locks/sysroot-el8-x86_64.json
COPY scripts/validate-release.py ./scripts/validate-release.py
COPY scripts/validate-sysroot-lock.py ./scripts/validate-sysroot-lock.py
COPY scripts/materialize-sysroot.py ./scripts/materialize-sysroot.py
COPY --from=sysroot-rpms-x86_64 /rpm-bundle/ /rpm-bundle/
RUN --network=none /usr/libexec/platform-python ./scripts/materialize-sysroot.py \
      ./locks/sysroot-el8-x86_64.json install \
      --bundle /rpm-bundle \
      --key ./keys/RPM-GPG-KEY-rockyofficial \
      --destination /opt/crossforge/sysroots/el8/x86_64

# This stage intentionally uses live Rocky repositories for host build
# requirements. It is suitable for the development slice only and is the
# remaining reproducibility gap before this graph can produce a candidate.
FROM rocky-base AS gts-prep-tools-unlocked
RUN dnf -y --setopt=install_weak_deps=False install \
         bzip2 diffutils file findutils \
         gcc-toolset-15-binutils gcc-toolset-15-gcc gcc-toolset-15-gcc-c++ \
         glibc-devel gmp-devel gzip libmpc-devel make mpfr-devel \
         patch perl-interpreter redhat-rpm-config rpm-build scl-utils-build sed tar \
         which xz zlib-devel \
    && dnf clean all
ENV PATH="/opt/rh/gcc-toolset-15/root/usr/bin:${PATH}"

FROM gts-prep-tools-unlocked AS binutils-prepared
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

FROM gts-prep-tools-unlocked AS gcc-prepared
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

FROM gts-prep-tools-unlocked AS gcc-build-tools-unlocked
RUN dnf -y --enablerepo=powertools --setopt=install_weak_deps=False install \
      bison flex libzstd-devel \
    && dnf clean all

FROM gcc-build-tools-unlocked AS gcc-x86_64-built
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

# Real compiler smoke gate. This target is cache-only and deliberately carries
# a dev suffix while its host package closure is still unlocked.
FROM gts-prep-tools-unlocked AS toolchain-x86_64-qualify-build
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
FROM gts-prep-tools-unlocked AS toolchain-x86_64-dev
COPY --from=sysroot-x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=binutils-x86_64 /work/binutils-stage/ /
COPY --from=gcc-x86_64 /work/gcc-stage/ /
COPY --from=runtime-smoke-x86_64 \
  /work/qualification/ /work/qualification/
