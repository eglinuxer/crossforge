# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Qt runtime qualification is deliberately isolated from qt.Dockerfile. Runtime
# policy and probe changes must consume, rather than invalidate, the qualified
# host and target Qt build artifacts.

FROM crossforge_host_qt AS qt-runtime-overlay-qualified
ARG QT_TARGET_ARCH
ARG ROCKY_TARGET_MANIFEST_DIGEST
ARG CROSSFORGE_COMPONENT_FUTURE_QT_RUNTIME_QUALIFICATION_SHA256
COPY --from=crossforge_rocky_target / /runtime-root/
COPY --from=crossforge_qt_runtime_rpms /rpm-bundle/ /runtime-rpm-bundle/
COPY config/release.json config/qt-qualification.json \
  config/qt-runtime-qualification.json /work/config/
COPY config/rpm/qt-runtime-el8-x86_64.plan.json \
  config/rpm/qt-runtime-el8-aarch64.plan.json /work/config/rpm/
COPY config/generated/components/future/qt-qualification.json \
  /work/config/qt-build-qualification-component.json
COPY config/generated/components/future/qt-runtime-qualification.json \
  /work/config/qt-runtime-qualification-component.json
COPY config/schemas/release.schema.json \
  config/schemas/rpm-lock.schema.json \
  config/schemas/rpm-plan.schema.json \
  config/schemas/rpm-transaction.schema.json \
  config/schemas/qt-qualification-plan.schema.json \
  config/schemas/qt-runtime-qualification-plan.schema.json \
  config/schemas/qt-runtime-overlay.schema.json \
  /work/config/schemas/
COPY keys/RPM-GPG-KEY-rockyofficial /work/keys/RPM-GPG-KEY-rockyofficial
COPY locks/qt-runtime-el8-x86_64.json \
  locks/qt-runtime-el8-aarch64.json /work/locks/
COPY locks/transactions/qt-runtime-el8-x86_64.json \
  locks/transactions/qt-runtime-el8-aarch64.json \
  /work/locks/transactions/
COPY locks/metadata/qt-runtime-el8-x86_64/ \
  /work/locks/metadata/qt-runtime-el8-x86_64/
COPY locks/metadata/qt-runtime-el8-aarch64/ \
  /work/locks/metadata/qt-runtime-el8-aarch64/
COPY --chmod=0755 scripts/assemble-python-runtime.py \
  scripts/assemble-qt-runtime.py scripts/materialize-sysroot.py \
  scripts/release_component.py scripts/validate-release.py \
  scripts/validate-rpm-lock.py /work/scripts/
RUN --network=none /usr/libexec/platform-python \
      /work/scripts/assemble-qt-runtime.py \
      "/work/locks/qt-runtime-el8-$QT_TARGET_ARCH.json" \
      --bundle /runtime-rpm-bundle \
      --key /work/keys/RPM-GPG-KEY-rockyofficial \
      --release /work/config/release.json \
      --plan /work/config/qt-runtime-qualification.json \
      --qualification-component \
        /work/config/qt-runtime-qualification-component.json \
      --qualification-component-sha256 \
        "$CROSSFORGE_COMPONENT_FUTURE_QT_RUNTIME_QUALIFICATION_SHA256" \
      --build-qualification-component \
        /work/config/qt-build-qualification-component.json \
      --runtime-root /runtime-root \
      --base-image-manifest-digest "$ROCKY_TARGET_MANIFEST_DIGEST" \
      --evidence /work/evidence/qt-runtime-overlay.json

FROM scratch AS qt-runtime-overlay-evidence
COPY --from=qt-runtime-overlay-qualified \
  /work/evidence/qt-runtime-overlay.json /

FROM crossforge_qt_target AS qt-runtime-artifacts-staged
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
COPY scripts/qt-plugin-probe.c /work/scripts/qt-plugin-probe.c
COPY --from=qt-runtime-overlay-qualified /runtime-root/ /runtime-root/
COPY --from=qt-runtime-overlay-qualified \
  /work/evidence/qt-runtime-overlay.json \
  /work/evidence/qt-runtime-overlay.json
COPY --from=qt-runtime-overlay-qualified \
  /work/config/release.json \
  /work/config/release.json
COPY --from=qt-runtime-overlay-qualified \
  /work/config/schemas/release.schema.json \
  /work/config/schemas/qt-runtime-overlay.schema.json \
  /work/config/schemas/
