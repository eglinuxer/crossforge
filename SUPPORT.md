# Support contract

Crossforge is an independently maintained, GTS-derived cross SDK built from
Rocky Linux rebuild sources. It is not an official or certified Red Hat,
Rocky Linux, Python, Qt, vcpkg or Docker product.

## Supported delivery and targets

- Tool host and OCI platform: `linux/amd64`.
- Stable image channel: `ghcr.io/eglinuxer/crossforge:gts15-el8` after the
  first public release.
- Compiler targets: `x86_64-unknown-linux-gnu` and
  `aarch64-unknown-linux-gnu`.
- Runtime baseline: the frozen EL8 ABI and the release-pinned Rocky Linux 8.10
  roots recorded in `config/release.json`.
- Build systems: CMake, Meson, Autotools and Make through explicit launcher
  environment selection; build-system-independent DEB/RPM staging through
  `crossforge package`.
- Dependency integration: the shipped vcpkg release and Crossforge triplets.
  Only the checked curated tiers are qualification evidence; arbitrary ports
  are not guaranteed.

Target programs are not supported as tool-host programs. Cross stages do not
use implicit binfmt or execute target artifacts. Run and test produced binaries
on the intended EL8 target environment.

## Python rows

The image carries cross SDKs for CPython 3.9 through 3.14. The upstream support
status is release-bound and exposed in `config/release.json`:

- 3.9 is EOL and retained only for compatibility;
- 3.10 through 3.12 are in upstream security-fix mode;
- 3.13 and 3.14 are in upstream bug-fix mode at the current release pins.

These labels describe upstream status at the pinned release. They are not a
promise that Crossforge backports fixes after an upstream line reaches EOL.

## What to include in a public defect

Use the structured bug form for non-sensitive, reproducible defects. Always
include:

- the immutable OCI digest, not only `gts15-el8`;
- `crossforge info --json`;
- target, Python minor, vcpkg linkage and package format where applicable;
- container engine/version and host architecture;
- a minimal reproducer and complete error output with secrets removed.

Feature requests may use a normal public issue. Questions that contain private
source or undisclosed security impact must not be posted publicly; follow
[SECURITY.md](SECURITY.md).

## Compatibility and updates

Version tags are immutable. The stable channel may advance to a newly
qualified version through promotion or be moved back to a previously published
immutable version through the dedicated evidence-verifying rollback workflow.
Rollback consumes the durable Release assets, so it remains available after
the originating Actions artifacts expire. A release never rebuilds an existing
version or overwrites its bytes.

Security fixes, CPython patch releases, GTS patches, vcpkg revisions and Rocky
errata require explicit lock/evidence changes and requalification. ABI,
package, source, license, SBOM/provenance and known-failure differences remain
fail-closed review inputs rather than automatic compatibility claims.
