# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

FROM crossforge_host_common AS sbom-generator-source
ARG SBOM_GENERATOR_SOURCE_COMPONENT_SHA256
WORKDIR /work
COPY config/generated/components/sources/sbom-generator.json \
  /work/config/sbom-generator.json
COPY keys/CRAZY-MAX-RELEASE-KEY.asc /work/keys/CRAZY-MAX-RELEASE-KEY.asc
COPY evidence/github/buildkit-syft-scanner-v1.12.0.tag.json.b64 \
  /work/evidence/buildkit-syft-scanner-v1.12.0.tag.json.b64
COPY --chmod=0755 scripts/release_component.py scripts/validate-release.py \
  scripts/fetch-release-source.py scripts/prepare-sbom-generator-source.py \
  /work/scripts/
RUN /usr/libexec/platform-python /work/scripts/fetch-release-source.py \
      sbom-generator \
      --component-file /work/config/sbom-generator.json \
      --expected-component sources/sbom-generator \
      --expected-scope supply \
      --expected-sha256 "$SBOM_GENERATOR_SOURCE_COMPONENT_SHA256" \
      --output /work/buildkit-syft-scanner-1.12.0.tar.gz
RUN --network=none base64 --decode \
      /work/evidence/buildkit-syft-scanner-v1.12.0.tag.json.b64 \
      > /work/buildkit-syft-scanner-v1.12.0.tag.json \
    && /usr/libexec/platform-python \
      /work/scripts/prepare-sbom-generator-source.py \
      --component /work/config/sbom-generator.json \
      --component-sha256 "$SBOM_GENERATOR_SOURCE_COMPONENT_SHA256" \
      --archive /work/buildkit-syft-scanner-1.12.0.tar.gz \
      --tag-evidence /work/buildkit-syft-scanner-v1.12.0.tag.json \
      --key /work/keys/CRAZY-MAX-RELEASE-KEY.asc \
      --destination /out

FROM scratch AS sbom-generator-source-output
COPY --from=sbom-generator-source /out/ /
