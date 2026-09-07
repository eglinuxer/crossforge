# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

FROM crossforge_rocky_amd64 AS qt-fetch
ARG QT_VERSION
ARG QT_SOURCE_URL
RUN test "$QT_VERSION" = 6.8.4 \
    && test "$QT_SOURCE_URL" = \
      https://download.qt.io/archive/qt/6.8/6.8.4/single/qt-everywhere-opensource-src-6.8.4.tar.xz \
    && mkdir -p /work/source \
    && curl --fail --location --retry 3 --retry-delay 2 \
      "$QT_SOURCE_URL" \
      --output /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz

FROM crossforge_rocky_amd64 AS qt-source
ARG QT_VERSION
ARG CROSSFORGE_COMPONENT_SOURCES_QT_SHA256
COPY --from=qt-fetch \
  /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz \
  /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz
COPY config/generated/components/sources/qt.json \
  /work/config/sources-qt.json
COPY config/schemas/qt-source-manifest.schema.json \
  /work/config/schemas/qt-source-manifest.schema.json
COPY evidence/checksums/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256.b64 \
  /work/evidence/checksums/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256.b64
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/prepare-qt-source.py /work/scripts/
RUN --network=none test "$QT_VERSION" = 6.8.4 \
    && /usr/libexec/platform-python /work/scripts/prepare-qt-source.py \
      --component /work/config/sources-qt.json \
      --component-sha256 "$CROSSFORGE_COMPONENT_SOURCES_QT_SHA256" \
      --archive /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz \
      --checksum-evidence \
        /work/evidence/checksums/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256.b64 \
      --output /out/source-manifest.json \
    && install -D -m 0644 \
      /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz \
      /out/materials/qt-everywhere-opensource-src-6.8.4.tar.xz \
    && base64 --decode \
      /work/evidence/checksums/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256.b64 \
      > /out/materials/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256

FROM scratch AS qt-source-export
COPY --from=qt-source /out/ /

FROM crossforge_rocky_amd64 AS ffmpeg-fetch
ARG FFMPEG_VERSION
ARG FFMPEG_SOURCE_URL
RUN test "$FFMPEG_VERSION" = 7.1.1 \
    && test "$FFMPEG_SOURCE_URL" = \
      https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz \
    && mkdir -p /work/source \
    && curl --fail --location --retry 3 --retry-delay 2 \
      "$FFMPEG_SOURCE_URL" \
      --output /work/source/ffmpeg-7.1.1.tar.xz

FROM crossforge_rocky_amd64 AS ffmpeg-source
ARG FFMPEG_VERSION
ARG CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256
COPY --from=ffmpeg-fetch /work/source/ffmpeg-7.1.1.tar.xz \
  /work/source/ffmpeg-7.1.1.tar.xz
COPY config/generated/components/sources/ffmpeg.json \
  /work/config/sources-ffmpeg.json
COPY config/schemas/ffmpeg-source-manifest.schema.json \
  /work/config/schemas/ffmpeg-source-manifest.schema.json
COPY keys/FFMPEG-RELEASE-KEY.asc /work/keys/FFMPEG-RELEASE-KEY.asc
COPY evidence/gpg/ffmpeg-7.1.1.tar.xz.asc.b64 \
  /work/evidence/gpg/ffmpeg-7.1.1.tar.xz.asc.b64
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/prepare-ffmpeg-source.py /work/scripts/
RUN --network=none test "$FFMPEG_VERSION" = 7.1.1 \
    && base64 --decode \
      /work/evidence/gpg/ffmpeg-7.1.1.tar.xz.asc.b64 \
      > /work/source/ffmpeg-7.1.1.tar.xz.asc \
    && /usr/libexec/platform-python /work/scripts/prepare-ffmpeg-source.py \
      --component /work/config/sources-ffmpeg.json \
      --component-sha256 "$CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256" \
      --archive /work/source/ffmpeg-7.1.1.tar.xz \
      --signature /work/source/ffmpeg-7.1.1.tar.xz.asc \
      --signature-evidence \
        /work/evidence/gpg/ffmpeg-7.1.1.tar.xz.asc.b64 \
      --output /out/source-manifest.json \
    && install -D -m 0644 /work/source/ffmpeg-7.1.1.tar.xz \
      /out/materials/ffmpeg-7.1.1.tar.xz \
    && install -D -m 0644 /work/source/ffmpeg-7.1.1.tar.xz.asc \
      /out/materials/ffmpeg-7.1.1.tar.xz.asc

FROM scratch AS ffmpeg-source-export
COPY --from=ffmpeg-source /out/ /

