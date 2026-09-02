# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Parameterized CPython row pipeline. Every external input is a Bake target
# context, so row/target cache identity and the QEMU qualification boundary are
# explicit in the generated graph.

FROM crossforge_config AS cpython-source
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 scripts/fetch-release-source.py /work/scripts/fetch-release-source.py
COPY scripts/validate-release.py /work/scripts/validate-release.py
COPY scripts/python_row_contract.py /work/scripts/python_row_contract.py
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER"
RUN /usr/libexec/platform-python /work/scripts/fetch-release-source.py python \
      --version "$CPYTHON_VERSION" \
      --config /src/config/release.json \
      --schema /src/config/schemas/release.schema.json \
      --output /out/Python.tar.xz

FROM crossforge_host_python AS python-host
WORKDIR /src
COPY config/release.json /src/config/release.json
COPY config/schemas/release.schema.json /src/config/schemas/release.schema.json
COPY scripts/validate-release.py /work/scripts/validate-release.py
COPY scripts/python_row_contract.py /work/scripts/python_row_contract.py
COPY scripts/finalize-cpython-qualification.py \
  scripts/python_sdk_identity.py scripts/target_artifact_audit.py \
  /work/scripts/
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 docker/finalize-python-row.py /work/scripts/finalize-python-row.py
RUN /usr/libexec/platform-python /work/scripts/validate-release.py \
      /src/config/release.json \
      --schema /src/config/schemas/release.schema.json

FROM python-host AS cpython-prepared
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
COPY --from=crossforge_cpython_source /out/Python.tar.xz /work/source/Python.tar.xz
COPY --chmod=0755 scripts/prepare-cpython-source.py /work/scripts/prepare-cpython-source.py
COPY patches/ /work/patches/
RUN --network=none command -v patch >/dev/null \
    && /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
    && /usr/libexec/platform-python /work/scripts/prepare-cpython-source.py \
      --row "$CPYTHON_ROW" \
      --archive /work/source/Python.tar.xz \
      --destination /work/src/cpython \
      --manifest /work/source/source-manifest.json \
      --config /src/config/release.json \
      --schema /src/config/schemas/release.schema.json \
    && /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --manifest /work/source/source-manifest.json

FROM crossforge_cpython_prepared AS cpython-build
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_JOBS=4
COPY --chmod=0755 scripts/build-cpython-native.sh /work/scripts/build-cpython-native.sh
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --manifest /work/source/source-manifest.json
RUN --network=none minor="${CPYTHON_VERSION%.*}" \
    && compact_minor="${minor/./}" \
    && /work/scripts/build-cpython-native.sh \
      /work/src/cpython \
      "/work/build/cpython-$CPYTHON_ROW-native" \
      "/opt/crossforge/python/$CPYTHON_ROW/build" \
      "$CPYTHON_VERSION" \
      "$CPYTHON_ADAPTER" \
      "$CROSSFORGE_JOBS" \
    && test "$CPYTHON_ROW" = "cp$compact_minor"

FROM python-host AS cpython-cross
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_toolchain \
  /opt/crossforge/targets/ /opt/crossforge/targets/
COPY --from=crossforge_toolchain \
  /opt/crossforge/sysroots/ /opt/crossforge/sysroots/
COPY --from=crossforge_cpython_prepared /work/src/cpython/ /work/src/cpython/
COPY --from=crossforge_cpython_prepared /work/source/ /work/source/
COPY --from=crossforge_cpython_build \
  /opt/crossforge/python/ /opt/crossforge/python/
COPY --chmod=0755 scripts/build-cpython-cross.sh /work/scripts/build-cpython-cross.sh
COPY scripts/deny-target-exec.c scripts/target-artifact-canary.c \
  scripts/target-exec-canary.c /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --manifest /work/source/source-manifest.json
RUN --network=none case "$CROSSFORGE_TARGET_ARCH:$CROSSFORGE_TARGET_TRIPLE" in \
      x86_64:x86_64-unknown-linux-gnu|aarch64:aarch64-unknown-linux-gnu) ;; \
      *) echo "error: target architecture/triple mismatch" >&2; exit 1 ;; \
    esac \
    && minor="${CPYTHON_VERSION%.*}" \
    && /work/scripts/build-cpython-cross.sh \
      /work/src/cpython \
      "/work/build/cpython-$CPYTHON_ROW-$CROSSFORGE_TARGET_ARCH" \
      "/opt/crossforge/python/$CPYTHON_ROW/targets/$CROSSFORGE_TARGET_TRIPLE" \
      "/opt/crossforge/sysroots/el8/$CROSSFORGE_TARGET_ARCH" \
      "/opt/crossforge/targets/$CROSSFORGE_TARGET_TRIPLE" \
      "$CROSSFORGE_TARGET_TRIPLE" \
      "/opt/crossforge/python/$CPYTHON_ROW/build/bin/python$minor" \
      "$CPYTHON_VERSION" \
      "$CPYTHON_ADAPTER" \
      "$CROSSFORGE_JOBS"

