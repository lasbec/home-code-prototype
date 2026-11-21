"""Command-line entry point for the language prototype."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import signal
import sys

from .checker import Checker
from .runtime import Console, Filesystem, Interpreter, RandomSource, display
from .syntax import LanguageError, Parser


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    cli = argparse.ArgumentParser(description="Check or run a program in the language prototype.")
    commands = cli.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate a source file without running it")
    check.add_argument("source", type=Path)
    run = commands.add_parser("run", help="validate and run main")
    run.add_argument("source", type=Path)
    run.add_argument("--console", action="store_true", help="pass live standard input and output")
    run.add_argument("--filesystem", action="store_true", help="pass access to the live filesystem")
    run.add_argument("--random", action="store_true", help="pass access to secure host randomness")
    args = cli.parse_args()
    try:
        source = args.source.read_text(encoding="utf-8")
        program = Checker(Parser(source).program()).program()
    except LanguageError as error:
        token = error.token
        print(f"{args.source}:{token.line}:{token.column}: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"{args.source}: {error}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"{args.source}: OK")
        return 0

    entry = program.get("main")
    if entry is None:
        cli.error("run requires a main function")
    capabilities = {}
    if args.console:
        capabilities["console"] = Console(sys.stdin, sys.stdout)
    if args.filesystem:
        capabilities["filesystem"] = Filesystem()
    if args.random:
        capabilities["random"] = RandomSource()
    arguments = []
    for parameter in entry.parameters:
        capability = parameter.type.name
        if capability not in capabilities:
            cli.error(f"main requires --{capability}")
        arguments.append(capabilities[capability])
    try:
        with ExitStack() as cleanup:
            if "console" in capabilities:
                previous = signal.signal(signal.SIGTERM, terminate)
                cleanup.callback(signal.signal, signal.SIGTERM, previous)
                cleanup.callback(capabilities["console"].restore_key_input)
            value = Interpreter(program).call("main", arguments)
    except KeyboardInterrupt:
        return 130
    except RecursionError:
        print("prototype interpreter reached its call stack limit", file=sys.stderr)
        return 1
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    if value is not None:
        print(display(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
