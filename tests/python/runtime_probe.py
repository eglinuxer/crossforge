#!/usr/bin/env python3
"""Exercise the target CPython runtime without relying on its test suite."""

from __future__ import annotations

import argparse
import bz2
import ctypes
import hashlib
import importlib
import json
import lzma
import multiprocessing
import os
import platform
import re
import socket
import sqlite3
import ssl
import sys
import sysconfig
import threading
import time
import uuid
import zlib


TARGETS = {
    "x86_64-unknown-linux-gnu": {
        "arch": "x86_64",
        "multiarch": "x86_64-linux-gnu",
    },
    "aarch64-unknown-linux-gnu": {
        "arch": "aarch64",
        "multiarch": "aarch64-linux-gnu",
    },
}
REQUIRED_IMPORTS = (
    "_bz2",
    "_ctypes",
    "_hashlib",
    "_lzma",
    "_multiprocessing",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "bz2",
    "ctypes",
    "fcntl",
    "hashlib",
    "lzma",
    "pty",
    "select",
    "sqlite3",
    "ssl",
    "termios",
    "tty",
    "uuid",
    "zlib",
)
PAYLOAD = b"crossforge-cpython-probe\x00" * 17


class ProbeError(RuntimeError):
    """A required target-runtime property was not observed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("core", "devices"), required=True)
    parser.add_argument("--target", choices=tuple(TARGETS), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--extension-dir", type=os.path.abspath)
    arguments = parser.parse_args()

    if re.fullmatch(r"3\.(?:11|13)\.[0-9]+", arguments.version) is None:
        parser.error("--version must be an implemented CPython 3.11/3.13 patch version")
    if arguments.mode == "core" and arguments.extension_dir is None:
        parser.error("--extension-dir is required in core mode")
    if arguments.mode == "devices" and arguments.extension_dir is not None:
        parser.error("--extension-dir is not valid in devices mode")
    return arguments


def validate_identity(target: str, version: str) -> dict[str, object]:
    target_data = TARGETS[target]
    arch = target_data["arch"]
    multiarch = target_data["multiarch"]
    minor = version.rsplit(".", 1)[0]
    compact_minor = minor.replace(".", "")
    prefix = f"/opt/crossforge/python/cp{compact_minor}/targets/{target}"
    toolchain = f"/opt/crossforge/targets/{target}"
    sysroot = f"/opt/crossforge/sysroots/el8/{arch}"

    require(
        sys.version_info[:3] == tuple(int(part) for part in version.split(".")),
        f"running Python {platform.python_version()} differs from {version}",
    )
    require(sys.implementation.name == "cpython", "runtime is not CPython")
    require(
        sys.implementation.cache_tag == f"cpython-{compact_minor}",
        "unexpected CPython cache tag",
    )
    require(sys.byteorder == "little", "target Python is not little-endian")
    require(ctypes.sizeof(ctypes.c_void_p) == 8, "target Python is not 64-bit")

    expected_config = {
        "ABIFLAGS": "",
        "BUILD_GNU_TYPE": "x86_64-pc-linux-gnu",
        "CC": f"{toolchain}/bin/{target}-gcc --sysroot={sysroot}",
        "CXX": f"{toolchain}/bin/{target}-g++ --sysroot={sysroot}",
        "EXT_SUFFIX": f".cpython-{compact_minor}-{multiarch}.so",
        "HOST_GNU_TYPE": target,
        "MULTIARCH": multiarch,
        "MACHDEP": "linux",
        "SOABI": f"cpython-{compact_minor}-{multiarch}",
        "VERSION": minor,
        "prefix": prefix,
    }
    observed_config = {
        key: sysconfig.get_config_var(key) for key in expected_config
    }
    for key, expected in expected_config.items():
        require(
            observed_config[key] == expected,
            f"sysconfig {key}={observed_config[key]!r}, expected {expected!r}",
        )

    require(sys.prefix == prefix, f"sys.prefix={sys.prefix!r}, expected {prefix!r}")
    require(sys.exec_prefix == prefix, "sys.exec_prefix differs from target prefix")
    require(
        sysconfig.get_platform() == f"linux-{arch}",
        "sysconfig platform differs from target architecture",
    )
    require(platform.machine() == arch, "runtime machine differs from target")
    require(sysconfig.get_config_var("Py_DEBUG") == 0, "debug Python is unsupported")
    config_vars = sysconfig.get_config_vars()
    if minor == "3.11":
        require(
            "Py_GIL_DISABLED" not in config_vars,
            "CPython 3.11 unexpectedly exposes Py_GIL_DISABLED",
        )
    else:
        require(
            "Py_GIL_DISABLED" in config_vars
            and config_vars["Py_GIL_DISABLED"] == 0,
            "CPython 3.13 must explicitly disable the free-threaded ABI",
        )
    config_args = sysconfig.get_config_var("CONFIG_ARGS")
    require(isinstance(config_args, str), "sysconfig CONFIG_ARGS is not text")
    for option in (
        f"--host={target}",
        f"--prefix={prefix}",
        "--with-computed-gotos=yes",
        "--with-ensurepip=no",
        "--disable-test-modules",
    ):
        require(option in config_args, f"sysconfig CONFIG_ARGS omits {option}")

    return {
        "arch": arch,
        "build_gnu_type": observed_config["BUILD_GNU_TYPE"],
        "cache_tag": sys.implementation.cache_tag,
        "cc": observed_config["CC"],
        "ext_suffix": observed_config["EXT_SUFFIX"],
        "host_gnu_type": observed_config["HOST_GNU_TYPE"],
        "multiarch": observed_config["MULTIARCH"],
        "platform": sysconfig.get_platform(),
        "prefix": prefix,
        "soabi": observed_config["SOABI"],
    }


def exercise_imports() -> list[str]:
    imported = []
    for module_name in REQUIRED_IMPORTS:
        importlib.import_module(module_name)
        imported.append(module_name)
    return imported


def exercise_libraries() -> dict[str, object]:
    for name, compressed in (
        ("zlib", zlib.compress(PAYLOAD, level=9)),
        ("bz2", bz2.compress(PAYLOAD, compresslevel=9)),
        ("lzma", lzma.compress(PAYLOAD, preset=9)),
    ):
        decompressors = {
            "zlib": zlib.decompress,
            "bz2": bz2.decompress,
            "lzma": lzma.decompress,
        }
        decompressor = decompressors[name]
        require(decompressor(compressed) == PAYLOAD, f"{name} round-trip failed")

    expected_sha256 = "822da7168e47d27301f5c747b5e678f593d60dc700049d33d3d3e1381dac1630"
    require(
        hashlib.sha256(b"crossforge-cpython-probe").hexdigest() == expected_sha256,
        "hashlib SHA-256 result differs",
    )
    openssl_sha256 = importlib.import_module("_hashlib").openssl_sha256
    require(
        openssl_sha256(b"crossforge-cpython-probe").hexdigest() == expected_sha256,
        "_hashlib OpenSSL SHA-256 result differs",
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    require(context.minimum_version != ssl.TLSVersion.SSLv3, "unsafe SSL minimum")
    require(ssl.OPENSSL_VERSION_NUMBER > 0, "OpenSSL version is unavailable")

    libc = ctypes.CDLL(None)
    libc.strlen.argtypes = (ctypes.c_char_p,)
    libc.strlen.restype = ctypes.c_size_t
    require(libc.strlen(b"crossforge") == 10, "ctypes libc call failed")

    with sqlite3.connect(":memory:") as database:
        database.execute("create table probe (id integer primary key, value text not null)")
        database.execute("insert into probe(value) values (?)", ("crossforge-交叉编译",))
        row = database.execute("select id, value from probe").fetchone()
    require(row == (1, "crossforge-交叉编译"), "SQLite round-trip failed")

    generated_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "https://crossforge.dev/cpython-probe")
    )
    require(
        generated_uuid == "d2222479-a666-5841-bee6-944f95190b64",
        "UUID result differs",
    )
    return {
        "compression_roundtrips": ["bz2", "lzma", "zlib"],
        "ctypes_strlen": 10,
        "hashlib_sha256": expected_sha256,
        "openssl": ssl.OPENSSL_VERSION,
        "sqlite": sqlite3.sqlite_version,
        "uuid5": generated_uuid,
    }


def exercise_hash_algorithm() -> dict[str, object]:
    info = sys.hash_info
    require(info.algorithm == "siphash13", "CPython hash algorithm is not siphash13")
    require(info.hash_bits == 64, "siphash13 output is not 64-bit")
    require(info.seed_bits == 128, "siphash13 seed is not 128-bit")
    return {
        "algorithm": info.algorithm,
        "hash_bits": info.hash_bits,
        "seed_bits": info.seed_bits,
    }


def exercise_threading() -> dict[str, object]:
    gate = threading.Event()
    lock = threading.Lock()
    result: list[int] = []

    def worker() -> None:
        with lock:
            result.append(sum(range(101)))
        gate.set()

    thread = threading.Thread(target=worker, name="crossforge-probe")
    thread.start()
    thread.join(timeout=10)
    require(not thread.is_alive(), "thread did not terminate")
    require(gate.is_set(), "threading event was not signalled")
    require(result == [5050], "thread produced the wrong result")
    return {"event": True, "result": result[0]}


def exercise_semaphore() -> dict[str, object]:
    lock = multiprocessing.Lock()
    require(lock.acquire(timeout=10), "multiprocessing lock acquisition timed out")
    try:
        require(
            not lock.acquire(block=False),
            "held multiprocessing lock did not provide nonblocking exclusivity",
        )
    finally:
        lock.release()
    require(
        lock.acquire(block=False),
        "released multiprocessing lock could not be reacquired",
    )
    lock.release()

    libc = ctypes.CDLL(None, use_errno=True)
    semaphore = (ctypes.c_long * 8)()
    pointer = ctypes.byref(semaphore)
    value = ctypes.c_int()
    for function, arguments in (
        (libc.sem_init, (pointer, 0, 0)),
        (libc.sem_getvalue, (pointer, ctypes.byref(value))),
    ):
        function.restype = ctypes.c_int
        require(function(*arguments) == 0, os.strerror(ctypes.get_errno()))
    require(value.value == 0, "zero semaphore has the wrong value")
    require(libc.sem_post(pointer) == 0, os.strerror(ctypes.get_errno()))
    require(libc.sem_getvalue(pointer, ctypes.byref(value)) == 0, os.strerror(ctypes.get_errno()))
    require(value.value == 1, "posted semaphore has the wrong value")
    require(libc.sem_wait(pointer) == 0, os.strerror(ctypes.get_errno()))
    require(libc.sem_getvalue(pointer, ctypes.byref(value)) == 0, os.strerror(ctypes.get_errno()))
    require(value.value == 0, "waited semaphore has the wrong value")
    require(libc.sem_destroy(pointer) == 0, os.strerror(ctypes.get_errno()))
    return {
        "multiprocessing_lock": True,
        "unnamed_acquire_release": True,
        "unnamed_get_value": True,
    }


def exercise_network() -> dict[str, object]:
    flags = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
    results = socket.getaddrinfo(
        "127.0.0.1",
        "443",
        family=socket.AF_INET,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
        flags=flags,
    )
    require(len(results) == 1, "numeric getaddrinfo returned an unexpected result count")
    family, socktype, protocol, canonical_name, address = results[0]
    require(family == socket.AF_INET, "numeric getaddrinfo returned the wrong family")
    require(socktype == socket.SOCK_STREAM, "numeric getaddrinfo returned the wrong type")
    require(protocol == socket.IPPROTO_TCP, "numeric getaddrinfo returned the wrong protocol")
    require(canonical_name == "", "numeric getaddrinfo returned a canonical name")
    require(address == ("127.0.0.1", 443), "numeric getaddrinfo returned the wrong address")
    return {"address": "127.0.0.1", "family": "AF_INET", "port": 443}


def exercise_timezone() -> dict[str, object]:
    require(hasattr(time, "tzset"), "time.tzset is unavailable")
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC0"
        time.tzset()
        require(
            time.localtime(0)[:6] == (1970, 1, 1, 0, 0, 0),
            "UTC0 conversion failed",
        )
        require(time.timezone == 0, "UTC0 timezone offset is not zero")

        os.environ["TZ"] = "EST5EDT,M3.2.0/2,M11.1.0/2"
        time.tzset()
        require(
            time.localtime(0)[:6] == (1969, 12, 31, 19, 0, 0),
            "POSIX TZ conversion failed",
        )
        require(time.timezone == 5 * 60 * 60, "POSIX TZ standard offset differs")
        require(time.daylight == 1, "POSIX TZ daylight rule was not enabled")
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()
    return {"posix_rule": True, "tzset": True, "utc_epoch": True}


def exercise_extension(extension_directory: str) -> tuple[dict[str, object], object]:
    require(os.path.isdir(extension_directory), "extension directory does not exist")
    sys.path.insert(0, extension_directory)
    try:
        extension = importlib.import_module("_crossforge")
    finally:
        sys.path.pop(0)

    module_file = os.path.realpath(extension.__file__)
    directory = os.path.realpath(extension_directory)
    require(
        os.path.commonpath((module_file, directory)) == directory,
        "qualification extension was imported outside --extension-dir",
    )
    require(extension.answer() == 42, "qualification extension returned the wrong value")
    return {
        "answer": 42,
        "file": os.path.basename(module_file),
        "module": "_crossforge",
    }, extension


def exercise_wchar(extension: object) -> dict[str, object]:
    value = "crossforge-交叉编译-🙂"
    require(ctypes.sizeof(ctypes.c_wchar) == 4, "target wchar_t is not 32-bit")
    buffer = ctypes.create_unicode_buffer(value)
    require(buffer.value == value, "ctypes wchar_t round-trip failed")
    require(extension.wchar_roundtrip(value) == value, "CPython wchar_t round-trip failed")

    libc = ctypes.CDLL(None)
    libc.wcslen.argtypes = (ctypes.c_wchar_p,)
    libc.wcslen.restype = ctypes.c_size_t
    require(libc.wcslen(value) == len(value), "libc wchar_t round-trip failed")
    return {"code_points": len(value), "cpython_api": True, "wchar_bytes": 4}


def exercise_pty() -> dict[str, object]:
    import pty
    import select
    import stat
    import termios
    import tty

    master = slave = None
    payload = b"crossforge-pty-probe\n"
    try:
        master, slave = pty.openpty()
        require(stat.S_ISCHR(os.fstat(master).st_mode), "PTY master is not a character device")
        require(stat.S_ISCHR(os.fstat(slave).st_mode), "PTY slave is not a character device")
        require(os.isatty(master) and os.isatty(slave), "PTY descriptors are not terminals")
        tty.setraw(slave)
        require(os.write(slave, payload) == len(payload), "PTY write was incomplete")
        ready, _, _ = select.select((master,), (), (), 10)
        require(ready == [master], "PTY master did not become readable")
        require(os.read(master, len(payload)) == payload, "PTY data round-trip failed")
        termios.tcgetattr(slave)
    finally:
        if slave is not None:
            os.close(slave)
        if master is not None:
            os.close(master)
    return {
        "character_devices": True,
        "isatty": True,
        "roundtrip_sha256": hashlib.sha256(payload).hexdigest(),
    }


def core_report(arguments: argparse.Namespace) -> dict[str, object]:
    identity = validate_identity(arguments.target, arguments.version)
    extension_report, extension = exercise_extension(arguments.extension_dir)
    return {
        "extension": extension_report,
        "functionality": exercise_libraries(),
        "hash_algorithm": exercise_hash_algorithm(),
        "imports": exercise_imports(),
        "mode": "core",
        "network": exercise_network(),
        "report_kind": "crossforge-cpython-probe",
        "schema_version": 2,
        "semaphore": exercise_semaphore(),
        "status": "passed",
        "sysconfig": identity,
        "target": arguments.target,
        "threading": exercise_threading(),
        "timezone": exercise_timezone(),
        "version": arguments.version,
        "wchar": exercise_wchar(extension),
    }


def devices_report(arguments: argparse.Namespace) -> dict[str, object]:
    identity = validate_identity(arguments.target, arguments.version)
    return {
        "mode": "devices",
        "probe": {"pty": exercise_pty()},
        "report_kind": "crossforge-cpython-probe",
        "schema_version": 2,
        "status": "passed",
        "sysconfig": identity,
        "target": arguments.target,
        "version": arguments.version,
    }


def main() -> None:
    arguments = parse_arguments()
    report = core_report(arguments) if arguments.mode == "core" else devices_report(arguments)
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