# Static qualification remains host-only. It compiles a target extension and
# audits every target ELF but has no QEMU input and performs no target execution.
FROM crossforge_cpython_cross AS cpython-qualify-build
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
COPY config/release.json /src/config/release.json
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 scripts/qualify-cpython.py /work/scripts/qualify-cpython.py
COPY scripts/python_sdk_identity.py scripts/target_artifact_audit.py /work/scripts/
COPY tests/python/minimal_extension.c /work/tests/python/minimal_extension.c
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER"
RUN --network=none minor="${CPYTHON_VERSION%.*}" \
    && /usr/libexec/platform-python /work/scripts/qualify-cpython.py \
      --prefix "/opt/crossforge/python/$CPYTHON_ROW/targets/$CROSSFORGE_TARGET_TRIPLE" \
      --build-python "/opt/crossforge/python/$CPYTHON_ROW/build/bin/python$minor" \
      --build-directory "/work/build/cpython-$CPYTHON_ROW-$CROSSFORGE_TARGET_ARCH" \
      --toolchain "/opt/crossforge/targets/$CROSSFORGE_TARGET_TRIPLE" \
      --sysroot "/opt/crossforge/sysroots/el8/$CROSSFORGE_TARGET_ARCH" \
      --target "$CROSSFORGE_TARGET_TRIPLE" \
      --version "$CPYTHON_VERSION" \
      --extension-source /work/tests/python/minimal_extension.c \
      --work "/work/qualification/python/$CPYTHON_ROW/$CROSSFORGE_TARGET_ARCH" \
      --release /src/config/release.json \
      --report "/work/qualification/python/$CPYTHON_ROW/$CROSSFORGE_TARGET_ARCH/compile.json"

FROM python-host AS cpython-runtime-input
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
COPY --from=crossforge_sysroot \
  /opt/crossforge/sysroots/el8/${CROSSFORGE_TARGET_ARCH}/ /runtime-locked/
COPY --from=crossforge_clean_runtime /runtime-root/ /runtime-clean/
COPY --from=crossforge_clean_runtime \
  /work/qualification/python/ /work/qualification/python/
COPY --from=crossforge_cpython_qualify_build \
  /opt/crossforge/python/ /opt/crossforge/python/
COPY --from=crossforge_cpython_qualify_build \
  /work/qualification/python/ /work/qualification/python/
COPY scripts/loader_evidence.py /work/scripts/loader_evidence.py
COPY --chmod=0755 scripts/run-cpython-runtime.py /work/scripts/run-cpython-runtime.py
COPY --chmod=0755 scripts/finalize-cpython-qualification.py \
  /work/scripts/finalize-cpython-qualification.py
COPY --chmod=0755 docker/run-python-qualification.sh \
  /work/scripts/run-python-qualification.sh
COPY tests/python/runtime_probe.py /work/tests/python/runtime_probe.py
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
    && mkdir -p /runtime-locked/.crossforge /runtime-clean/.crossforge

FROM cpython-runtime-input AS cpython-qualify-x86_64
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
RUN --network=none \
    --mount=type=tmpfs,target=/runtime-locked/dev/shm \
    --mount=type=tmpfs,target=/runtime-clean/dev/shm \
    test "$CROSSFORGE_TARGET_ARCH" = x86_64 \
    && test "$CROSSFORGE_TARGET_TRIPLE" = x86_64-unknown-linux-gnu \
    && /work/scripts/run-python-qualification.sh \
      "$CPYTHON_ROW" "$CPYTHON_VERSION" "$CPYTHON_ADAPTER" \
      "$CROSSFORGE_TARGET_ARCH" "$CROSSFORGE_TARGET_TRIPLE"

FROM cpython-runtime-input AS cpython-qualify-aarch64
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
RUN --network=none \
    --mount=type=tmpfs,target=/runtime-locked/dev/shm \
    --mount=type=tmpfs,target=/runtime-clean/dev/shm \
    --mount=type=bind,from=crossforge_qemu_validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/runtime-locked/.crossforge/qemu-aarch64,ro \
    --mount=type=bind,from=crossforge_qemu_validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/runtime-clean/.crossforge/qemu-aarch64,ro \
    --mount=type=bind,from=crossforge_qemu_validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/work/qemu-aarch64,ro \
    test "$CROSSFORGE_TARGET_ARCH" = aarch64 \
    && test "$CROSSFORGE_TARGET_TRIPLE" = aarch64-unknown-linux-gnu \
    && /work/scripts/run-python-qualification.sh \
      "$CPYTHON_ROW" "$CPYTHON_VERSION" "$CPYTHON_ADAPTER" \
      "$CROSSFORGE_TARGET_ARCH" "$CROSSFORGE_TARGET_TRIPLE" \
      /work/qemu-aarch64

# A row export is deliberately scratch-based. Build-only RPMs, toolchains,
# runtime roots, QEMU and qualification extensions cannot cross this boundary.
FROM python-host AS cpython-row-assemble
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
COPY --from=crossforge_cpython_build \
  /opt/crossforge/python/${CPYTHON_ROW}/build/ \
  /row-export/opt/crossforge/python/${CPYTHON_ROW}/build/