RUN --network=none set -eu; \
    qt_root="/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE"; \
    sysroot="/opt/crossforge/sysroots/el8/$QT_TARGET_ARCH"; \
    consumer="/work/build/qt-$QT_TARGET_ARCH/target-consumer-qualification/build/qt-target-consumer"; \
    compiler="/opt/crossforge/targets/$QT_TARGET_TRIPLE/bin/$QT_TARGET_TRIPLE-gcc"; \
    test -d "$qt_root/usr"; \
    test -x "$consumer"; \
    test -x "$compiler"; \
    test ! -e /runtime-root/usr/lib/libQt6Core.so.6; \
    cp -a "$qt_root/usr/." /runtime-root/usr/; \
    install -D -m 0755 "$consumer" \
      /runtime-root/opt/crossforge-qualification/qt/qt-target-consumer; \
    "$compiler" --sysroot="$sysroot" -O2 -Wall -Wextra -Werror \
      -Wl,--enable-new-dtags,-rpath,/usr/lib:/usr/lib64 \
      -o /runtime-root/opt/crossforge-qualification/qt/qt-plugin-probe \
      /work/scripts/qt-plugin-probe.c -ldl; \
    test -x /runtime-root/opt/crossforge-qualification/qt/qt-plugin-probe; \
    cp -a "$sysroot"/usr/lib64/libavcodec.so* /runtime-root/usr/lib/; \
    cp -a "$sysroot"/usr/lib64/libavformat.so* /runtime-root/usr/lib/; \
    cp -a "$sysroot"/usr/lib64/libavutil.so* /runtime-root/usr/lib/; \
    cp -a "$sysroot"/usr/lib64/libswresample.so* /runtime-root/usr/lib/; \
    cp -a "$sysroot"/usr/lib64/libswscale.so* /runtime-root/usr/lib/; \
    cp -a "$sysroot"/usr/lib64/libxcb-cursor.so* /runtime-root/usr/lib64/; \
    install -d -m 0755 /runtime-root/.crossforge

FROM qt-runtime-artifacts-staged AS qt-target-runtime-x86_64-qualified
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
COPY config/schemas/qt-target-build.schema.json \
  config/schemas/qt-target-runtime.schema.json /work/config/schemas/
COPY --chmod=0755 scripts/loader_evidence.py \
  scripts/run-qt-target-runtime.py /work/scripts/
RUN --network=none test "$QT_TARGET_ARCH" = x86_64 \
    && test "$QT_TARGET_TRIPLE" = x86_64-unknown-linux-gnu \
    && /usr/libexec/platform-python /work/scripts/run-qt-target-runtime.py \
      --arch "$QT_TARGET_ARCH" \
      --triple "$QT_TARGET_TRIPLE" \
      --runtime-root /runtime-root \
      --release /work/config/release.json \
      --build-evidence \
        "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE/qt-target-build.json" \
      --overlay-evidence /work/evidence/qt-runtime-overlay.json \
      --output /work/evidence/qt-target-runtime.json

FROM scratch AS qt-target-runtime-x86_64-evidence
COPY --from=qt-target-runtime-x86_64-qualified \
  /work/evidence/qt-runtime-overlay.json /
COPY --from=qt-target-runtime-x86_64-qualified \
  /work/evidence/qt-target-runtime.json /

FROM qt-runtime-artifacts-staged AS qt-target-runtime-aarch64-qualified
ARG QT_TARGET_ARCH
ARG QT_TARGET_TRIPLE
ARG QEMU_EXECUTOR_CPU
ARG QEMU_EXECUTOR_UNAME_RELEASE
COPY config/schemas/qt-target-build.schema.json \
  config/schemas/qt-target-runtime.schema.json /work/config/schemas/
COPY --chmod=0755 scripts/loader_evidence.py \
  scripts/run-qt-target-runtime.py /work/scripts/
RUN --network=none \
    --mount=type=bind,from=crossforge_qemu_validated,source=/usr/local/libexec/crossforge/qemu-aarch64,target=/runtime-root/.crossforge/qemu-aarch64,ro \
    test "$QT_TARGET_ARCH" = aarch64 \
    && test "$QT_TARGET_TRIPLE" = aarch64-unknown-linux-gnu \
    && /usr/libexec/platform-python /work/scripts/run-qt-target-runtime.py \
      --arch "$QT_TARGET_ARCH" \
      --triple "$QT_TARGET_TRIPLE" \
      --runtime-root /runtime-root \
      --release /work/config/release.json \
      --build-evidence \
        "/opt/crossforge/qualification/qt/6.8.4/targets/$QT_TARGET_TRIPLE/qt-target-build.json" \
      --overlay-evidence /work/evidence/qt-runtime-overlay.json \
      --qemu /runtime-root/.crossforge/qemu-aarch64 \
      --qemu-cpu "$QEMU_EXECUTOR_CPU" \
      --qemu-uname-release "$QEMU_EXECUTOR_UNAME_RELEASE" \
      --output /work/evidence/qt-target-runtime.json

FROM scratch AS qt-target-runtime-aarch64-evidence
COPY --from=qt-target-runtime-aarch64-qualified \
  /work/evidence/qt-runtime-overlay.json /
COPY --from=qt-target-runtime-aarch64-qualified \
  /work/evidence/qt-target-runtime.json /

FROM scratch AS qt-native-runtime-root
COPY --from=qt-runtime-artifacts-staged /runtime-root/ /
COPY --from=qt-runtime-artifacts-staged \
  /work/evidence/qt-runtime-overlay.json \
  /opt/crossforge-qualification/qt/evidence/qt-runtime-overlay.json
COPY --from=qt-runtime-artifacts-staged \
  /opt/crossforge/qualification/qt/6.8.4/targets/aarch64-unknown-linux-gnu/qt-target-build.json \
  /opt/crossforge-qualification/qt/evidence/qt-target-build.json
