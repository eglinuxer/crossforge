# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

FROM crossforge_host_common AS source-bundle-assemble
ARG CROSSFORGE_SOURCE_COMMIT
WORKDIR /work

COPY . /work/project/
COPY --from=crossforge_rpm_sources /source-rpms/ \
  /work/bundle/sources/product/rpm/
COPY --from=crossforge_rpm_sources /rpm-source-el8.json \
  /work/bundle/metadata/rpm-source-el8.json

COPY --from=crossforge_cpython_cp39 /out/Python.tar.xz \
  /work/bundle/sources/product/cpython/Python-3.9.25.tar.xz
COPY --from=crossforge_cpython_cp310 /out/Python.tar.xz \
  /work/bundle/sources/product/cpython/Python-3.10.21.tar.xz
COPY --from=crossforge_cpython_cp311 /out/Python.tar.xz \
  /work/bundle/sources/product/cpython/Python-3.11.16.tar.xz
COPY --from=crossforge_cpython_cp312 /out/Python.tar.xz \
  /work/bundle/sources/product/cpython/Python-3.12.14.tar.xz
COPY --from=crossforge_cpython_cp313 /out/Python.tar.xz \
  /work/bundle/sources/product/cpython/Python-3.13.15.tar.xz
COPY --from=crossforge_cpython_cp314 /out/Python.tar.xz \
  /work/bundle/sources/product/cpython/Python-3.14.7.tar.xz

COPY --from=crossforge_zstd_source /out/materials/zstd.tar.gz \
  /work/bundle/sources/product/zstd/zstd-1.5.7.tar.gz
COPY --from=crossforge_zstd_source /out/materials/zstd.tar.gz.sig \
  /work/bundle/verification/zstd/zstd-1.5.7.tar.gz.sig
COPY --from=crossforge_zstd_source /out/source-manifest.json \
  /work/bundle/metadata/zstd-source.json

COPY --from=crossforge_cmake_source /materials/cmake-4.4.0.tar.gz \
  /work/bundle/sources/product/cmake/cmake-4.4.0.tar.gz
COPY --from=crossforge_cmake_source /materials/cmake-4.4.0-SHA-256.txt \
  /work/bundle/verification/cmake/cmake-4.4.0-SHA-256.txt
COPY --from=crossforge_cmake_source /materials/cmake-4.4.0-SHA-256.txt.asc \
  /work/bundle/verification/cmake/cmake-4.4.0-SHA-256.txt.asc
COPY --from=crossforge_cmake_source /materials/CMAKE-RELEASE-KEY.asc \
  /work/bundle/verification/cmake/CMAKE-RELEASE-KEY.asc
COPY --from=crossforge_cmake_source /source-manifest.json \
  /work/bundle/metadata/cmake-source.json

COPY --from=crossforge_ninja_source /materials/ninja-source.tar.gz \
  /work/bundle/sources/product/ninja/ninja-1.13.2.tar.gz
COPY --from=crossforge_ninja_source /source.json \
  /work/bundle/metadata/ninja-source.json

COPY --from=crossforge_vcpkg_source /root/ /work/vcpkg-registry/
COPY --from=crossforge_vcpkg_source /materials/vcpkg-tool-98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8.tar.gz \
  /work/bundle/sources/product/vcpkg/vcpkg-tool-98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8.tar.gz
COPY --from=crossforge_vcpkg_source /materials/vcpkg-glibc.sig \
  /work/bundle/verification/vcpkg/vcpkg-glibc.sig
COPY --from=crossforge_vcpkg_source /materials/MICROSOFT-RELEASE-KEY.asc \
  /work/bundle/verification/vcpkg/MICROSOFT-RELEASE-KEY.asc
COPY --from=crossforge_vcpkg_source /source.json \
  /work/bundle/metadata/vcpkg-source.json

COPY --from=crossforge_nfpm_tool /source/nfpm-2.47.0.tar.gz \
  /work/bundle/sources/product/nfpm/nfpm-2.47.0.tar.gz
COPY --from=crossforge_nfpm_tool /materials/checksums.txt \
  /work/bundle/verification/nfpm/checksums.txt
COPY --from=crossforge_nfpm_tool /materials/checksums.txt.sigstore.json \
  /work/bundle/verification/nfpm/checksums.txt.sigstore.json
