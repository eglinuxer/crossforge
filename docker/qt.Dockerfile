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
