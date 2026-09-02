# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

FROM crossforge_rocky_amd64 AS zstd-source
ARG ZSTD_VERSION
ARG ZSTD_SOURCE_COMPONENT_SHA256
COPY config/generated/components/sources/zstd.json /work/config/sources-zstd.json
COPY keys/ZSTD-RELEASE-KEY.asc /work/keys/ZSTD-RELEASE-KEY.asc
COPY evidence/gpg/zstd-1.5.7.tar.gz.sig.b64 /work/evidence/gpg/zstd-1.5.7.tar.gz.sig.b64
COPY evidence/git/zstd-v1.5.7.tag.b64 /work/evidence/git/zstd-v1.5.7.tag.b64
COPY evidence/git/zstd-v1.5.7.commit.b64 /work/evidence/git/zstd-v1.5.7.commit.b64
COPY --chmod=0755 scripts/release_component.py scripts/fetch-release-source.py \
  scripts/prepare-zstd-source.py /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/fetch-release-source.py zstd \
      --version "$ZSTD_VERSION" \
      --component-file /work/config/sources-zstd.json \
      --expected-component sources/zstd \
      --expected-scope build \
      --expected-sha256 "$ZSTD_SOURCE_COMPONENT_SHA256" \
      --output /work/source/zstd.tar.gz \
    && base64 --decode /work/evidence/gpg/zstd-1.5.7.tar.gz.sig.b64 \
      > /work/source/zstd.tar.gz.sig \
    && /usr/libexec/platform-python /work/scripts/prepare-zstd-source.py \
      --component /work/config/sources-zstd.json \
      --component-sha256 "$ZSTD_SOURCE_COMPONENT_SHA256" \
      --archive /work/source/zstd.tar.gz \
      --signature /work/source/zstd.tar.gz.sig \
      --destination /out/zstd \
      --manifest /out/source-manifest.json \
      --repository /work \
    && install -D -m 0644 /work/source/zstd.tar.gz \
      /out/materials/zstd.tar.gz \
    && install -D -m 0644 /work/source/zstd.tar.gz.sig \
      /out/materials/zstd.tar.gz.sig

FROM crossforge_host_common AS zstd-host-build
ARG ZSTD_VERSION
ARG ZSTD_SOURCE_COMPONENT_SHA256
ARG ZSTD_BUILD_POLICY_COMPONENT_SHA256
ARG ZSTD_BUILD_COMPONENT_SHA256
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_zstd_source /out/ /work/prepared/
COPY config/generated/components/sources/zstd.json /work/config/sources-zstd.json
COPY config/generated/components/implementation/zstd-build-policy.json /work/config/zstd-policy.json
COPY config/generated/components/zstd/host-build.json /work/config/zstd-build.json
COPY --chmod=0755 scripts/release_component.py scripts/prepare-zstd-source.py \
  scripts/build-zstd.sh /work/scripts/
RUN --network=none /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/sources-zstd.json --expected-component sources/zstd \
      --expected-scope build --expected-sha256 "$ZSTD_SOURCE_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/zstd-build.json --expected-component zstd/host-build \
      --expected-scope build --expected-sha256 "$ZSTD_BUILD_COMPONENT_SHA256" \
    && /work/scripts/build-zstd.sh \
      /work/prepared/zstd /work/build/zstd \
      "/opt/crossforge/deps/zstd/$ZSTD_VERSION/host" \
      /work/prepared/source-manifest.json \
      /work/prepared/materials/zstd.tar.gz \
      /work/prepared/materials/zstd.tar.gz.sig \
      "/opt/crossforge/deps/zstd/$ZSTD_VERSION/host/build-manifest.json" \
      /work/config/sources-zstd.json "$ZSTD_SOURCE_COMPONENT_SHA256" \
      /work/config/zstd-policy.json "$ZSTD_BUILD_POLICY_COMPONENT_SHA256" \
      zstd/host-build "$ZSTD_BUILD_COMPONENT_SHA256" \
      host /opt/rh/gcc-toolset-15/root/usr/bin - "$CROSSFORGE_JOBS"

