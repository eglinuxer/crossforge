# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Parameterized CPython row pipeline. Every external input is a Bake target
# context, so row/target cache identity and the QEMU qualification boundary are
# explicit in the generated graph.

FROM crossforge_rocky_amd64 AS cpython-source
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_SOURCE_COMPONENT
ARG CPYTHON_SOURCE_COMPONENT_SHA256
COPY config/generated/components/python/${CPYTHON_ROW}-source.json \
  /work/config/python-source-component.json
COPY --chmod=0755 scripts/release_component.py \
  scripts/fetch-release-source.py /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/fetch-release-source.py python \
      --version "$CPYTHON_VERSION" \
      --component-file /work/config/python-source-component.json \
      --expected-component "$CPYTHON_SOURCE_COMPONENT" \
      --expected-scope build \
      --expected-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256" \
      --output /out/Python.tar.xz

# Row patch contexts are normalized to one scratch layout. Patched rows receive
# only their minor directory as a named local context; unpatched rows receive a
# controlled empty directory with no repository fallback.
FROM crossforge_rocky_amd64 AS cpython-empty-patches-build
RUN mkdir -p /row-patches

FROM scratch AS cpython-empty-patches
COPY --from=cpython-empty-patches-build /row-patches/ /row-patches/

FROM scratch AS cpython-patch-context
COPY --from=crossforge_cpython_patch_files / /row-patches/

# A stable absence context lets every parameterized build use the same COPY
# shape without making pre-3.14 rows depend on the private zstd source/build.
FROM crossforge_rocky_amd64 AS zstd-empty-build
RUN for identity in host x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu; do \
      directory="/empty/opt/crossforge/deps/zstd/none/$identity"; \
      mkdir -p "$directory"; \
      touch "$directory/.crossforge-empty"; \
    done

FROM scratch AS zstd-empty
COPY --from=zstd-empty-build /empty/ /

# Build stages inherit only the locked host tool closure. Release-wide policy
# and qualification/finalization tools enter through the sibling host below.
FROM crossforge_host_python AS python-build-host
WORKDIR /src

FROM python-build-host AS python-host
COPY config/release.json /src/config/release.json
COPY config/schemas/release.schema.json /src/config/schemas/release.schema.json
COPY scripts/validate-release.py /work/scripts/validate-release.py
COPY scripts/python_row_contract.py /work/scripts/python_row_contract.py
COPY scripts/finalize-cpython-qualification.py \
  scripts/python_sdk_identity.py scripts/python_zstd_evidence.py \
  scripts/target_artifact_audit.py \
  scripts/python_source_release_binding.py \
  scripts/release-components-core.py \
  /work/scripts/
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 docker/finalize-python-row.py /work/scripts/finalize-python-row.py
RUN /usr/libexec/platform-python /work/scripts/validate-release.py \
      /src/config/release.json \
      --schema /src/config/schemas/release.schema.json

FROM python-build-host AS cpython-prepared
ARG CPYTHON_ROW
ARG CPYTHON_MINOR
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CPYTHON_SOURCE_COMPONENT
ARG CPYTHON_SOURCE_COMPONENT_SHA256
ARG CPYTHON_BUILD_POLICY_COMPONENT
ARG CPYTHON_BUILD_POLICY_COMPONENT_SHA256
COPY --from=crossforge_cpython_source /out/Python.tar.xz /work/source/Python.tar.xz
COPY --from=crossforge_cpython_source \
  /work/config/python-source-component.json \
  /work/config/python-source-component.json
COPY config/generated/components/implementation/python-${CPYTHON_ROW}-build-policy.json \
  /work/config/python-build-policy-component.json
COPY --from=crossforge_cpython_patches /row-patches/ \
  /work/patches/cpython/${CPYTHON_MINOR}/