COPY --from=crossforge_cpython_build \
  /work/source/source-manifest.json \
  /row-export/opt/crossforge/qualification/python/${CPYTHON_ROW}/source.json
COPY --from=crossforge_cpython_x86_64 \
  /opt/crossforge/python/${CPYTHON_ROW}/targets/x86_64-unknown-linux-gnu/ \
  /row-export/opt/crossforge/python/${CPYTHON_ROW}/targets/x86_64-unknown-linux-gnu/
COPY --from=crossforge_cpython_aarch64 \
  /opt/crossforge/python/${CPYTHON_ROW}/targets/aarch64-unknown-linux-gnu/ \
  /row-export/opt/crossforge/python/${CPYTHON_ROW}/targets/aarch64-unknown-linux-gnu/
COPY --from=crossforge_cpython_x86_64 \
  /work/qualification/python/${CPYTHON_ROW}/x86_64.json \
  /row-export/opt/crossforge/qualification/python/${CPYTHON_ROW}/x86_64.json
COPY --from=crossforge_cpython_aarch64 \
  /work/qualification/python/${CPYTHON_ROW}/aarch64.json \
  /row-export/opt/crossforge/qualification/python/${CPYTHON_ROW}/aarch64.json
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --manifest "/row-export/opt/crossforge/qualification/python/$CPYTHON_ROW/source.json" \
    && /usr/libexec/platform-python /work/scripts/finalize-python-row.py \
      --root /row-export \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --release /src/config/release.json \
      --source-manifest "/row-export/opt/crossforge/qualification/python/$CPYTHON_ROW/source.json" \
      --output "/row-export/opt/crossforge/qualification/python/$CPYTHON_ROW/row.json"

FROM scratch AS cpython-row-export
COPY --from=cpython-row-assemble /row-export/ /

# The common dev base carries both qualified toolchains/sysroots exactly once.
FROM python-host AS sdk-toolchains-dev
COPY --from=crossforge_toolchain_x86_64 \
  /opt/crossforge/targets/x86_64-unknown-linux-gnu/ \
  /opt/crossforge/targets/x86_64-unknown-linux-gnu/
COPY --from=crossforge_toolchain_x86_64 \
  /opt/crossforge/sysroots/el8/x86_64/ \
  /opt/crossforge/sysroots/el8/x86_64/
COPY --from=crossforge_toolchain_aarch64 \
  /opt/crossforge/targets/aarch64-unknown-linux-gnu/ \
  /opt/crossforge/targets/aarch64-unknown-linux-gnu/
COPY --from=crossforge_toolchain_aarch64 \
  /opt/crossforge/sysroots/el8/aarch64/ \
  /opt/crossforge/sysroots/el8/aarch64/
RUN test ! -e /usr/local/libexec/crossforge/qemu-aarch64 \
    && test ! -e /runtime-locked \
    && test ! -e /runtime-clean \
    && test ! -e /rpm-bundle \
    && test ! -e /rpm-bundle-python

# This is the only cumulative stage. It appends one already-qualified scratch
# row to an SDK base and rejects duplicate rows before COPY can overwrite them.
FROM crossforge_sdk_base AS python-sdk-append
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
COPY config/release.json /src/config/release.json
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 docker/finalize-python-row.py /work/scripts/finalize-python-row.py
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
    && test ! -e "/opt/crossforge/python/$CPYTHON_ROW" \
    && test ! -e "/opt/crossforge/qualification/python/$CPYTHON_ROW"
COPY --from=crossforge_python_row \
  /opt/crossforge/python/${CPYTHON_ROW}/ \
  /opt/crossforge/python/${CPYTHON_ROW}/
COPY --from=crossforge_python_row \
  /opt/crossforge/qualification/python/${CPYTHON_ROW}/ \
  /opt/crossforge/qualification/python/${CPYTHON_ROW}/
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --manifest "/opt/crossforge/qualification/python/$CPYTHON_ROW/source.json" \
    && /usr/libexec/platform-python /work/scripts/finalize-python-row.py \
      --root / \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --release /src/config/release.json \
      --source-manifest "/opt/crossforge/qualification/python/$CPYTHON_ROW/source.json" \
      --output /tmp/python-row.json \
    && cmp -s /tmp/python-row.json \
      "/opt/crossforge/qualification/python/$CPYTHON_ROW/row.json" \
    && rm -f /tmp/python-row.json

FROM crossforge_sdk_base AS python-sdk-final
ARG CROSSFORGE_PYTHON_ROWS
RUN test ! -e /usr/local/libexec/crossforge/qemu-aarch64 \
    && test ! -e /runtime-locked \
    && test ! -e /runtime-clean \
    && expected=0 \
    && for row in $CROSSFORGE_PYTHON_ROWS; do \
         test -f "/opt/crossforge/qualification/python/$row/row.json"; \
         expected=$((expected + 1)); \
       done \
    && actual=$(find /opt/crossforge/qualification/python \
         -mindepth 2 -maxdepth 2 -name row.json -type f | wc -l) \
    && test "$actual" -eq "$expected"
