#!/usr/bin/env python3
"""Verify a pinned Sigstore TUF root rotation and trusted-root target."""

import argparse
import base64
import binascii
import datetime
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_SIGNATURE_RE = re.compile(r"^(?:[0-9a-f]{2})+$")
P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_A = P256_P - 3
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
P256_SPKI_PREFIX = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d03010703420004"
)


class TUFError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TUFError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise TUFError("invalid JSON in %s: %s" % (path, error)) from error


def canonical_string(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def canonical_text(value):
    if isinstance(value, str):
        return canonical_string(value)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if type(value) is int:
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_text(item) for item in value) + "]"
    if isinstance(value, dict):
        require(
            all(isinstance(key, str) for key in value),
            "canonical TUF object contains a non-string key",
        )
        return "{" + ",".join(
            canonical_string(key) + ":" + canonical_text(value[key])
            for key in sorted(value)
        ) + "}"
    raise TUFError(
        "canonical TUF object contains an unsupported JSON value"
    )


def canonical_bytes(document):
    return canonical_text(document).encode("utf-8")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def decode_base64_evidence(source, destination, label):
    try:
        encoded = b"".join(Path(source).read_bytes().split())
        payload = base64.b64decode(encoded, validate=True)
    except (OSError, binascii.Error, ValueError) as error:
        raise TUFError("invalid base64 %s" % label) from error
    require(payload, "%s is empty" % label)
    Path(destination).write_bytes(payload)
    return Path(destination)


def validate_envelope(document, metadata_type, version, label):
    require(
        isinstance(document, dict)
        and set(document) == {"signatures", "signed"},
        "%s envelope fields differ" % label,
    )
    signed = document["signed"]
    require(isinstance(signed, dict), "%s signed body differs" % label)
    require(
        signed.get("_type") == metadata_type,
        "%s metadata type differs" % label,
    )
    require(
        type(signed.get("version")) is int
        and signed["version"] == version,
        "%s metadata version differs" % label,
    )
    signatures = document["signatures"]
    require(
        isinstance(signatures, list) and signatures,
        "%s signatures are missing" % label,
    )
    keyids = []
    for signature in signatures:
        require(
            isinstance(signature, dict)
            and set(signature) == {"keyid", "sig"}
            and isinstance(signature["keyid"], str)
            and isinstance(signature["sig"], str)
            and (
                signature["sig"] == ""
                or HEX_SIGNATURE_RE.match(signature["sig"])
            ),
            "%s signature record differs" % label,
        )
        keyids.append(signature["keyid"])
    require(
        len(keyids) == len(set(keyids)),
        "%s repeats a signature key" % label,
    )
    return document


def validate_root(document, version):
    label = "TUF root %d" % version
    validate_envelope(document, "root", version, label)
    signed = document["signed"]
    required_fields = {
            "_type",
            "consistent_snapshot",
            "expires",
            "keys",
            "roles",
            "spec_version",
            "version",
        }
    optional_fields = {
        "x-tuf-on-ci-expiry-period",
        "x-tuf-on-ci-signing-period",
    }
    require(
        required_fields.issubset(set(signed))
        and set(signed).issubset(required_fields | optional_fields),
        "%s signed fields differ" % label,
    )
    for field in optional_fields & set(signed):
        require(
            type(signed[field]) is int and signed[field] >= 1,
            "%s %s differs" % (label, field),
        )
    require(
        signed["consistent_snapshot"] is True,
        "%s does not require consistent snapshots" % label,
    )
    keys = signed["keys"]
    roles = signed["roles"]
    require(
        isinstance(keys, dict) and keys,
        "%s key set is empty" % label,
    )
    require(
        isinstance(roles, dict)
        and set(roles) == {"root", "snapshot", "targets", "timestamp"},
        "%s roles differ" % label,
    )
    for role_name, role in roles.items():
        require(
            isinstance(role, dict)
            and "keyids" in role
            and "threshold" in role
            and isinstance(role["keyids"], list)
            and role["keyids"]
            and len(role["keyids"]) == len(set(role["keyids"]))
            and all(keyid in keys for keyid in role["keyids"])
            and type(role["threshold"]) is int
            and 1 <= role["threshold"] <= len(role["keyids"]),
            "%s %s role differs" % (label, role_name),
        )
    for keyid, key in keys.items():
        require(
            isinstance(keyid, str)
            and isinstance(key, dict)
            and key.get("keytype")
            in ("ecdsa", "ecdsa-sha2-nistp256")
            and key.get("scheme") == "ecdsa-sha2-nistp256"
            and isinstance(key.get("keyval"), dict)
            and set(key["keyval"]) == {"public"}
            and key["keyval"]["public"].startswith(
                "-----BEGIN PUBLIC KEY-----\n"
            )
            and key["keyval"]["public"].endswith(
                "-----END PUBLIC KEY-----\n"
            ),
            "%s key %s differs" % (label, keyid),
        )
    return document