COPY --chmod=0755 scripts/prepare-cpython-source.py \
  scripts/release_component.py /work/scripts/
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
RUN --network=none command -v patch >/dev/null \
    && test "$CPYTHON_MINOR" = "${CPYTHON_VERSION%.*}" \
    && test "$CPYTHON_SOURCE_COMPONENT" = "python/$CPYTHON_ROW-source" \
    && test "$CPYTHON_BUILD_POLICY_COMPONENT" = \
      "implementation/python-$CPYTHON_ROW-build-policy" \
    && /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --source-component /work/config/python-source-component.json \
      --source-component-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256" \
      --policy-component /work/config/python-build-policy-component.json \
      --policy-component-sha256 "$CPYTHON_BUILD_POLICY_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/prepare-cpython-source.py \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --archive /work/source/Python.tar.xz \
      --destination /work/src/cpython \
      --manifest /work/source/source-manifest.json \
      --source-component /work/config/python-source-component.json \
      --source-component-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256" \
      --policy-component /work/config/python-build-policy-component.json \
      --policy-component-sha256 "$CPYTHON_BUILD_POLICY_COMPONENT_SHA256" \
    && /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --source-component /work/config/python-source-component.json \
      --source-component-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256" \
      --policy-component /work/config/python-build-policy-component.json \
      --policy-component-sha256 "$CPYTHON_BUILD_POLICY_COMPONENT_SHA256" \
      --manifest /work/source/source-manifest.json

FROM crossforge_cpython_prepared AS cpython-build
ARG CPYTHON_ROW
ARG CPYTHON_MINOR
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CPYTHON_SOURCE_COMPONENT_SHA256
ARG CPYTHON_BUILD_POLICY_COMPONENT_SHA256
ARG CPYTHON_ZSTD_VERSION
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_zstd \
  /opt/crossforge/deps/zstd/${CPYTHON_ZSTD_VERSION}/host/ \
  /work/deps/zstd/
COPY --chmod=0755 scripts/build-cpython-native.sh /work/scripts/build-cpython-native.sh
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --source-component /work/config/python-source-component.json \
      --source-component-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256" \
      --policy-component /work/config/python-build-policy-component.json \
      --policy-component-sha256 "$CPYTHON_BUILD_POLICY_COMPONENT_SHA256" \
      --manifest /work/source/source-manifest.json
RUN --network=none test "$CPYTHON_MINOR" = "${CPYTHON_VERSION%.*}" \
    && compact_minor="${CPYTHON_MINOR/./}" \
    && /work/scripts/build-cpython-native.sh \
      /work/src/cpython \
      "/work/build/cpython-$CPYTHON_ROW-native" \
      "/opt/crossforge/python/$CPYTHON_ROW/build" \
      "$CPYTHON_VERSION" \
      /work/deps/zstd \
      "$CROSSFORGE_JOBS" \
    && test "$CPYTHON_ROW" = "cp$compact_minor"

FROM python-build-host AS cpython-cross
ARG CPYTHON_ROW
ARG CPYTHON_MINOR
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CPYTHON_SOURCE_COMPONENT_SHA256
ARG CPYTHON_BUILD_POLICY_COMPONENT_SHA256
ARG CPYTHON_ZSTD_VERSION
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
ARG CROSSFORGE_JOBS=4
COPY --from=crossforge_toolchain \
  /opt/crossforge/targets/ /opt/crossforge/targets/
COPY --from=crossforge_toolchain \
  /opt/crossforge/sysroots/ /opt/crossforge/sysroots/
COPY --from=crossforge_cpython_prepared /work/src/cpython/ /work/src/cpython/
COPY --from=crossforge_cpython_prepared /work/source/ /work/source/
COPY --from=crossforge_cpython_prepared /work/config/ /work/config/
COPY --from=crossforge_cpython_prepared /work/scripts/ /work/scripts/
COPY --from=crossforge_cpython_build \
  /opt/crossforge/python/ /opt/crossforge/python/
COPY --from=crossforge_zstd \
  /opt/crossforge/deps/zstd/${CPYTHON_ZSTD_VERSION}/${CROSSFORGE_TARGET_TRIPLE}/ \
  /work/deps/zstd/
COPY --chmod=0755 scripts/build-cpython-cross.sh \
  scripts/verify-python-build-sysconfig.py /work/scripts/
