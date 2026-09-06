# Crossforge source bundle

This archive contains the exact source materials used to build or qualify the
Crossforge SDK identified by `MANIFEST.json`.

- `sources/product/` contains corresponding source for shipped components.
- `sources/qualification/` contains source used only by release qualification.
- `sources/verification/` contains source for release-only verification tools,
  including the exact BuildKit Syft scanner used to generate public SBOMs.
- `verification/` contains detached signatures, checksum manifests, Sigstore
  bundles, and public keys needed to inspect upstream authentication.
- `metadata/` contains the source-stage reports and the complete Rocky SRPM
  content lock.

Every regular input file is listed by path, SHA256, size, role, component, and
origin in `MANIFEST.json`. The Crossforge project snapshot is bound to the
candidate source commit. The vcpkg registry snapshot retains its complete Git
history and version-database objects, but excludes the prebuilt tool injected
into the SDK copy.

The SBOM generator is not selected through the mutable `stable-1` tag at build
time. Release configuration pins its OCI index and amd64 manifest, signed
v1.12.0 tag object, peeled source commit, source archive, Apache-2.0 license and
maintainer signing key. Its source stage verifies the GitHub tag evidence and
the same detached OpenPGP payload offline before adding them to this archive.

The SDK's `/opt/crossforge/SOURCES.json` names the public source OCI image by
digest and records this archive's filename, SHA256 and byte size. Pull that OCI
reference, copy the archive from the container root, and verify the recorded
identity before extraction; the paired tag is only a convenience locator.

QEMU 10.2.3 and CMake 4.4.0 have cryptographically valid upstream signatures
made after the relevant OpenPGP key or signing subkey expired. The manifest and
release policy preserve those explicit exceptions; they must not be described
as signatures made by current keys.