FROM crossforge_rocky_amd64 AS xcb-util-cursor-fetch
ARG XCB_UTIL_CURSOR_VERSION
ARG XCB_UTIL_CURSOR_SOURCE_URL
RUN test "$XCB_UTIL_CURSOR_VERSION" = 0.1.6 \
    && test "$XCB_UTIL_CURSOR_SOURCE_URL" = \
      https://xorg.freedesktop.org/archive/individual/lib/xcb-util-cursor-0.1.6.tar.xz \
    && mkdir -p /work/source \
    && curl --fail --location --retry 3 --retry-delay 2 \
      "$XCB_UTIL_CURSOR_SOURCE_URL" \
      --output /work/source/xcb-util-cursor-0.1.6.tar.xz

FROM crossforge_rocky_amd64 AS xcb-util-cursor-source
ARG XCB_UTIL_CURSOR_VERSION
ARG CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256
COPY --from=xcb-util-cursor-fetch \
  /work/source/xcb-util-cursor-0.1.6.tar.xz \
  /work/source/xcb-util-cursor-0.1.6.tar.xz
COPY config/generated/components/sources/xcb-util-cursor.json \
  /work/config/sources-xcb-util-cursor.json
COPY config/schemas/xcb-util-cursor-source-manifest.schema.json \
  /work/config/schemas/xcb-util-cursor-source-manifest.schema.json
COPY keys/XCB-UTIL-CURSOR-RELEASE-KEY.asc \
  /work/keys/XCB-UTIL-CURSOR-RELEASE-KEY.asc
COPY evidence/gpg/xcb-util-cursor-0.1.6.tar.xz.sig.b64 \
  /work/evidence/gpg/xcb-util-cursor-0.1.6.tar.xz.sig.b64
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/prepare-xcb-util-cursor-source.py /work/scripts/
RUN --network=none test "$XCB_UTIL_CURSOR_VERSION" = 0.1.6 \
    && base64 --decode \
      /work/evidence/gpg/xcb-util-cursor-0.1.6.tar.xz.sig.b64 \
      > /work/source/xcb-util-cursor-0.1.6.tar.xz.sig \
    && /usr/libexec/platform-python \
      /work/scripts/prepare-xcb-util-cursor-source.py \
      --component /work/config/sources-xcb-util-cursor.json \
      --component-sha256 \
        "$CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256" \
      --archive /work/source/xcb-util-cursor-0.1.6.tar.xz \
      --signature /work/source/xcb-util-cursor-0.1.6.tar.xz.sig \
      --signature-evidence \
        /work/evidence/gpg/xcb-util-cursor-0.1.6.tar.xz.sig.b64 \
      --output /out/source-manifest.json \
    && install -D -m 0644 \
      /work/source/xcb-util-cursor-0.1.6.tar.xz \
      /out/materials/xcb-util-cursor-0.1.6.tar.xz \
    && install -D -m 0644 \
      /work/source/xcb-util-cursor-0.1.6.tar.xz.sig \
      /out/materials/xcb-util-cursor-0.1.6.tar.xz.sig

FROM scratch AS xcb-util-cursor-source-export
COPY --from=xcb-util-cursor-source /out/ /

FROM crossforge_host_qt AS xcb-util-cursor-host-build
ARG CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_xcb_util_cursor_source / \
  /work/prepared/xcb-util-cursor/
COPY config/generated/components/sources/xcb-util-cursor.json \
  /work/config/sources-xcb-util-cursor.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/qt-qualification-plan.schema.json \
  config/schemas/rpm-lock.schema.json \
  config/schemas/rpm-transaction.schema.json \
  config/schemas/xcb-util-cursor-build.schema.json \
  config/schemas/xcb-util-cursor-source-manifest.schema.json \
  /work/config/schemas/
COPY locks/host-qt-build-el8-x86_64.json \
  /work/locks/host-qt-build-el8-x86_64.json
COPY locks/transactions/host-qt-build-el8-x86_64.json \
  /work/locks/transactions/host-qt-build-el8-x86_64.json
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/build-xcb-util-cursor.py /work/scripts/
RUN --network=none /work/scripts/build-xcb-util-cursor.py \
      --identity host \
      --source-archive \
        /work/prepared/xcb-util-cursor/materials/xcb-util-cursor-0.1.6.tar.xz \
      --source-manifest \
        /work/prepared/xcb-util-cursor/source-manifest.json \
      --source-component /work/config/sources-xcb-util-cursor.json \
      --source-component-sha256 \
        "$CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256" \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --plan /work/config/qt-qualification.json \
      --rpm-lock /work/locks/host-qt-build-el8-x86_64.json \
      --rpm-transaction \
        /work/locks/transactions/host-qt-build-el8-x86_64.json \
      --toolchain /opt/rh/gcc-toolset-15/root/usr/bin \
      --prefix \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor \
      --build-root /work/build/xcb-util-cursor-host \
      --jobs "$CROSSFORGE_JOBS" \
      --output \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor-build.json