def parse_expiration(value, label):
    require(isinstance(value, str), "%s expiration differs" % label)
    try:
        return datetime.datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc)
    except ValueError as error:
        raise TUFError("%s expiration differs" % label) from error


def inverse_mod(value, modulus):
    require(value % modulus, "ECDSA inverse is undefined")
    return pow(value, modulus - 2, modulus)


def point_add(left, right):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % P256_P == 0:
        return None
    if left == right:
        slope = (
            (3 * x1 * x1 + P256_A)
            * inverse_mod(2 * y1, P256_P)
        ) % P256_P
    else:
        slope = (
            (y2 - y1) * inverse_mod(x2 - x1, P256_P)
        ) % P256_P
    x3 = (slope * slope - x1 - x2) % P256_P
    y3 = (slope * (x1 - x3) - y1) % P256_P
    return x3, y3


def jacobian_double(point):
    x, y, z = point
    if z == 0 or y == 0:
        return 0, 1, 0
    yy = y * y % P256_P
    yyyy = yy * yy % P256_P
    zz = z * z % P256_P
    slope = (3 * x * x + P256_A * zz * zz) % P256_P
    offset = 4 * x * yy % P256_P
    x3 = (slope * slope - 2 * offset) % P256_P
    y3 = (slope * (offset - x3) - 8 * yyyy) % P256_P
    z3 = 2 * y * z % P256_P
    return x3, y3, z3


def jacobian_add(left, right):
    x1, y1, z1 = left
    x2, y2, z2 = right
    if z1 == 0:
        return right
    if z2 == 0:
        return left
    z1z1 = z1 * z1 % P256_P
    z2z2 = z2 * z2 % P256_P
    u1 = x1 * z2z2 % P256_P
    u2 = x2 * z1z1 % P256_P
    s1 = y1 * z2 * z2z2 % P256_P
    s2 = y2 * z1 * z1z1 % P256_P
    if u1 == u2:
        return jacobian_double(left) if s1 == s2 else (0, 1, 0)
    difference = (u2 - u1) % P256_P
    doubled = 2 * difference % P256_P
    square = doubled * doubled % P256_P
    product = difference * square % P256_P
    rise = 2 * (s2 - s1) % P256_P
    anchor = u1 * square % P256_P
    x3 = (rise * rise - product - 2 * anchor) % P256_P
    y3 = (rise * (anchor - x3) - 2 * s1 * product) % P256_P
    z3 = (
        ((z1 + z2) * (z1 + z2) - z1z1 - z2z2)
        * difference
    ) % P256_P
    return x3, y3, z3


def jacobian_scalar_multiply(scalar, point):
    require(type(scalar) is int and scalar >= 0, "invalid curve scalar")
    result = (0, 1, 0)
    addend = (point[0], point[1], 1)
    while scalar:
        if scalar & 1:
            result = jacobian_add(result, addend)
        addend = jacobian_double(addend)
        scalar >>= 1
    return result


def jacobian_to_affine(point):
    x, y, z = point
    if z == 0:
        return None
    inverse = inverse_mod(z, P256_P)
    inverse_squared = inverse * inverse % P256_P
    return (
        x * inverse_squared % P256_P,
        y * inverse_squared * inverse % P256_P,
    )


def scalar_multiply(scalar, point):
    return jacobian_to_affine(jacobian_scalar_multiply(scalar, point))