FROM scratch AS zstd-host-build-export
ARG ZSTD_VERSION
COPY --from=zstd-host-build /opt/crossforge/deps/zstd/${ZSTD_VERSION}/host/ \
  /opt/crossforge/deps/zstd/${ZSTD_VERSION}/host/

FROM crossforge_host_common AS zstd-target-build
ARG ZSTD_VERSION
ARG ZSTD_TARGET_ARCH
ARG ZSTD_TARGET_TRIPLE
ARG ZSTD_SOURCE_COMPONENT_SHA256
ARG ZSTD_BUILD_POLICY_COMPONENT_SHA256
ARG ZSTD_BUILD_COMPONENT
ARG ZSTD_BUILD_COMPONENT_SHA256
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_zstd_source /out/ /work/prepared/
COPY --from=crossforge_toolchain /opt/crossforge/ /opt/crossforge/
COPY config/generated/components/sources/zstd.json /work/config/sources-zstd.json
COPY config/generated/components/implementation/zstd-build-policy.json /work/config/zstd-policy.json
COPY config/generated/components/zstd/${ZSTD_TARGET_ARCH}-build.json /work/config/zstd-build.json
COPY --chmod=0755 scripts/release_component.py scripts/prepare-zstd-source.py \
  scripts/build-zstd.sh /work/scripts/
RUN --network=none case "$ZSTD_TARGET_ARCH:$ZSTD_TARGET_TRIPLE:$ZSTD_BUILD_COMPONENT" in \
      x86_64:x86_64-unknown-linux-gnu:zstd/x86_64-build|aarch64:aarch64-unknown-linux-gnu:zstd/aarch64-build) ;; \
      *) echo 'error: invalid zstd target identity' >&2; exit 1 ;; \
    esac \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/sources-zstd.json --expected-component sources/zstd \
      --expected-scope build --expected-sha256 "$ZSTD_SOURCE_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/release_component.py validate \
      /work/config/zstd-build.json --expected-component "$ZSTD_BUILD_COMPONENT" \
      --expected-scope build --expected-sha256 "$ZSTD_BUILD_COMPONENT_SHA256" \
    && /work/scripts/build-zstd.sh \
      /work/prepared/zstd /work/build/zstd \
      "/opt/crossforge/deps/zstd/$ZSTD_VERSION/$ZSTD_TARGET_TRIPLE" \
      /work/prepared/source-manifest.json \
      /work/prepared/materials/zstd.tar.gz \
      /work/prepared/materials/zstd.tar.gz.sig \
      "/opt/crossforge/deps/zstd/$ZSTD_VERSION/$ZSTD_TARGET_TRIPLE/build-manifest.json" \
      /work/config/sources-zstd.json "$ZSTD_SOURCE_COMPONENT_SHA256" \
      /work/config/zstd-policy.json "$ZSTD_BUILD_POLICY_COMPONENT_SHA256" \
      "$ZSTD_BUILD_COMPONENT" "$ZSTD_BUILD_COMPONENT_SHA256" \
      "$ZSTD_TARGET_TRIPLE" "/opt/crossforge/targets/$ZSTD_TARGET_TRIPLE/bin" \
      "/opt/crossforge/sysroots/el8/$ZSTD_TARGET_ARCH" "$CROSSFORGE_JOBS"

FROM scratch AS zstd-target-build-export
ARG ZSTD_VERSION
ARG ZSTD_TARGET_TRIPLE
COPY --from=zstd-target-build \
  /opt/crossforge/deps/zstd/${ZSTD_VERSION}/${ZSTD_TARGET_TRIPLE}/ \
  /opt/crossforge/deps/zstd/${ZSTD_VERSION}/${ZSTD_TARGET_TRIPLE}/