FROM crossforge_host_qt AS xcb-util-cursor-target-build
ARG XCB_UTIL_CURSOR_TARGET_ARCH
ARG XCB_UTIL_CURSOR_TARGET_TRIPLE
ARG XCB_UTIL_CURSOR_RPM_LOCK
ARG XCB_UTIL_CURSOR_RPM_TRANSACTION
ARG CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_xcb_util_cursor_source / \
  /work/prepared/xcb-util-cursor/
COPY --from=crossforge_toolchain \
  /opt/crossforge/targets/${XCB_UTIL_CURSOR_TARGET_TRIPLE}/ \
  /opt/crossforge/targets/${XCB_UTIL_CURSOR_TARGET_TRIPLE}/
COPY --from=crossforge_qt_target \
  /opt/crossforge/sysroots/el8/${XCB_UTIL_CURSOR_TARGET_ARCH}/ \
  /opt/crossforge/sysroots/el8/${XCB_UTIL_CURSOR_TARGET_ARCH}/
COPY config/generated/components/sources/xcb-util-cursor.json \
  /work/config/sources-xcb-util-cursor.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/qt-qualification-plan.schema.json \
  config/schemas/rpm-lock.schema.json \
  config/schemas/rpm-transaction.schema.json \
  config/schemas/xcb-util-cursor-build.schema.json \
  config/schemas/xcb-util-cursor-source-manifest.schema.json \
  /work/config/schemas/
COPY --from=crossforge_qt_target /src/locks/ /work/locks/
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/build-xcb-util-cursor.py /work/scripts/
RUN --network=none case \
      "$XCB_UTIL_CURSOR_TARGET_ARCH:$XCB_UTIL_CURSOR_TARGET_TRIPLE:$XCB_UTIL_CURSOR_RPM_LOCK:$XCB_UTIL_CURSOR_RPM_TRANSACTION" in \
      x86_64:x86_64-unknown-linux-gnu:locks/qt-target-el8-x86_64.json:locks/transactions/qt-target-el8-x86_64.json|aarch64:aarch64-unknown-linux-gnu:locks/qt-target-el8-aarch64.json:locks/transactions/qt-target-el8-aarch64.json) ;; \
      *) echo 'error: invalid xcb-util-cursor target identity' >&2; exit 1 ;; \
    esac \
    && /work/scripts/build-xcb-util-cursor.py \
      --identity "$XCB_UTIL_CURSOR_TARGET_TRIPLE" \
      --source-archive \
        /work/prepared/xcb-util-cursor/materials/xcb-util-cursor-0.1.6.tar.xz \
      --source-manifest \
        /work/prepared/xcb-util-cursor/source-manifest.json \
      --source-component /work/config/sources-xcb-util-cursor.json \
      --source-component-sha256 \
        "$CROSSFORGE_COMPONENT_SOURCES_XCB_UTIL_CURSOR_SHA256" \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --plan /work/config/qt-qualification.json \
      --rpm-lock "/work/$XCB_UTIL_CURSOR_RPM_LOCK" \
      --rpm-transaction "/work/$XCB_UTIL_CURSOR_RPM_TRANSACTION" \
      --toolchain \
        "/opt/crossforge/targets/$XCB_UTIL_CURSOR_TARGET_TRIPLE/bin" \
      --sysroot "/opt/crossforge/sysroots/el8/$XCB_UTIL_CURSOR_TARGET_ARCH" \
      --prefix /usr \
      --build-root \
        "/work/build/xcb-util-cursor-$XCB_UTIL_CURSOR_TARGET_ARCH" \
      --jobs "$CROSSFORGE_JOBS" \
      --output \
        "/opt/crossforge/qualification/qt/6.8.4/deps/$XCB_UTIL_CURSOR_TARGET_TRIPLE/xcb-util-cursor-build.json"

FROM crossforge_xcb_host AS ffmpeg-host-build
ARG FFMPEG_VERSION
ARG CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_ffmpeg_source / \
  /work/prepared/ffmpeg/
COPY config/generated/components/sources/ffmpeg.json \
  /work/config/sources-ffmpeg.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/ffmpeg-build.schema.json \
  config/schemas/ffmpeg-source-manifest.schema.json \
  config/schemas/qt-qualification-plan.schema.json \
  config/schemas/rpm-lock.schema.json \
  config/schemas/rpm-transaction.schema.json \
  /work/config/schemas/
COPY locks/host-qt-build-el8-x86_64.json \
  /work/locks/host-qt-build-el8-x86_64.json