COPY --from=crossforge_nfpm_tool /source.json \
  /work/bundle/metadata/nfpm-source.json

COPY --from=crossforge_qemu_source /materials/qemu-10.2.3.tar.xz \
  /work/bundle/sources/product/qemu/qemu-10.2.3.tar.xz
COPY --from=crossforge_qemu_source /materials/binfmt-e29e7d72c9672c8c8bf846655ab149b50e1a62bd.tar.gz \
  /work/bundle/sources/product/qemu/binfmt-e29e7d72c9672c8c8bf846655ab149b50e1a62bd.tar.gz
COPY --from=crossforge_qemu_source /materials/qemu-10.2.3.tar.xz.sig \
  /work/bundle/verification/qemu/qemu-10.2.3.tar.xz.sig
COPY --from=crossforge_qemu_source /materials/QEMU-RELEASE-KEY.asc \
  /work/bundle/verification/qemu/QEMU-RELEASE-KEY.asc
COPY --from=crossforge_qemu_source /source-manifest.json \
  /work/bundle/metadata/qemu-source.json

COPY --from=crossforge_qt_source /materials/qt-everywhere-opensource-src-6.8.4.tar.xz \
  /work/bundle/sources/qualification/qt/qt-everywhere-opensource-src-6.8.4.tar.xz
COPY --from=crossforge_qt_source /materials/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256 \
  /work/bundle/verification/qt/qt-everywhere-opensource-src-6.8.4.tar.xz.sha256
COPY --from=crossforge_qt_source /source-manifest.json \
  /work/bundle/metadata/qt-source.json

COPY --from=crossforge_ffmpeg_source /materials/ffmpeg-7.1.1.tar.xz \
  /work/bundle/sources/qualification/ffmpeg/ffmpeg-7.1.1.tar.xz
COPY --from=crossforge_ffmpeg_source /materials/ffmpeg-7.1.1.tar.xz.asc \
  /work/bundle/verification/ffmpeg/ffmpeg-7.1.1.tar.xz.asc
COPY --from=crossforge_ffmpeg_source /source-manifest.json \
  /work/bundle/metadata/ffmpeg-source.json

COPY --from=crossforge_xcb_source /materials/xcb-util-cursor-0.1.6.tar.xz \
  /work/bundle/sources/qualification/xcb-util-cursor/xcb-util-cursor-0.1.6.tar.xz
COPY --from=crossforge_xcb_source /materials/xcb-util-cursor-0.1.6.tar.xz.sig \
  /work/bundle/verification/xcb-util-cursor/xcb-util-cursor-0.1.6.tar.xz.sig
COPY --from=crossforge_xcb_source /source-manifest.json \
  /work/bundle/metadata/xcb-util-cursor-source.json

COPY keys/RPM-GPG-KEY-rockyofficial \
  /work/bundle/verification/rocky/RPM-GPG-KEY-rockyofficial
COPY keys/ZSTD-RELEASE-KEY.asc \
  /work/bundle/verification/zstd/ZSTD-RELEASE-KEY.asc
COPY keys/FFMPEG-RELEASE-KEY.asc \
  /work/bundle/verification/ffmpeg/FFMPEG-RELEASE-KEY.asc
COPY keys/XCB-UTIL-CURSOR-RELEASE-KEY.asc \
  /work/bundle/verification/xcb-util-cursor/XCB-UTIL-CURSOR-RELEASE-KEY.asc