COPY scripts/deny-target-exec.c scripts/target-artifact-canary.c \
  scripts/target-exec-canary.c /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --source-component /work/config/python-source-component.json \
      --source-component-sha256 "$CPYTHON_SOURCE_COMPONENT_SHA256" \
      --policy-component /work/config/python-build-policy-component.json \
      --policy-component-sha256 "$CPYTHON_BUILD_POLICY_COMPONENT_SHA256" \
      --manifest /work/source/source-manifest.json
RUN --network=none case "$CROSSFORGE_TARGET_ARCH:$CROSSFORGE_TARGET_TRIPLE" in \
      x86_64:x86_64-unknown-linux-gnu|aarch64:aarch64-unknown-linux-gnu) ;; \
      *) echo "error: target architecture/triple mismatch" >&2; exit 1 ;; \
    esac \
    && test "$CPYTHON_MINOR" = "${CPYTHON_VERSION%.*}" \
    && /work/scripts/build-cpython-cross.sh \
      /work/src/cpython \
      "/work/build/cpython-$CPYTHON_ROW-$CROSSFORGE_TARGET_ARCH" \
      "/opt/crossforge/python/$CPYTHON_ROW/targets/$CROSSFORGE_TARGET_TRIPLE" \
      "/opt/crossforge/sysroots/el8/$CROSSFORGE_TARGET_ARCH" \
      "/opt/crossforge/targets/$CROSSFORGE_TARGET_TRIPLE" \
      "$CROSSFORGE_TARGET_TRIPLE" \
      "/opt/crossforge/python/$CPYTHON_ROW/build/bin/python$CPYTHON_MINOR" \
      "$CPYTHON_VERSION" \
      /work/deps/zstd \
      "$CROSSFORGE_JOBS"

# Static qualification remains host-only. It compiles a target extension and
# audits every target ELF but has no QEMU input and performs no target execution.
FROM crossforge_cpython_cross AS cpython-qualify-build
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
ARG CROSSFORGE_COMPONENT_IMPLEMENTATION_PYTHON_QUALIFICATION_POLICY_SHA256
ARG CROSSFORGE_COMPONENT_PYTHON_QUALIFICATION_SHA256
COPY config/release.json /src/config/release.json
COPY config/schemas/release.schema.json /src/config/schemas/release.schema.json
COPY abi/el8/${CROSSFORGE_TARGET_ARCH}.json /work/config/abi-baseline.json
COPY evidence/abi/el8-${CROSSFORGE_TARGET_ARCH}-sysroot.json \
  /work/config/abi-sysroot-inventory.json
COPY evidence/abi/el8-${CROSSFORGE_TARGET_ARCH}-python-provider-catalog.json \
  /work/config/python-provider-catalog.json
COPY config/abi-providers.json config/python-runtime-providers.json \
  /work/config/
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 scripts/qualify-cpython.py /work/scripts/qualify-cpython.py
COPY scripts/abi_contract.py scripts/python_abi_audit.py \
  scripts/python_runtime_providers.py scripts/python_sdk_identity.py \
  scripts/target_artifact_audit.py \
  scripts/python_source_release_binding.py scripts/release-components-core.py \
  scripts/python_row_contract.py scripts/python_zstd_evidence.py \
  scripts/validate-release.py /work/scripts/
COPY tests/python/minimal_extension.c /work/tests/python/minimal_extension.c
RUN /usr/libexec/platform-python /work/scripts/validate-release.py \
      /src/config/release.json \
      --schema /src/config/schemas/release.schema.json \
    && /usr/libexec/platform-python /work/scripts/verify-python-row.py \
      --release /src/config/release.json \
      --row "$CPYTHON_ROW" \
      --version "$CPYTHON_VERSION" \
      --adapter "$CPYTHON_ADAPTER" \
      --manifest /work/source/source-manifest.json
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
      --abi-baseline /work/config/abi-baseline.json \
      --abi-provider-manifest /work/config/abi-providers.json \
      --sysroot-abi-inventory /work/config/abi-sysroot-inventory.json \
      --runtime-provider-policy /work/config/python-runtime-providers.json \
      --python-provider-catalog /work/config/python-provider-catalog.json \
      --qualification-policy-component-sha256 \
        "$CROSSFORGE_COMPONENT_IMPLEMENTATION_PYTHON_QUALIFICATION_POLICY_SHA256" \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_PYTHON_QUALIFICATION_SHA256" \
      --report "/work/qualification/python/$CPYTHON_ROW/$CROSSFORGE_TARGET_ARCH/compile.json"

