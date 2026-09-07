#!/usr/bin/env python3
"""Run a command while emitting periodic liveness records."""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path


class HeartbeatError(ValueError):
    pass


def positive_interval(value):
    try:
        interval = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("interval must be an integer") from error
    if interval < 1 or interval > 3600:
        raise argparse.ArgumentTypeError("interval must be between 1 and 3600")
    return interval


def normalize_returncode(returncode):
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def forward_signal(process, state, signum, _frame):
    state["signal"] = signum
    try:
        if process.poll() is None:
            process.send_signal(signum)
    except OSError:
        # The child may exit between poll() and send_signal(). The wrapper
        # still records the signal and returns the corresponding shell code.
        pass


def execute(
    command,
    label,
    interval,
    popen=subprocess.Popen,
    clock=time.monotonic,
    reporter=print,
    install_signal_handlers=True,
    output=None,
    log_path=None,
):
    if not command:
        raise HeartbeatError("command is required")
    if output is None:
        process = popen(command)
    else:
        process = popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    started = clock()
    state = {"signal": None}
    previous_handlers = {}
    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(
                signum,
                lambda observed, frame: forward_signal(
                    process, state, observed, frame
                ),
            )
    try:
        while True:
            try:
                returncode = process.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                message = "heartbeat: %s elapsed=%ds pid=%d status=running" % (
                    label,
                    int(clock() - started),
                    process.pid,
                )
                if log_path is not None:
                    try:
                        size = Path(log_path).stat().st_size
                    except OSError:
                        message += " log_bytes=unavailable"
                    else:
                        message += " log_bytes=%d" % size
                reporter(message, flush=True)
                continue
            if state["signal"] is not None:
                return 128 + state["signal"]
            return normalize_returncode(returncode)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--label", required=True)
    parser.add_argument("--interval", type=positive_interval, default=60)
    parser.add_argument("--log", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.command and arguments.command[0] == "--":
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("command is required after --")
    return arguments


def main(argv=None):
    arguments = parse_arguments(argv)
    output = None
    try:
        if arguments.log is not None:
            output = arguments.log.open("xb")
        return execute(
            arguments.command,
            arguments.label,
            arguments.interval,
            output=output,
            log_path=arguments.log,
        )
    except OSError as error:
        print("error: cannot start command: %s" % error, file=sys.stderr)
        return 127
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    sys.exit(main())
