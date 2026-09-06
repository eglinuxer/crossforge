#!/usr/bin/env python3
"""Shared exact-identity verification for explicitly expired OpenPGP keys."""

import calendar
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path


REJECTED_GPG_STATUS = {"BADSIG", "ERRSIG", "NO_PUBKEY", "REVKEYSIG"}


class SignatureError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SignatureError(message)


def file_identity(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "missing signature input")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def timestamp_epoch(value):
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        raise SignatureError("signature timestamp is invalid")


def gpg_status(command, home):
    environment = os.environ.copy()
    environment.update({"GNUPGHOME": str(home), "LANG": "C", "LC_ALL": "C"})
    process = subprocess.run(
        [str(value) for value in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=environment,
    )
    require(
        process.returncode == 0,
        "OpenPGP operation failed: %s"
        % (process.stdout + process.stderr)[-4000:],
    )
    return process.stdout, process.stderr


def key_fingerprints(gpg, key, home):
    output, _error = gpg_status(
        [
            gpg,
            "--no-options",
            "--batch",
            "--no-autostart",
            "--show-keys",
            "--with-colons",
            "--fingerprint",
            key,
        ],
        home,
    )
    return [
        fields[9].lower()
        for fields in (line.split(":") for line in output.splitlines())
        if fields[0] == "fpr" and len(fields) > 9
    ]


def verify_expired_key_signature(gpg, signed, signature, key, policy):
    required = {
        "key_sha256",
        "primary_fingerprint",
        "signing_fingerprint",
        "signing_key_expires_at",
        "signature_time",
        "status",
        "exception",
    }
    require(set(policy) == required, "expired-key signature policy differs")
    require(
        file_identity(key)["sha256"] == policy["key_sha256"],
        "OpenPGP key digest differs",
    )
    primary = policy["primary_fingerprint"].lower()
    signing = policy["signing_fingerprint"].lower()
    require(
        len(primary) == 40
        and len(signing) == 40
        and all(character in "0123456789abcdef" for character in primary + signing),
        "OpenPGP fingerprint is invalid",
    )
    with tempfile.TemporaryDirectory(prefix="crossforge-source-gpg-") as temporary:
        home = Path(temporary)
        os.chmod(str(home), 0o700)
        fingerprints = key_fingerprints(gpg, key, home)
        require(
            fingerprints
            and fingerprints[0] == primary
            and signing in fingerprints,
            "OpenPGP key fingerprints differ",
        )
        gpg_status(
            [
                gpg,
                "--no-options",
                "--batch",
                "--no-autostart",
                "--no-auto-key-retrieve",
                "--import",
                key,
            ],
            home,
        )
        output, _error = gpg_status(
            [
                gpg,
                "--no-options",
                "--batch",
                "--no-autostart",
                "--no-auto-key-retrieve",
                "--status-fd",
                "1",
                "--verify",
                signature,
                signed,
            ],
            home,
        )
    statuses = []
    for line in output.splitlines():
        if line.startswith("[GNUPG:] "):
            statuses.append(line[len("[GNUPG:] ") :].split())
    require(
        not any(fields and fields[0] in REJECTED_GPG_STATUS for fields in statuses),
        "OpenPGP signature was rejected",
    )
    signature_epoch = timestamp_epoch(policy["signature_time"])
    expiration_epoch = timestamp_epoch(policy["signing_key_expires_at"])
    valid = [fields for fields in statuses if fields and fields[0] == "VALIDSIG"]
    expired = [fields for fields in statuses if fields and fields[0] == "EXPKEYSIG"]
    require(
        len(valid) == 1
        and len(valid[0]) > 3
        and valid[0][1].lower() == signing
        and int(valid[0][3]) == signature_epoch
        and valid[0][-1].lower() == primary,
        "OpenPGP VALIDSIG identity differs",
    )
    require(
        len(expired) == 1
        and len(expired[0]) > 1
        and expired[0][1].lower() == signing[-16:]
        and expiration_epoch < signature_epoch,
        "OpenPGP expired-key status differs",
    )
    return {
        "status": policy["status"],
        "signature_time": policy["signature_time"],
        "exception": policy["exception"],
        "primary_fingerprint": primary,
        "signing_fingerprint": signing,
        "signing_key_expires_at": policy["signing_key_expires_at"],
    }
