"""Command-line entry point for the language prototype."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import signal
import sys

from .modules import ModuleLoader
from .runtime import Console, Filesystem, Interpreter, RandomSource, display
from .syntax import LanguageError


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def run_examples(program, recursive):
    interpreter = Interpreter(program.functions)
    passed = failed = 0
    for function in program.functions.values():
        if not recursive and function.module != program.entry:
            continue
        for example in function.examples:
            token = example.token
            location = f"{token.source}:{token.line}:{token.column}"
            label = f"example for '{function.name}'"
            if example.description is not None:
                label += f" {display(example.description, literal=True)}"
            try:
                mismatches = list(interpreter.example_mismatches(function, example))
            except RecursionError:
                print(f"{location}: {label}: interpreter reached its call stack limit", file=sys.stderr)
                failed += 1
                continue
            if mismatches:
                failed += 1
                for subject, expected, actual in mismatches:
                    print(f"{location}: {label}: {subject}: expected {display(expected, literal=True)}, "
                          f"got {display(actual, literal=True)}", file=sys.stderr)
            else:
                passed += 1
    print(f"{passed} passed, {failed} failed ({passed + failed} total)")
    return int(failed > 0)


def main():
    cli = argparse.ArgumentParser(description="Check, test, or run a program in the language prototype.")
    commands = cli.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate a source file without running it")
    check.add_argument("source", type=Path)
    test = commands.add_parser("test", help="validate and run attached examples")
    test.add_argument("source", type=Path)
    test.add_argument("-r", action="store_true", help="also test transitively imported files")
    run = commands.add_parser("run", help="validate and run main")
    run.add_argument("source", type=Path)
    run.add_argument("--console", action="store_true", help="pass live standard input and output")
    run.add_argument("--filesystem", action="store_true", help="pass access to the live filesystem")
    run.add_argument("--random", action="store_true", help="pass access to secure host randomness")
    args = cli.parse_args()
    try:
        program = ModuleLoader().load(args.source)
    except LanguageError as error:
        token = error.token
        print(f"{token.source or args.source}:{token.line}:{token.column}: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"{args.source}: {error}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"{args.source}: OK")
        return 0
    if args.command == "test":
        try:
            return run_examples(program, args.r)
        except KeyboardInterrupt:
            return 130

    entry = program.functions.get((program.entry, "main"))
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
            value = Interpreter(program.functions).call(entry.key, arguments)
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