COPY locks/transactions/host-qt-build-el8-x86_64.json \
  /work/locks/transactions/host-qt-build-el8-x86_64.json
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/build-ffmpeg.sh scripts/qualify-ffmpeg-build.py /work/scripts/
RUN --network=none test "$FFMPEG_VERSION" = 7.1.1 \
    && /work/scripts/build-ffmpeg.sh \
      host \
      /work/prepared/ffmpeg/materials/ffmpeg-7.1.1.tar.xz \
      /work/build/ffmpeg-host \
      /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg \
      /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg \
      '' \
      /opt/rh/gcc-toolset-15/root/usr/bin \
      "$CROSSFORGE_JOBS" \
    && /work/scripts/qualify-ffmpeg-build.py \
      --identity host \
      --source-archive \
        /work/prepared/ffmpeg/materials/ffmpeg-7.1.1.tar.xz \
      --source-manifest /work/prepared/ffmpeg/source-manifest.json \
      --source-component /work/config/sources-ffmpeg.json \
      --source-component-sha256 \
        "$CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256" \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --plan /work/config/qt-qualification.json \
      --rpm-lock /work/locks/host-qt-build-el8-x86_64.json \
      --rpm-transaction \
        /work/locks/transactions/host-qt-build-el8-x86_64.json \
      --toolchain /opt/rh/gcc-toolset-15/root/usr/bin \
      --install-root \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg \
      --prefix /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg \
      --build-root /work/build/ffmpeg-host \
      --builder /work/scripts/build-ffmpeg.sh \
      --output \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg/ffmpeg-build.json

FROM crossforge_host_qt AS ffmpeg-target-build
ARG FFMPEG_VERSION
ARG FFMPEG_TARGET_ARCH
ARG FFMPEG_TARGET_TRIPLE
ARG FFMPEG_RPM_LOCK
ARG FFMPEG_RPM_TRANSACTION
ARG CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_ffmpeg_source / \
  /work/prepared/ffmpeg/
COPY --from=crossforge_toolchain \
  /opt/crossforge/targets/${FFMPEG_TARGET_TRIPLE}/ \
  /opt/crossforge/targets/${FFMPEG_TARGET_TRIPLE}/
COPY --from=crossforge_qt_target \
  /opt/crossforge/sysroots/el8/${FFMPEG_TARGET_ARCH}/ \
  /opt/crossforge/sysroots/el8/${FFMPEG_TARGET_ARCH}/
COPY config/generated/components/sources/ffmpeg.json \
  /work/config/sources-ffmpeg.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/ffmpeg-build.schema.json \
  config/schemas/ffmpeg-source-manifest.schema.json \
  config/schemas/qt-qualification-plan.schema.json \
  config/schemas/rpm-lock.schema.json \
  config/schemas/rpm-transaction.schema.json \
  /work/config/schemas/
COPY --from=crossforge_qt_target /src/locks/ /work/locks/
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/build-ffmpeg.sh scripts/qualify-ffmpeg-build.py /work/scripts/
RUN --network=none case \
      "$FFMPEG_TARGET_ARCH:$FFMPEG_TARGET_TRIPLE:$FFMPEG_RPM_LOCK:$FFMPEG_RPM_TRANSACTION" in \
      x86_64:x86_64-unknown-linux-gnu:locks/qt-target-el8-x86_64.json:locks/transactions/qt-target-el8-x86_64.json|aarch64:aarch64-unknown-linux-gnu:locks/qt-target-el8-aarch64.json:locks/transactions/qt-target-el8-aarch64.json) ;; \
      *) echo 'error: invalid FFmpeg target identity' >&2; exit 1 ;; \
    esac \
    && /work/scripts/build-ffmpeg.sh \
      "$FFMPEG_TARGET_TRIPLE" \
      /work/prepared/ffmpeg/materials/ffmpeg-7.1.1.tar.xz \
      "/work/build/ffmpeg-$FFMPEG_TARGET_ARCH" \
      "/opt/crossforge/qualification/qt/6.8.4/deps/$FFMPEG_TARGET_TRIPLE/ffmpeg" \
      /usr \
      "/opt/crossforge/sysroots/el8/$FFMPEG_TARGET_ARCH" \
      "/opt/crossforge/targets/$FFMPEG_TARGET_TRIPLE/bin" \
      "$CROSSFORGE_JOBS" \
    && /work/scripts/qualify-ffmpeg-build.py \
      --identity "$FFMPEG_TARGET_TRIPLE" \
      --source-archive \
        /work/prepared/ffmpeg/materials/ffmpeg-7.1.1.tar.xz \
      --source-manifest /work/prepared/ffmpeg/source-manifest.json \
      --source-component /work/config/sources-ffmpeg.json \
      --source-component-sha256 \
        "$CROSSFORGE_COMPONENT_SOURCES_FFMPEG_SHA256" \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --plan /work/config/qt-qualification.json \
      --rpm-lock "/work/$FFMPEG_RPM_LOCK" \
      --rpm-transaction "/work/$FFMPEG_RPM_TRANSACTION" \
      --toolchain "/opt/crossforge/targets/$FFMPEG_TARGET_TRIPLE/bin" \
      --install-root \
        "/opt/crossforge/qualification/qt/6.8.4/deps/$FFMPEG_TARGET_TRIPLE/ffmpeg/usr" \
      --prefix /usr \
      --build-root "/work/build/ffmpeg-$FFMPEG_TARGET_ARCH" \
      --builder /work/scripts/build-ffmpeg.sh \
      --output \
        "/opt/crossforge/qualification/qt/6.8.4/deps/$FFMPEG_TARGET_TRIPLE/ffmpeg/ffmpeg-build.json"

