"""Small, Python 3.6-compatible content identity primitives.

These functions validate data and bytes, not producer trust or qualification.
Callers must supply independently selected expected inputs and trusted digests.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class IdentityError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise IdentityError(message)


def exact_fields(value, names, label):
    require(type(value) is dict, "%s must be an object" % label)
    require(all(type(key) is str for key in value), "%s keys must be strings" % label)
    require(set(value) == set(names), "%s fields differ: missing=%s unknown=%s" % (
        label, sorted(set(names) - set(value)), sorted(set(value) - set(names))))


def digest_value(value, label, oci=False):
    pattern = OCI_DIGEST if oci else HEX_SHA256
    require(type(value) is str and pattern.fullmatch(value), "%s is invalid" % label)
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _invalid_number(value):
    raise IdentityError("non-finite JSON number: %s" % value)


def _finite_float(value):
    parsed = float(value)
    require(math.isfinite(parsed), "non-finite JSON number: %s" % value)
    return parsed


def parse_json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_number, parse_float=_finite_float)
    except (UnicodeError, ValueError) as error:
        raise IdentityError("invalid JSON: %s" % error) from error


def load_json(path):
    try:
        return parse_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise IdentityError("cannot read JSON %s: %s" % (path, error)) from error


def _json_value(value):
    if type(value) is dict:
        require(all(type(key) is str for key in value), "JSON keys must be strings")
        for child in value.values():
            _json_value(child)
    elif type(value) is list:
        for child in value:
            _json_value(child)
    else:
        require(type(value) in (str, int, float, bool, type(None)),
                "unsupported JSON value type: %s" % type(value).__name__)
        require(type(value) is not float or math.isfinite(value), "non-finite JSON number")


def canonical_bytes(value):
    _json_value(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise IdentityError("cannot encode canonical JSON: %s" % error) from error


def content_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def relative_path(value):
    require(type(value) is str and value and not value.startswith("/"),
            "material path must be relative")
    require("\\" not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "material path contains unsupported characters")
    require(all(part not in ("", ".", "..", ".git") for part in value.split("/")),
            "material path contains an unsafe segment")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise IdentityError("material path is not valid UTF-8") from error
    return value


def file_record(root, relative):
    """Hash one regular source file; timestamps are observation guards, not identity."""
    relative_path(relative)
    root = Path(root).resolve()
    path = root
    try:
        for part in relative.split("/"):
            path = path / part
            require(not path.is_symlink(), "symlink material is unsupported: %s" % relative)
        require(stat.S_ISREG(path.lstat().st_mode),
                "material is not a regular file: %s" % relative)
        with os.fdopen(os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode), "material is not a regular file: %s" % relative)
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())
        observed = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
        require(all(getattr(before, key) == getattr(after, key) == getattr(observed, key)
                    for key in fields), "material changed while hashing: %s" % relative)
    except OSError as error:
        raise IdentityError("cannot read material %s: %s" % (relative, error)) from error
    return {"path": relative, "sha256": digest.hexdigest(),
            "mode": "%04o" % stat.S_IMODE(before.st_mode)}
