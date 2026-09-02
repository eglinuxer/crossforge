# Supply-chain Evidence

Files below `oci/`, `git/`, `github/`, `gpg/`, and `sigstore/` are base64 envelopes of exact upstream bytes.
Base64 keeps OCI manifest digests and Git object IDs reproducible even when a
text editor changes line endings. `scripts/validate-supply-chain-evidence.py`
decodes each file, recomputes its content identity, and verifies every
relationship recorded in `config/release.json`.

The QEMU tag envelope archives the signed annotated tag, but Crossforge does
not yet carry a QEMU maintainer keyring or claim local OpenPGP trust
verification. The validator does prove that the archived tag object names the
archived commit and that the pinned binfmt provenance used that tag in its
checkout step. Refresh evidence only as part of an audited release-input
update; never edit decoded JSON or Git object bodies by hand.

Ninja uses a lightweight tag and its GitHub release is not immutable. The
`github/` envelopes therefore preserve the exact tag-ref mapping and release
asset metadata, while `git/` preserves the commit object. Crossforge treats
the full commit plus independent archive and extracted-binary hashes as the
trust boundary; it does not claim that Ninja 1.13.2 is upstream-signed.

The zstd release input has a separate, locked trust boundary. The checked-in
release key is content- and fingerprint-locked, `gpg/` preserves the detached
signature over the selected tarball, and normal source preparation must verify
that signature offline after verifying the downloaded size and SHA256. The Git
envelopes preserve the exact signed annotated tag and target commit bytes; the
supply validator recomputes their Git object IDs and tag-to-commit relationship.
It also binds the selected BSD-3-Clause license and the upstream `LICENSE` and
`COPYING` digests. Structural Git evidence is not a substitute for the detached
tarball signature, and no QEMU OpenPGP claim is implied by the zstd key.

The CPython bundles are content-addressed archival evidence only. The validator
checks their structure and binds their message/Rekor digests to the selected
tarballs, but does not yet verify the message signature, Fulcio certificate
chain or identity, Rekor SET/inclusion proof, or TSA. Accordingly release.json
must label them `archived-unverified`; changing that label is a schema error.

## ABI evidence

Files below `abi/` are maintainer-generated inventories, not public ABI
allowlists. Export the four release-bound roots with the cache-only
`abi-export` Bake target, overriding its output to a new tar review archive,
then extract that archive into a new directory. Run
`scripts/extract-abi-inventory.py` separately for each clean OCI root and
locked sysroot; the extractor binds provider bytes, the fixed provider
manifest, the release-derived source identity, GNU readelf commands, and the
canonical inventory digest.

Only a clean inventory at the fixed path
`evidence/abi/el8-{arch}-clean.json` can be promoted. After reviewing its exact
symbol set and comparing the locked-sysroot inventory, invoke
`scripts/freeze-abi-baseline.py` with `--arch <arch>` and
`--accept-inventory-sha256 <canonical-digest>`. The repeated digest is the
explicit approval boundary; the tool creates `abi/el8/{arch}.json` once and
never replaces an existing baseline. Run `scripts/validate-frozen-abi.py` to
recheck the complete two-target matrix, release identities, extraction
provenance, exact clean baselines, and the locked-sysroot ABI diff; CI runs the
same read-only gate.