SHELL ["/bin/bash", "-Eeuo", "pipefail", "-c"]
RUN --network=none \
    test "$CROSSFORGE_SOURCE_COMMIT" != 0000000000000000000000000000000000000000 \
    && [[ "$CROSSFORGE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    && mkdir -p /work/bundle/verification/cpython \
      /work/bundle/sources/product/crossforge \
      /work/bundle/sources/product/vcpkg /work/archive-root /out \
    && for version in 3.9.25 3.10.21 3.11.16 3.12.14 3.13.15 3.14.7; do \
      base64 --decode \
        "/work/project/evidence/sigstore/cpython-${version}.tar.xz.sigstore.b64" \
        > "/work/bundle/verification/cpython/Python-${version}.tar.xz.sigstore"; \
    done \
    && rm -f /work/vcpkg-registry/vcpkg \
      /work/vcpkg-registry/vcpkg.disable-metrics \
    && rm -rf /work/vcpkg-registry/licenses/vcpkg-tool \
    && test ! -e /work/vcpkg-registry/vcpkg \
    && test ! -e /work/vcpkg-registry/vcpkg.disable-metrics \
    && test ! -e /work/vcpkg-registry/licenses/vcpkg-tool \
    && mkdir -p /work/vcpkg-root/vcpkg-2026.07.29 \
    && cp -a /work/vcpkg-registry/. /work/vcpkg-root/vcpkg-2026.07.29/ \
    && tar --sort=name --format=posix \
      --pax-option=delete=atime,delete=ctime --mtime=@0 \
      --owner=0 --group=0 --numeric-owner \
      -C /work/vcpkg-root -cf - vcpkg-2026.07.29 \
      | gzip -n > /work/bundle/sources/product/vcpkg/vcpkg-registry-2026.07.29.tar.gz \
    && mkdir -p "/work/project-root/crossforge-${CROSSFORGE_SOURCE_COMMIT}" \
    && cp -a /work/project/. \
      "/work/project-root/crossforge-${CROSSFORGE_SOURCE_COMMIT}/" \
    && tar --sort=name --format=posix \
      --pax-option=delete=atime,delete=ctime --mtime=@0 \
      --owner=0 --group=0 --numeric-owner \
      -C /work/project-root -cf - "crossforge-${CROSSFORGE_SOURCE_COMMIT}" \
      | gzip -n > "/work/bundle/sources/product/crossforge/crossforge-${CROSSFORGE_SOURCE_COMMIT}.tar.gz" \
    && /usr/libexec/platform-python \
      /work/project/scripts/assemble-source-bundle.py \
      --root /work/bundle --source-commit "$CROSSFORGE_SOURCE_COMMIT" \
      --output /work/bundle/MANIFEST.json \
    && cp /work/project/docs/source-bundle.md /work/bundle/README.md \
    && mv /work/bundle \
      "/work/archive-root/crossforge-source-${CROSSFORGE_SOURCE_COMMIT}" \
    && tar --sort=name --format=posix \
      --pax-option=delete=atime,delete=ctime --mtime=@0 \
      --owner=0 --group=0 --numeric-owner \
      -C /work/archive-root -cf - \
      "crossforge-source-${CROSSFORGE_SOURCE_COMMIT}" \
      | zstd -q -T0 -1 -o \
        "/out/crossforge-source-${CROSSFORGE_SOURCE_COMMIT}.tar.zst" \
    && cd /out \
    && sha256sum "crossforge-source-${CROSSFORGE_SOURCE_COMMIT}.tar.zst" \
      > "crossforge-source-${CROSSFORGE_SOURCE_COMMIT}.tar.zst.sha256"

FROM scratch AS source-bundle-output
ARG CROSSFORGE_SOURCE_COMMIT
COPY --from=source-bundle-assemble /out/ /
LABEL org.opencontainers.image.title="Crossforge corresponding source" \
      org.opencontainers.image.description="Complete corresponding source for a Crossforge SDK candidate" \
      org.opencontainers.image.source="https://github.com/eglinuxer/crossforge" \
      org.opencontainers.image.revision="${CROSSFORGE_SOURCE_COMMIT}"

FROM source-bundle-assemble AS source-bundle-identity
ARG CROSSFORGE_SOURCE_COMMIT
RUN --network=none mkdir -p /identity \
    && /usr/libexec/platform-python \
      /work/project/scripts/source-bundle-identity.py \
      --archive "/out/crossforge-source-${CROSSFORGE_SOURCE_COMMIT}.tar.zst" \
      --checksum "/out/crossforge-source-${CROSSFORGE_SOURCE_COMMIT}.tar.zst.sha256" \
      --manifest \
        "/work/archive-root/crossforge-source-${CROSSFORGE_SOURCE_COMMIT}/MANIFEST.json" \
      --source-commit "$CROSSFORGE_SOURCE_COMMIT" \
      --output /identity/source-bundle.json

FROM scratch AS source-bundle-identity-output
COPY --from=source-bundle-identity /identity/source-bundle.json \
  /source-bundle.json