FROM scratch AS ffmpeg-host-build-observation
COPY --from=ffmpeg-host-build /work/build/ffmpeg-host/source/config.h /
COPY --from=ffmpeg-host-build /work/build/ffmpeg-host/source/ffbuild/config.mak /
COPY --from=ffmpeg-host-build \
  /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg/ /ffmpeg/

FROM crossforge_ffmpeg_host AS qt-host-configure
ARG QT_VERSION
COPY --from=crossforge_qt_source \
  /materials/qt-everywhere-opensource-src-6.8.4.tar.xz \
  /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz
COPY --from=crossforge_qt_source /source-manifest.json \
  /work/prepared/qt/source-manifest.json
COPY --from=crossforge_cmake \
  /opt/crossforge/host-tools/cmake/4.4.0/ \
  /opt/crossforge/host-tools/cmake/4.4.0/
COPY --from=crossforge_ninja \
  /opt/crossforge/host-tools/ninja/1.13.2/ \
  /opt/crossforge/host-tools/ninja/1.13.2/
COPY --chmod=0755 scripts/configure-qt-host.sh /work/scripts/configure-qt-host.sh
RUN --network=none test "$QT_VERSION" = 6.8.4 \
    && /work/scripts/configure-qt-host.sh \
      /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz \
      /work/source/qt-everywhere-src-6.8.4 \
      /work/build/qt-host \
      /opt/crossforge/qualification/qt/6.8.4/host \
      /opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor \
      /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg

FROM qt-host-configure AS qt-host-configure-qualified
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/ffmpeg-build.schema.json \
  config/schemas/qt-host-configure.schema.json \
  config/schemas/qt-qualification-plan.schema.json \
  config/schemas/qt-source-manifest.schema.json \
  config/schemas/xcb-util-cursor-build.schema.json \
  /work/config/schemas/
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/qualify-qt-host-configure.py /work/scripts/
RUN --network=none /work/scripts/qualify-qt-host-configure.py \
      --cache /work/build/qt-host/CMakeCache.txt \
      --summary /work/build/qt-host/config.summary \
      --log /work/build/qt-host/configure.log \
      --plan /work/config/qt-qualification.json \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --qt-source-manifest /work/prepared/qt/source-manifest.json \
      --xcb-build-manifest \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor-build.json \
      --ffmpeg-build-manifest \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg/ffmpeg-build.json \
      --ffmpeg-prefix \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg \
      --cmake /opt/crossforge/host-tools/cmake/4.4.0/bin/cmake \
      --ninja /opt/crossforge/host-tools/ninja/1.13.2/bin/ninja \
      --cxx /opt/rh/gcc-toolset-15/root/usr/bin/g++ \
      --output /work/build/qt-host/qt-host-configure.json

FROM scratch AS qt-host-configure-evidence
COPY --from=qt-host-configure-qualified /work/build/qt-host/CMakeCache.txt /
COPY --from=qt-host-configure-qualified /work/build/qt-host/config.summary /
COPY --from=qt-host-configure-qualified /work/build/qt-host/configure.log /
COPY --from=qt-host-configure-qualified /work/build/qt-host/qt-host-configure.json /

FROM qt-host-configure AS qt-host-webengine-build
ARG CROSSFORGE_JOBS=4
COPY --chmod=0755 scripts/build-qt-host.sh \
  scripts/print-build-log-diagnostics.py scripts/run-with-heartbeat.py \
  /work/scripts/
RUN --network=none /work/scripts/build-qt-host.sh \
      /work/build/qt-host \
      /opt/crossforge/qualification/qt/6.8.4/host \
      "$CROSSFORGE_JOBS" webengine

FROM qt-host-webengine-build AS qt-host-build
ARG CROSSFORGE_JOBS=4
RUN --network=none /work/scripts/build-qt-host.sh \
      /work/build/qt-host \
      /opt/crossforge/qualification/qt/6.8.4/host \
      "$CROSSFORGE_JOBS" complete

