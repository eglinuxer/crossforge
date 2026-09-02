# Supply-chain Evidence

Files below `oci/`, `git/`, and `sigstore/` are base64 envelopes of exact upstream bytes.
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

The CPython bundles are content-addressed archival evidence only. The validator
checks their structure and binds their message/Rekor digests to the selected
tarballs, but does not yet verify the message signature, Fulcio certificate
chain or identity, Rekor SET/inclusion proof, or TSA. Accordingly release.json
must label them `archived-unverified`; changing that label is a schema error.
