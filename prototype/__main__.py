"""Command-line entry point for the language prototype."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import signal
import sqlite3
import sys

from .modules import ModuleLoader
from .runtime import (
    Cell, Console, Filesystem, Interpreter, MockCapability, RandomSource, display, write_atomic,
)
from .snapshot import export_program, save_program
from .syntax import CAPABILITIES, UNIT, LanguageError


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


def report_error(source, error):
    if isinstance(error, LanguageError):
        token = error.token
        print(f"{token.source or source}:{token.line}:{token.column}: {error}", file=sys.stderr)
    else:
        print(f"{source}: {error}", file=sys.stderr)


def add_state_options(command, *, output=False):
    for environment in CAPABILITIES:
        command.add_argument(f"--{environment}-state", type=Path, metavar="FILE",
                             help=f"read initial state for the {environment} mock")
        if output:
            command.add_argument(f"--{environment}-state-out", type=Path, metavar="FILE",
                                 help=f"save final state for the {environment} mock")


def cli_arguments(arguments):
    # Only the attached form takes a path. A bare flag may precede the source path.
    if not arguments or arguments[0] != "run":
        return arguments
    result = []
    for index, argument in enumerate(arguments):
        if argument == "--":
            return result + arguments[index:]
        option, equals, path = argument.partition("=")
        environment = option[2:]
        if equals and option.startswith("--") and environment in CAPABILITIES:
            argument = f"--_{environment}-mock={path}"
        result.append(argument)
    return result


def load_mocks(args, cli):
    mocks = {}
    for environment in CAPABILITIES:
        paths = getattr(args, f"_{environment}_mock") or []
        state = getattr(args, f"{environment}_state")
        output = getattr(args, f"{environment}_state_out")
        if not paths:
            if state is not None or output is not None:
                cli.error(f"{environment} state options require --{environment}=MOCK")
            continue
        if len(paths) > 1 or getattr(args, environment):
            cli.error(f"select one provider for {environment}")
        path = paths[0]
        source = path
        try:
            provider = ModuleLoader().load(path)
            if provider.mock is None:
                raise ValueError(f"expected a 'mock {environment}' header")
            header = provider.mock.mock_header
            if header.text != environment:
                raise LanguageError(header, f"--{environment} requires 'mock {environment}', found 'mock {header.text}'")
            fixture = None
            if state is not None:
                source = state
                fixture = provider.load_state(state)
            initializer = (provider.entry, "init")
            if fixture is None and initializer not in provider.functions and provider.mock.state_type != UNIT:
                raise LanguageError(header, f"mock requires --{environment}-state or a private init function")
        except (LanguageError, OSError, ValueError, RuntimeError) as error:
            report_error(source, error)
            return None
        mocks[environment] = provider, fixture, output
    return mocks


def main():
    cli = argparse.ArgumentParser(description="Check, test, run, or snapshot a program in the language prototype.")
    commands = cli.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate a source file without running it")
    check.add_argument("source", type=Path)
    add_state_options(check)
    test = commands.add_parser("test", help="validate and run attached examples")
    test.add_argument("source", type=Path)
    test.add_argument("-r", action="store_true", help="also test transitively imported files")
    save = commands.add_parser("save", help="check a program and save it to SQLite")
    save.add_argument("source", type=Path)
    save.add_argument("database", type=Path)
    export = commands.add_parser("export", help="regenerate .proto files from a program database")
    export.add_argument("database", type=Path)
    export.add_argument("directory", type=Path)
    run = commands.add_parser("run", help="validate and run main")
    run.add_argument("source", type=Path)
    for environment in CAPABILITIES:
        run.add_argument(f"--{environment}", action="store_true",
                         help=f"use live {environment}, or use --{environment}=MOCK to load a .proto mock")
        run.add_argument(f"--_{environment}-mock", action="append", type=Path, help=argparse.SUPPRESS)
    add_state_options(run, output=True)
    args = cli.parse_args(cli_arguments(sys.argv[1:]))
    if args.command in {"save", "export"}:
        source = args.source if args.command == "save" else args.database
        try:
            if args.command == "save":
                save_program(source, args.database)
                print(args.database)
            else:
                print(export_program(source, args.directory))
        except (LanguageError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            report_error(source, error)
            return 1
        return 0
    try:
        program = ModuleLoader().load(args.source)
    except (LanguageError, OSError, ValueError, RuntimeError) as error:
        report_error(args.source, error)
        return 1
    if args.command == "check":
        for environment in CAPABILITIES:
            state = getattr(args, f"{environment}_state")
            if state is None:
                continue
            if program.mock is None or program.mock.mock_header.text != environment:
                cli.error(f"--{environment}-state requires a 'mock {environment}' source")
            try:
                program.load_state(state)
            except (LanguageError, OSError, ValueError, RuntimeError) as error:
                report_error(state, error)
                return 1
        print(f"{args.source}: OK")
        return 0
    if args.command == "test":
        try:
            return run_examples(program, args.r)
        except KeyboardInterrupt:
            return 130

    if program.mock:
        cli.error("run requires an application source file")
    entry = program.functions.get((program.entry, "main"))
    if entry is None:
        cli.error("run requires a main function")
    mocks = load_mocks(args, cli)
    if mocks is None:
        return 1
    for parameter in entry.parameters:
        environment = parameter.type.name
        if environment not in mocks and not getattr(args, environment):
            cli.error(f"main requires --{environment}")
    capabilities = {}
    if args.console:
        capabilities["console"] = Console(sys.stdin, sys.stdout)
    if args.filesystem:
        capabilities["filesystem"] = Filesystem()
    if args.random:
        capabilities["random"] = RandomSource()
    functions = program.functions.copy()
    for provider, _, _ in mocks.values():
        functions.update(provider.functions)
    interpreter = Interpreter(functions)
    try:
        with ExitStack() as cleanup:
            if args.console:
                previous = signal.signal(signal.SIGTERM, terminate)
                cleanup.callback(signal.signal, signal.SIGTERM, previous)
                cleanup.callback(capabilities["console"].restore_key_input)
            for environment, (provider, fixture, _) in mocks.items():
                initializer = (provider.entry, "init")
                if fixture is not None:
                    state = interpreter.expression(fixture, {})
                elif initializer in functions:
                    state = interpreter.call(initializer, ())
                else:
                    state = None
                capabilities[environment] = MockCapability(environment, provider.entry, Cell(state))
            arguments = [capabilities[parameter.type.name] for parameter in entry.parameters]
            value = interpreter.call(entry.key, arguments)
        snapshots = [
            (output, display(capabilities[environment].state.value, literal=True) + "\n")
            for environment, (_, _, output) in mocks.items() if output is not None
        ]
        for output, snapshot in snapshots:
            write_atomic(output, snapshot)
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