FROM qt-host-build AS qt-host-install-checked
COPY --chmod=0755 scripts/check-qt-host-install.sh \
  /work/scripts/check-qt-host-install.sh
RUN --network=none /work/scripts/check-qt-host-install.sh \
      /opt/crossforge/qualification/qt/6.8.4/host

FROM qt-host-install-checked AS qt-host-qualified
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
COPY --from=qt-host-configure-qualified \
  /work/build/qt-host/qt-host-configure.json \
  /work/evidence/qt-host-configure.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/qt-host-build.schema.json \
  config/schemas/qt-host-configure.schema.json \
  config/schemas/qt-qualification-plan.schema.json \
  /work/config/schemas/
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/qualify-qt-host-build.py /work/scripts/
RUN --network=none /work/scripts/qualify-qt-host-build.py \
      --prefix /opt/crossforge/qualification/qt/6.8.4/host \
      --build-root /work/build/qt-host \
      --configure-evidence /work/evidence/qt-host-configure.json \
      --plan /work/config/qt-qualification.json \
      --qualification-component /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --ffmpeg-prefix \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/ffmpeg \
      --xcb-prefix \
        /opt/crossforge/qualification/qt/6.8.4/deps/host/xcb-util-cursor \
      --cmake /opt/crossforge/host-tools/cmake/4.4.0/bin/cmake \
      --ninja /opt/crossforge/host-tools/ninja/1.13.2/bin/ninja \
      --cxx /opt/rh/gcc-toolset-15/root/usr/bin/g++ \
      --toolchain /opt/rh/gcc-toolset-15/root/usr/bin \
      --builder /work/scripts/build-qt-host.sh \
      --install-checker /work/scripts/check-qt-host-install.sh \
      --diagnostics /work/scripts/print-build-log-diagnostics.py \
      --heartbeat /work/scripts/run-with-heartbeat.py \
      --output \
        /opt/crossforge/qualification/qt/6.8.4/host/qt-host-build.json

FROM scratch AS qt-host-qualification-evidence
COPY --from=qt-host-qualified /work/build/qt-host/webengine-build.log /
COPY --from=qt-host-qualified \
  /opt/crossforge/qualification/qt/6.8.4/host/qt-host-build.json /
COPY --from=qt-host-qualified /work/build/qt-host/build.log /
COPY --from=qt-host-qualified /work/build/qt-host/install.log /
COPY --from=qt-host-qualified /work/build/qt-host/install_manifest.txt /

FROM crossforge_host_qt AS qt-target-configure
ARG QT_VERSION
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
ARG QT_XNNPACK_PATCH_SHA256
COPY --from=crossforge_qt_source \
  /materials/qt-everywhere-opensource-src-6.8.4.tar.xz \
  /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz
COPY --from=crossforge_cmake \
  /opt/crossforge/host-tools/cmake/4.4.0/ \
  /opt/crossforge/host-tools/cmake/4.4.0/
COPY --from=crossforge_ninja \
  /opt/crossforge/host-tools/ninja/1.13.2/ \
  /opt/crossforge/host-tools/ninja/1.13.2/
COPY --from=crossforge_toolchain \
  /opt/crossforge/targets/${QT_TARGET_TRIPLE}/ \
  /opt/crossforge/targets/${QT_TARGET_TRIPLE}/
COPY --from=crossforge_xcb_target \
  /opt/crossforge/sysroots/el8/${QT_TARGET_ARCH}/ \
  /opt/crossforge/sysroots/el8/${QT_TARGET_ARCH}/
COPY --from=crossforge_ffmpeg_target \
  /opt/crossforge/qualification/qt/6.8.4/deps/${QT_TARGET_TRIPLE}/ffmpeg/usr/ \
  /opt/crossforge/sysroots/el8/${QT_TARGET_ARCH}/usr/
COPY --from=crossforge_qt_host \
  /opt/crossforge/qualification/qt/6.8.4/host/ \
  /opt/crossforge/qualification/qt/6.8.4/host/
COPY integration/cmake/ /work/integration/cmake/
COPY patches/qt/0001-xnnpack-use-uint16-neon-fp16-load.patch \
  /work/patches/qt/0001-xnnpack-use-uint16-neon-fp16-load.patch
COPY --chmod=0755 scripts/configure-qt-target.sh \
  /work/scripts/configure-qt-target.sh