def public_point(pem):
    require(isinstance(pem, str), "TUF public key is not text")
    lines = pem.strip().splitlines()
    require(
        len(lines) >= 3
        and lines[0] == "-----BEGIN PUBLIC KEY-----"
        and lines[-1] == "-----END PUBLIC KEY-----",
        "TUF public key PEM differs",
    )
    try:
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (binascii.Error, ValueError) as error:
        raise TUFError("TUF public key PEM differs") from error
    require(
        len(der) == len(P256_SPKI_PREFIX) + 64
        and der.startswith(P256_SPKI_PREFIX),
        "TUF public key is not a canonical P-256 SPKI",
    )
    point = (
        int.from_bytes(der[-64:-32], "big"),
        int.from_bytes(der[-32:], "big"),
    )
    x, y = point
    require(
        0 <= x < P256_P
        and 0 <= y < P256_P
        and (y * y - (x * x * x + P256_A * x + P256_B))
        % P256_P
        == 0
        and scalar_multiply(P256_N, point) is None,
        "TUF P-256 public key point is invalid",
    )
    return point


def der_length(value, offset):
    require(offset < len(value), "truncated ECDSA signature length")
    first = value[offset]
    if first < 128:
        return first, offset + 1
    count = first & 0x7F
    require(
        1 <= count <= 2 and offset + 1 + count <= len(value),
        "invalid ECDSA signature length",
    )
    length = int.from_bytes(value[offset + 1:offset + 1 + count], "big")
    require(length >= 128, "non-canonical ECDSA signature length")
    return length, offset + 1 + count


def der_integer(value, offset):
    require(offset < len(value) and value[offset] == 0x02, "ECDSA integer tag differs")
    length, start = der_length(value, offset + 1)
    end = start + length
    require(
        length >= 1
        and end <= len(value)
        and value[start] & 0x80 == 0
        and not (
            length > 1
            and value[start] == 0
            and value[start + 1] & 0x80 == 0
        ),
        "ECDSA integer encoding differs",
    )
    return int.from_bytes(value[start:end], "big"), end


def parse_ecdsa_signature(value):
    require(value and value[0] == 0x30, "ECDSA signature sequence differs")
    length, offset = der_length(value, 1)
    require(offset + length == len(value), "ECDSA signature length differs")
    r, offset = der_integer(value, offset)
    s, offset = der_integer(value, offset)
    require(offset == len(value), "ECDSA signature has trailing data")
    require(
        1 <= r < P256_N and 1 <= s < P256_N,
        "ECDSA signature scalar differs",
    )
    return r, s


def verify_ecdsa_sha256(public_key, signature, payload):
    point = public_point(public_key)
    r, s = parse_ecdsa_signature(signature)
    digest = int.from_bytes(hashlib.sha256(payload).digest(), "big")
    inverse = inverse_mod(s, P256_N)
    candidate = point_add(
        scalar_multiply((digest * inverse) % P256_N, P256_G),
        scalar_multiply((r * inverse) % P256_N, point),
    )
    return candidate is not None and candidate[0] % P256_N == r


def verify_signatures(document, trusted_root, role_name, label):
    role = trusted_root["signed"]["roles"][role_name]
    keys = trusted_root["signed"]["keys"]
    signatures = {
        signature["keyid"]: signature["sig"]
        for signature in document["signatures"]
        if signature["sig"]
    }
    authorized = set(role["keyids"])
    valid = []
    payload = canonical_bytes(document["signed"])
    for keyid in sorted(authorized & set(signatures)):
        if verify_ecdsa_sha256(
            keys[keyid]["keyval"]["public"],
            bytes.fromhex(signatures[keyid]),
            payload,
        ):
            valid.append(keyid)
    require(
        len(valid) >= role["threshold"],
        "%s has %d valid %s signatures; threshold is %d"
        % (label, len(valid), role_name, role["threshold"]),
    )
    return valid


