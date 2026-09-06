# Security policy

## Reporting a vulnerability

Do not open a public issue for an undisclosed vulnerability, suspected secret
exposure, or private reproducer. Use the repository's **Report a
vulnerability** button to open a private GitHub security advisory:

<https://github.com/eglinuxer/crossforge/security/advisories/new>

Private Vulnerability Reporting is a mandatory release prerequisite. The
stable-promotion workflow fails before creating a Git tag, GitHub Release, or
stable OCI tag when that repository setting is disabled.

Include the following when it is safe to do so:

- the exact Crossforge version and OCI digest;
- affected target, Python minor, vcpkg linkage and package format;
- a minimal reproducer or affected path;
- expected and observed behavior;
- impact, exploit prerequisites and any known workaround;
- whether disclosure is time-sensitive.

Do not include credentials, private signing material, access tokens or
unrelated proprietary source. Maintainers will acknowledge the report through
the private advisory, reproduce it against an immutable digest, and coordinate
remediation and disclosure there. No fixed response-time SLA is promised.

## Supported release line

Crossforge has not published its first stable release. Before that release,
`main` is development software and has no security-support SLA. After release,
the current `gts15-el8` channel and immutable version tags explicitly listed as
supported in [SUPPORT.md](SUPPORT.md) are in scope. A bundled upstream runtime
that is already EOL does not acquire upstream security support merely because
Crossforge continues to carry it for compatibility.

## Supply-chain verification

Report any mismatch in image signatures, SLSA provenance, SPDX SBOM, source
binding, release-evidence archive, RPM signatures, ABI evidence or immutable
release assets through the same private channel. Preserve the original digest
and verification output; do not retag or republish the suspect artifact.