RUN --network=none test "$QT_VERSION" = 6.8.4 \
    && /work/scripts/configure-qt-target.sh \
      /work/source/qt-everywhere-opensource-src-6.8.4.tar.xz \
      "/work/source/qt-everywhere-src-6.8.4-$QT_TARGET_ARCH" \
      "/work/build/qt-$QT_TARGET_ARCH" \
      "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE" \
      "$QT_TARGET_ARCH" \
      "$QT_TARGET_TRIPLE" \
      "/opt/crossforge/sysroots/el8/$QT_TARGET_ARCH" \
      /opt/crossforge/qualification/qt/6.8.4/host \
      /work/patches/qt/0001-xnnpack-use-uint16-neon-fp16-load.patch \
      "$QT_XNNPACK_PATCH_SHA256"

FROM scratch AS qt-target-configure-observation
ARG QT_TARGET_ARCH
COPY --from=qt-target-configure \
  /work/build/qt-${QT_TARGET_ARCH}/CMakeCache.txt /
COPY --from=qt-target-configure \
  /work/build/qt-${QT_TARGET_ARCH}/config.summary /
COPY --from=qt-target-configure \
  /work/build/qt-${QT_TARGET_ARCH}/configure.log /
COPY --from=qt-target-configure \
  /work/build/qt-${QT_TARGET_ARCH}/patch.log /

FROM qt-target-configure AS qt-target-configure-qualified
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
COPY --from=crossforge_qt_source /source-manifest.json \
  /work/prepared/qt/source-manifest.json
COPY --from=crossforge_xcb_target \
  /opt/crossforge/qualification/qt/6.8.4/deps/${QT_TARGET_TRIPLE}/xcb-util-cursor-build.json \
  /work/prepared/xcb-util-cursor-build.json
COPY --from=crossforge_ffmpeg_target \
  /opt/crossforge/qualification/qt/6.8.4/deps/${QT_TARGET_TRIPLE}/ffmpeg/ffmpeg-build.json \
  /work/prepared/ffmpeg-build.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/ffmpeg-build.schema.json \
  config/schemas/qt-qualification-plan.schema.json \
  config/schemas/qt-source-manifest.schema.json \
  config/schemas/qt-target-configure.schema.json \
  config/schemas/xcb-util-cursor-build.schema.json \
  /work/config/schemas/
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/qualify-qt-target-configure.py /work/scripts/
RUN --network=none /work/scripts/qualify-qt-target-configure.py \
      --arch "$QT_TARGET_ARCH" \
      --triple "$QT_TARGET_TRIPLE" \
      --install-root \
        "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE" \
      --sysroot "/opt/crossforge/sysroots/el8/$QT_TARGET_ARCH" \
      --qt-host /opt/crossforge/qualification/qt/6.8.4/host \
      --toolchain-file "/work/integration/cmake/$QT_TARGET_TRIPLE.cmake" \
      --cache "/work/build/qt-$QT_TARGET_ARCH/CMakeCache.txt" \
      --summary "/work/build/qt-$QT_TARGET_ARCH/config.summary" \
      --log "/work/build/qt-$QT_TARGET_ARCH/configure.log" \
      --patch-log "/work/build/qt-$QT_TARGET_ARCH/patch.log" \
      --patch \
        /work/patches/qt/0001-xnnpack-use-uint16-neon-fp16-load.patch \
      --plan /work/config/qt-qualification.json \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --qt-source-manifest /work/prepared/qt/source-manifest.json \
      --xcb-build-manifest /work/prepared/xcb-util-cursor-build.json \
      --ffmpeg-build-manifest /work/prepared/ffmpeg-build.json \
      --ffmpeg-prefix "/opt/crossforge/sysroots/el8/$QT_TARGET_ARCH/usr" \
      --cmake /opt/crossforge/host-tools/cmake/4.4.0/bin/cmake \
      --ninja /opt/crossforge/host-tools/ninja/1.13.2/bin/ninja \
      --cxx "/opt/crossforge/targets/$QT_TARGET_TRIPLE/bin/$QT_TARGET_TRIPLE-g++" \
      --output \
        "/work/build/qt-$QT_TARGET_ARCH/qt-target-configure.json"

FROM scratch AS qt-target-configure-evidence
ARG QT_TARGET_ARCH
COPY --from=qt-target-configure-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/CMakeCache.txt /
COPY --from=qt-target-configure-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/config.summary /
COPY --from=qt-target-configure-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/configure.log /
COPY --from=qt-target-configure-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/patch.log /
COPY --from=qt-target-configure-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/qt-target-configure.json /

FROM qt-target-configure AS qt-target-webengine-build
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
ARG CROSSFORGE_JOBS=4
COPY --chmod=0755 scripts/build-qt-target.sh \
  scripts/print-build-log-diagnostics.py scripts/run-with-heartbeat.py \
  /work/scripts/