def verify_root_chain(
    root_directory,
    initial_version,
    final_version,
    initial_root_sha256,
    targets_path,
    targets_sha256,
    trusted_root_path,
    trusted_root_sha256,
    now=None,
):
    for digest, label in (
        (initial_root_sha256, "initial root SHA256"),
        (targets_sha256, "targets SHA256"),
        (trusted_root_sha256, "trusted root SHA256"),
    ):
        require(
            isinstance(digest, str) and SHA256_RE.match(digest),
            "%s is invalid" % label,
        )
    require(
        type(initial_version) is int
        and type(final_version) is int
        and 1 <= initial_version <= final_version,
        "TUF root version range differs",
    )
    initial_path = Path(root_directory) / (
        "%d.root.json" % initial_version
    )
    require(
        file_sha256(initial_path) == initial_root_sha256,
        "initial TUF root digest differs",
    )
    previous = validate_root(load_json(initial_path), initial_version)
    verify_signatures(
        previous, previous, "root", "initial TUF root"
    )
    for version in range(initial_version + 1, final_version + 1):
        path = Path(root_directory) / ("%d.root.json" % version)
        current = validate_root(load_json(path), version)
        verify_signatures(
            current,
            previous,
            "root",
            "TUF root %d signed by root %d" % (version, version - 1),
        )
        verify_signatures(
            current,
            current,
            "root",
            "TUF root %d self-signature" % version,
        )
        previous = current

    current_time = now or datetime.datetime.now(datetime.timezone.utc)
    require(
        current_time < parse_expiration(
            previous["signed"]["expires"], "final TUF root"
        ),
        "final TUF root metadata is expired",
    )
    require(
        file_sha256(targets_path) == targets_sha256,
        "TUF targets digest differs",
    )
    targets = load_json(targets_path)
    targets_version = targets.get("signed", {}).get("version")
    require(
        type(targets_version) is int and targets_version >= 1,
        "TUF targets version differs",
    )
    validate_envelope(
        targets, "targets", targets_version, "TUF targets"
    )
    require(
        current_time
        < parse_expiration(
            targets["signed"].get("expires"), "TUF targets"
        ),
        "TUF targets metadata is expired",
    )
    verify_signatures(
        targets, previous, "targets", "TUF targets"
    )
    target = targets["signed"].get("targets", {}).get(
        "trusted_root.json"
    )
    require(
        isinstance(target, dict)
        and target.get("hashes", {}).get("sha256")
        == trusted_root_sha256
        and type(target.get("length")) is int,
        "trusted root target metadata differs",
    )
    require(
        file_sha256(trusted_root_path) == trusted_root_sha256
        and Path(trusted_root_path).stat().st_size == target["length"],
        "trusted root target content differs",
    )
    return {
        "initial_root_version": initial_version,
        "final_root_version": final_version,
        "targets_version": targets_version,
        "trusted_root_sha256": trusted_root_sha256,
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--root-directory", type=Path, required=True)
    result.add_argument("--initial-version", type=int, required=True)
    result.add_argument("--final-version", type=int, required=True)
    result.add_argument("--initial-root-sha256", required=True)
    result.add_argument("--targets", type=Path, required=True)
    result.add_argument("--targets-sha256", required=True)
    result.add_argument("--trusted-root", type=Path, required=True)
    result.add_argument("--trusted-root-sha256", required=True)
    result.add_argument("--base64-envelopes", action="store_true")
    return result


def verify_arguments(arguments):
    if not arguments.base64_envelopes:
        return verify_root_chain(
            arguments.root_directory,
            arguments.initial_version,
            arguments.final_version,
            arguments.initial_root_sha256,
            arguments.targets,
            arguments.targets_sha256,
            arguments.trusted_root,
            arguments.trusted_root_sha256,
        )
    with tempfile.TemporaryDirectory(
        prefix="crossforge-sigstore-tuf-"
    ) as directory:
        root = Path(directory)
        for version in range(
            arguments.initial_version, arguments.final_version + 1
        ):
            decode_base64_evidence(
                arguments.root_directory
                / ("%d.root.json.b64" % version),
                root / ("%d.root.json" % version),
                "TUF root %d" % version,
            )
        targets = decode_base64_evidence(
            arguments.targets,
            root / "targets.json",
            "TUF targets",
        )
        trusted_root = decode_base64_evidence(
            arguments.trusted_root,
            root / "trusted_root.json",
            "Sigstore trusted root",
        )
        return verify_root_chain(
            root,
            arguments.initial_version,
            arguments.final_version,
            arguments.initial_root_sha256,
            targets,
            arguments.targets_sha256,
            trusted_root,
            arguments.trusted_root_sha256,
        )


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        result = verify_arguments(arguments)
    except (KeyError, OSError, TUFError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "valid Sigstore TUF root %d->%d, targets %d: %s"
        % (
            result["initial_root_version"],
            result["final_root_version"],
            result["targets_version"],
            result["trusted_root_sha256"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