FROM python-host AS cpython-runtime-input
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
ARG CROSSFORGE_TARGET_ARCH
ARG CROSSFORGE_TARGET_TRIPLE
COPY config/python-runtime-providers.json \
  /src/config/python-runtime-providers.json
COPY --from=crossforge_sysroot \
  /opt/crossforge/sysroots/el8/${CROSSFORGE_TARGET_ARCH}/ /runtime-locked/
COPY --from=crossforge_clean_runtime /runtime-root/ /runtime-clean/
COPY --from=crossforge_clean_runtime \
  /work/qualification/python/ /work/qualification/python/
COPY --from=crossforge_cpython_qualify_build \
  /opt/crossforge/python/ /opt/crossforge/python/
COPY --from=crossforge_cpython_qualify_build \
  /work/qualification/python/ /work/qualification/python/
COPY --from=crossforge_cpython_qualify_build \
  /work/config/ /work/config/
COPY scripts/loader_evidence.py /work/scripts/loader_evidence.py
COPY scripts/abi_contract.py scripts/python_abi_audit.py \
  scripts/python_runtime_providers.py /work/scripts/
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
COPY --from=crossforge_cpython_x86_64 \
  /work/config/ /work/abi-inputs/x86_64/
COPY --from=crossforge_cpython_aarch64 \
  /work/config/ /work/abi-inputs/aarch64/
COPY scripts/abi_contract.py scripts/python_abi_audit.py \
  scripts/python_runtime_providers.py /work/scripts/
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
      --abi-input-root /work/abi-inputs \
      --output "/row-export/opt/crossforge/qualification/python/$CPYTHON_ROW/row.json"

FROM scratch AS cpython-row-export
COPY --from=cpython-row-assemble /row-export/ /

# The user-facing base starts from the independently locked and qualified host
# runtime. Python/GCC build closures remain confined to earlier stages.
FROM crossforge_host_runtime AS sdk-toolchains-dev
WORKDIR /
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
COPY --from=crossforge_toolchain_x86_64 \
  /work/qualification/x86_64.json \
  /opt/crossforge/qualification/toolchain/x86_64.json
COPY --from=crossforge_toolchain_x86_64 \
  /work/qualification/clean-rocky.ok \
  /opt/crossforge/qualification/toolchain/x86_64-clean-runtime.ok
COPY --from=crossforge_toolchain_aarch64 \
  /work/qualification/aarch64.json \
  /opt/crossforge/qualification/toolchain/aarch64.json
RUN test ! -e /usr/local/libexec/crossforge/qemu-aarch64 \
    && test ! -e /runtime-locked \
    && test ! -e /runtime-clean \
    && test ! -e /rpm-bundle \
    && test ! -e /rpm-bundle-python \
    && rm -rf /src /work \
    && mkdir -p /workspace
WORKDIR /workspace

# This is the only cumulative stage. It appends one already-qualified scratch
# row to an SDK base and rejects duplicate rows before COPY can overwrite them.
FROM crossforge_sdk_base AS python-sdk-append
ARG CPYTHON_ROW
ARG CPYTHON_VERSION
ARG CPYTHON_ADAPTER
COPY config/release.json /src/config/release.json
COPY config/schemas/release.schema.json /src/config/schemas/release.schema.json
COPY --chmod=0755 docker/verify-python-row.py /work/scripts/verify-python-row.py
COPY --chmod=0755 docker/finalize-python-row.py /work/scripts/finalize-python-row.py
COPY scripts/abi_contract.py scripts/finalize-cpython-qualification.py \
  scripts/python_abi_audit.py scripts/python_runtime_providers.py \
  scripts/python_sdk_identity.py scripts/python_zstd_evidence.py \
  scripts/target_artifact_audit.py /work/scripts/
COPY scripts/python_source_release_binding.py \
  scripts/release-components-core.py scripts/python_row_contract.py \
  scripts/validate-release.py /work/scripts/