RUN --network=none /work/scripts/build-qt-target.sh \
      "/work/build/qt-$QT_TARGET_ARCH" \
      "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE" \
      "$QT_TARGET_ARCH" \
      "$QT_TARGET_TRIPLE" \
      "$CROSSFORGE_JOBS" \
      webengine

FROM qt-target-webengine-build AS qt-target-build
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
ARG CROSSFORGE_JOBS=4
RUN --network=none /work/scripts/build-qt-target.sh \
      "/work/build/qt-$QT_TARGET_ARCH" \
      "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE" \
      "$QT_TARGET_ARCH" \
      "$QT_TARGET_TRIPLE" \
      "$CROSSFORGE_JOBS" \
      complete

FROM qt-target-build AS qt-target-install-checked
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
COPY --chmod=0755 scripts/check-qt-target-install.sh \
  /work/scripts/check-qt-target-install.sh
RUN --network=none /work/scripts/check-qt-target-install.sh \
      "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE" \
      "$QT_TARGET_ARCH" \
      "$QT_TARGET_TRIPLE"

FROM scratch AS qt-target-build-observation
ARG QT_TARGET_ARCH
COPY --from=qt-target-install-checked \
  /work/build/qt-${QT_TARGET_ARCH}/webengine-build.log /
COPY --from=qt-target-install-checked \
  /work/build/qt-${QT_TARGET_ARCH}/build.log /
COPY --from=qt-target-install-checked \
  /work/build/qt-${QT_TARGET_ARCH}/install.log /
COPY --from=qt-target-install-checked \
  /work/build/qt-${QT_TARGET_ARCH}/install_manifest.txt /

FROM qt-target-install-checked AS qt-target-build-qualified
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
ARG CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256
COPY --from=qt-target-configure-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/qt-target-configure.json \
  /work/evidence/qt-target-configure.json
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-qualification-component.json
COPY config/qt-qualification.json /work/config/qt-qualification.json
COPY config/schemas/qt-qualification-plan.schema.json \
  config/schemas/qt-target-build.schema.json \
  config/schemas/qt-target-configure.schema.json \
  /work/config/schemas/
COPY integration/cmake/ /work/integration/cmake/
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/qualify-qt-target-configure.py \
  scripts/qualify-qt-target-build.py /work/scripts/
RUN --network=none /work/scripts/qualify-qt-target-build.py \
      --arch "$QT_TARGET_ARCH" \
      --triple "$QT_TARGET_TRIPLE" \
      --install-root \
        "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE" \
      --sysroot "/opt/crossforge/sysroots/el8/$QT_TARGET_ARCH" \
      --toolchain-root "/opt/crossforge/targets/$QT_TARGET_TRIPLE" \
      --qt-host /opt/crossforge/qualification/qt/6.8.4/host \
      --toolchain-file "/work/integration/cmake/$QT_TARGET_TRIPLE.cmake" \
      --build-root "/work/build/qt-$QT_TARGET_ARCH" \
      --configure-evidence /work/evidence/qt-target-configure.json \
      --plan /work/config/qt-qualification.json \
      --qualification-component \
        /work/config/qt-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_QUALIFICATION_SHA256" \
      --patch \
        /work/patches/qt/0001-xnnpack-use-uint16-neon-fp16-load.patch \
      --cmake /opt/crossforge/host-tools/cmake/4.4.0/bin/cmake \
      --ninja /opt/crossforge/host-tools/ninja/1.13.2/bin/ninja \
      --cxx "/opt/crossforge/targets/$QT_TARGET_TRIPLE/bin/$QT_TARGET_TRIPLE-g++" \
      --readelf \
        "/opt/crossforge/targets/$QT_TARGET_TRIPLE/bin/$QT_TARGET_TRIPLE-readelf" \
      --configure-builder /work/scripts/configure-qt-target.sh \
      --configure-qualifier /work/scripts/qualify-qt-target-configure.py \
      --builder /work/scripts/build-qt-target.sh \
      --install-checker /work/scripts/check-qt-target-install.sh \
      --diagnostics /work/scripts/print-build-log-diagnostics.py \
      --heartbeat /work/scripts/run-with-heartbeat.py \
      --output \
        "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE/qt-target-build.json"

FROM scratch AS qt-target-qualification-evidence
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
COPY --from=qt-target-build-qualified \
  /opt/crossforge/qualification/qt/6.8.4/targets/${QT_TARGET_TRIPLE}/qt-target-build.json /
COPY --from=qt-target-build-qualified \
  /work/evidence/qt-target-configure.json /
COPY --from=qt-target-build-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/webengine-build.log /
COPY --from=qt-target-build-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/build.log /
COPY --from=qt-target-build-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/install.log /
COPY --from=qt-target-build-qualified \
  /work/build/qt-${QT_TARGET_ARCH}/install_manifest.txt /