COPY abi/el8/x86_64.json /work/abi-inputs/x86_64/abi-baseline.json
COPY evidence/abi/el8-x86_64-sysroot.json \
  /work/abi-inputs/x86_64/abi-sysroot-inventory.json
COPY config/abi-providers.json \
  /work/abi-inputs/x86_64/abi-providers.json
COPY config/python-runtime-providers.json \
  /work/abi-inputs/x86_64/python-runtime-providers.json
COPY evidence/abi/el8-x86_64-python-provider-catalog.json \
  /work/abi-inputs/x86_64/python-provider-catalog.json
COPY abi/el8/aarch64.json /work/abi-inputs/aarch64/abi-baseline.json
COPY evidence/abi/el8-aarch64-sysroot.json \
  /work/abi-inputs/aarch64/abi-sysroot-inventory.json
COPY config/abi-providers.json \
  /work/abi-inputs/aarch64/abi-providers.json
COPY config/python-runtime-providers.json \
  /work/abi-inputs/aarch64/python-runtime-providers.json
COPY evidence/abi/el8-aarch64-python-provider-catalog.json \
  /work/abi-inputs/aarch64/python-provider-catalog.json
RUN /usr/libexec/platform-python /work/scripts/validate-release.py \
      /src/config/release.json \
      --schema /src/config/schemas/release.schema.json \
    && /usr/libexec/platform-python /work/scripts/verify-python-row.py \
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
      --abi-input-root /work/abi-inputs \
      --output /tmp/python-row.json \
    && cmp -s /tmp/python-row.json \
      "/opt/crossforge/qualification/python/$CPYTHON_ROW/row.json" \
    && rm -f /tmp/python-row.json \
    && rm -rf /work/abi-inputs

FROM crossforge_sdk_base AS python-sdk-final
ARG CROSSFORGE_PYTHON_ROWS
COPY --from=crossforge_qemu_validated \
  /usr/local/libexec/crossforge/qemu-aarch64 \
  /usr/local/libexec/crossforge/qemu-aarch64
COPY config/release.json /opt/crossforge/release.json
COPY --chmod=0755 scripts/qualify-final-sdk.py \
  scripts/loader_evidence.py scripts/python_row_contract.py \
  scripts/python_sdk_identity.py scripts/release-components-core.py \
  scripts/validate-release.py /work/scripts/
RUN --network=none test ! -e /runtime-locked \
    && test ! -e /runtime-clean \
    && expected=0 \
    && for row in $CROSSFORGE_PYTHON_ROWS; do \
         test -f "/opt/crossforge/qualification/python/$row/row.json"; \
         expected=$((expected + 1)); \
       done \
    && actual=$(find /opt/crossforge/qualification/python \
         -mindepth 2 -maxdepth 2 -name row.json -type f | wc -l) \
    && test "$actual" -eq "$expected" \
    && /work/scripts/qualify-final-sdk.py \
      --release /opt/crossforge/release.json \
      --rows $CROSSFORGE_PYTHON_ROWS \
      --qemu /usr/local/libexec/crossforge/qemu-aarch64 \
      --output /opt/crossforge/qualification/final-sdk.json \
    && rm -rf /src /work /sources /out /resolved /row-export /plans \
      /runtime-root /runtime-locked /runtime-clean /sysroot \
      /rpm-bundle /rpm-bundle-gcc \
      /rpm-bundle-python \
    && mkdir -p /workspace \
    && test ! -e /src \
    && test ! -e /work \
    && test ! -e /sources \
    && test ! -e /out \
    && test ! -e /resolved \
    && test ! -e /row-export \
    && test ! -e /runtime-root \
    && test ! -e /sysroot \
    && test ! -e /rpm-bundle \
    && test ! -e /rpm-bundle-gcc \
    && test ! -e /rpm-bundle-python \
    && test ! -e /opt/crossforge/README.phase1 \
    && ! find /opt/crossforge \
      \( -name .crossforge-empty -o -name '_crossforge*.so' \) \
      -print -quit | grep -q .
ENV CROSSFORGE_QEMU_AARCH64=/usr/local/libexec/crossforge/qemu-aarch64
WORKDIR /workspace
