"""Interpreter for checked programs and live built-in host environments."""

import codecs
from dataclasses import dataclass, field
import json
from operator import eq, ge, gt, le, lt, ne, truediv
import os
from pathlib import Path
import secrets
from select import select
from tempfile import NamedTemporaryFile
from time import monotonic

from .syntax import EXAMPLE_ERROR_MESSAGES

try:
    import termios
    import tty
except ImportError:
    termios = None


@dataclass
class Cell:
    value: object


@dataclass(frozen=True)
class Result:
    tag: str
    value: object


@dataclass(frozen=True)
class Record:
    name: str
    fields: tuple


@dataclass(frozen=True)
class Variant:
    name: str
    tag: str
    value: object = None


@dataclass(frozen=True)
class Failure:
    code: str
    message: str = ""


@dataclass
class Console:
    stdin: object
    stdout: object
    _terminal_state: tuple | None = field(default=None, init=False, repr=False)
    _decoder: codecs.IncrementalDecoder | None = field(default=None, init=False, repr=False)

    def restore_key_input(self):
        if self._terminal_state is None:
            return
        fd, saved = self._terminal_state
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
        except termios.error as error:
            raise OSError(f"could not restore terminal settings: {error}") from error
        self._terminal_state = None
        self._decoder = None


@dataclass(frozen=True)
class Filesystem:
    pass


class RandomSource:
    def randbelow(self, width):
        return secrets.randbelow(width)


def with_ordered_bounds(draw):
    def bounded_draw(source, bound0, bound1):
        lower, upper = sorted((bound0, bound1))
        return draw(source, lower, upper)

    return bounded_draw


@with_ordered_bounds
def random_int(source, lower, upper):
    try:
        return Result("Ok", lower + source.randbelow(upper - lower + 1))
    except OSError as error:
        return Result("Err", Failure("RandomError", str(error)))


@with_ordered_bounds
def pseudo_random(state, lower, upper):
    # PCG RXS M XS with 32-bit state and output. Scale one output to the
    # interval by taking the high 32 bits, dividing the samples as evenly as possible.
    mask = (1 << 32) - 1
    next_state = (state * 747796405 + 2891336453) & mask
    shift = (next_state >> 28) + 4
    word = ((next_state ^ (next_state >> shift)) * 277803737) & mask
    sample = word ^ (word >> 22)
    value = lower + ((sample * (upper - lower + 1)) >> 32)
    signed_state = next_state - (1 << 32) if next_state >= (1 << 31) else next_state
    return Record("RandomDraw", (("value", value), ("state", signed_state)))


def output_text(console, text, error_code):
    try:
        console.stdout.write(text)
        console.stdout.flush()
        return Result("Ok", None)
    except (OSError, UnicodeError, ValueError) as error:
        return Result("Err", Failure(error_code, str(error)))


def print_text(console, text):
    return output_text(console, text + "\n", "PrintError")


def write_text(console, text):
    return output_text(console, text, "ConsoleWriteError")


def input_line(console):
    if console._terminal_state is not None:
        return Result("Err", Failure("InputError", "line input is unavailable in key input mode"))
    try:
        line = console.stdin.readline()
        if not line:
            return Result("Err", Failure("EndOfInput"))
        line = line.removesuffix("\n").removesuffix("\r")
        line.encode("utf-8")
        return Result("Ok", line)
    except (OSError, UnicodeError, ValueError) as error:
        return Result("Err", Failure("InputError", str(error)))


def enable_key_input(console):
    if console._terminal_state is not None:
        return Result("Ok", True)
    if termios is None or os.environ.get("TERM") == "dumb":
        return Result("Ok", False)
    try:
        if not (console.stdin.isatty() and console.stdout.isatty()):
            return Result("Ok", False)
        fd = console.stdin.fileno()
        decoder = codecs.getincrementaldecoder(console.stdin.encoding or "utf-8")(errors="replace")
        console._terminal_state = (fd, termios.tcgetattr(fd))
        try:
            tty.setcbreak(fd, termios.TCSANOW)
        except (OSError, termios.error):
            console._terminal_state = None
            raise
        console._decoder = decoder
        return Result("Ok", True)
    except (OSError, ValueError, LookupError, termios.error) as error:
        return Result("Err", Failure("TerminalModeError", str(error)))


def disable_key_input(console):
    try:
        console.restore_key_input()
        return Result("Ok", None)
    except OSError as error:
        return Result("Err", Failure("TerminalModeError", str(error)))


def read_keys_for(console, timeout):
    if console._terminal_state is None:
        return Result("Err", Failure("InputError", "key input requires an enabled terminal"))
    if timeout < 0:
        return Result("Err", Failure("InvalidTimeout", "expected a nonnegative duration"))
    fd, _ = console._terminal_state
    deadline = monotonic() + timeout / 1000
    keys = []
    try:
        while True:
            remaining = max(0, deadline - monotonic())
            ready, _, _ = select([fd], [], [], remaining)
            if not ready:
                return Result("Ok", tuple(keys))
            data = os.read(fd, 4096)
            if not data:
                if keys:
                    return Result("Ok", tuple(keys))
                return Result("Err", Failure("EndOfInput"))
            keys.extend(console._decoder.decode(data))
            if monotonic() >= deadline:
                return Result("Ok", tuple(keys))
    except (OSError, ValueError) as error:
        return Result("Err", Failure("InputError", str(error)))


def read_file(filesystem, path):
    try:
        return Result("Ok", Path(path).read_bytes().decode("utf-8"))
    except FileNotFoundError as error:
        return Result("Err", Failure("FileNotFound", str(error)))
    except (OSError, UnicodeError, ValueError) as error:
        return Result("Err", Failure("ReadError", str(error)))


def write_file(filesystem, path, text):
    try:
        Path(path).write_bytes(text.encode("utf-8"))
        return Result("Ok", None)
    except (OSError, UnicodeError, ValueError) as error:
        return Result("Err", Failure("WriteError", str(error)))


def replace_file(filesystem, path, text):
    temporary = None
    try:
        destination = Path(path)
        content = text.encode("utf-8")
        with NamedTemporaryFile(dir=destination.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        os.replace(temporary, destination)
        temporary = None
        return Result("Ok", None)
    except (OSError, UnicodeError, ValueError) as error:
        return Result("Err", Failure("WriteError", str(error)))
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def checked_int(value):
    if -(2**31) <= value < 2**31:
        return Result("Ok", value)
    return Result("Err", Failure("Overflow"))


def nonzero(value):
    return Result("Ok", value) if value else Result("Err", Failure("Zero"))


def list_at(items, index):
    if 0 <= index < len(items):
        return Result("Ok", items[index])
    return Result("Err", Failure("IndexError", f"index {index} is outside the list"))


def split_lines(text):
    if not text:
        return ()
    return tuple(text.replace("\r\n", "\n").removesuffix("\n").split("\n"))


def parse_int(text):
    digits = text.removeprefix("-")
    if not (digits.isascii() and digits.isdecimal()):
        return Result("Err", Failure("ParseIntError", "expected a decimal integer"))
    significant = digits.lstrip("0") or "0"
    if len(significant) <= 10:
        value = int(significant)
        if text.startswith("-"):
            value = -value
        if -(2**31) <= value < 2**31:
            return Result("Ok", value)
    return Result("Err", Failure("ParseIntError", "integer is outside the int32 range"))


def host_error_kind(error):
    tag = error.code if error.code in {"EndOfInput", "FileNotFound"} else "Other"
    return Variant("HostErrorKind", tag)


BUILTINS = {
    "print": print_text,
    "write": write_text,
    "input": input_line,
    "enableKeyInput": enable_key_input,
    "disableKeyInput": disable_key_input,
    "readKeysFor": read_keys_for,
    "readFileUTF8": read_file,
    "writeFileUTF8": write_file,
    "replaceFileUTF8": replace_file,
    "random": random_int,
    "pseudoRandom": pseudo_random,
    "add": lambda left, right: checked_int(left + right),
    "subtract": lambda left, right: checked_int(left - right),
    "min": min,
    "max": max,
    "nonzero": nonzero,
    "at": list_at,
    "prepend": lambda item, items: (item, *items),
    "dropLast": lambda items: items[:-1],
    "concat": lambda left, right: left + right,
    "splitLines": split_lines,
    "parseInt32": parse_int,
    "formatInt32": str,
    "hostErrorKind": host_error_kind,
    "describeHostError": lambda error: display(error),
}
OPERATORS = {"==": eq, "!=": ne, "<": lt, ">": gt, "<=": le, ">=": ge, "/": truediv,
             "*": lambda left, right: checked_int(left * right)}


class Interpreter:
    def __init__(self, functions):
        self.functions = functions

    def call(self, name, arguments):
        if name in BUILTINS:
            return BUILTINS[name](*arguments)
        function = self.functions[name]
        environment = {
            parameter.name: argument if parameter.mutable else Cell(argument)
            for parameter, argument in zip(function.parameters, arguments, strict=True)
        }
        _, value = self.block(function.body, environment)
        return value

    def example_mismatches(self, function, example):
        arguments = []
        mutable = {}
        for parameter, expression in zip(function.parameters, example.inputs, strict=True):
            value = self.expression(expression, {})
            if parameter.mutable:
                value = Cell(value)
                mutable[parameter.name] = value
            arguments.append(value)

        actual = self.call(function.key, arguments)
        expected = self.expression(example.output, {})
        if actual != expected:
            yield "return value", expected, actual
        for name, expression in example.after:
            actual = mutable[name].value
            expected = self.expression(expression, {})
            if actual != expected:
                yield f"mut parameter '{name}'", expected, actual

    def block(self, statements, environment):
        for node in statements:
            kind, fields = node.kind, node.fields
            if kind == "let":
                binding, expression, _ = fields
                environment[binding.text] = Cell(self.expression(expression, environment))
            elif kind == "assign":
                binding, expression = fields
                environment[binding.text].value = self.expression(expression, environment)
            elif kind == "return":
                return True, self.expression(fields[0], environment)
            elif kind == "expression":
                self.expression(fields[0], environment)
            else:
                returns, value = self.control(node, environment)
                if returns:
                    return True, value
        return False, None

    def control(self, node, environment):
        kind, fields = node.kind, node.fields
        if kind == "if":
            condition, yes, no = fields
            return self.block(yes if self.expression(condition, environment) else no,
                              environment.copy())
        if kind == "match":
            subject, arms = fields
            result = self.expression(subject, environment)
            name, body = arms[result.tag]
            scope = environment.copy()
            if name is not None:
                scope[name] = Cell(result.value)
            return self.block(body, scope)
        if kind == "loop":
            while True:
                returns, value = self.block(fields[0], environment.copy())
                if returns:
                    return True, value
        if kind == "for":
            name, source, body = fields
            for item in self.expression(source, environment):
                returns, value = self.block(body, {**environment, name: Cell(item)})
                if returns:
                    return True, value
            return False, None
        raise AssertionError(kind)

    def expression(self, node, environment):
        kind, fields = node.kind, node.fields
        if kind == "value":
            return fields[0]
        if kind == "variable":
            return environment[fields[0]].value
        if kind == "list":
            return tuple(self.expression(item, environment) for item in fields)
        if kind == "result":
            tag, value = fields
            return Result(tag, self.expression(value, environment))
        if kind == "failure":
            code, message = fields
            return Failure(code, "" if message is None else self.expression(message, environment))
        if kind == "variant":
            name, tag, payload = fields
            value = None if payload is None else self.expression(payload, environment)
            return Variant(name, tag, value)
        if kind == "record":
            name, labels, plan = fields
            arguments = self.arguments(plan, environment)
            return Record(name, tuple(zip(labels, arguments, strict=True)))
        if kind == "field":
            value, index = fields
            return self.expression(value, environment).fields[index][1]
        if kind == "binary":
            left, right, _ = fields
            return OPERATORS[node.token.text](self.expression(left, environment),
                                              self.expression(right, environment))
        if kind == "invoke":
            name, plan, _ = fields
            return self.call(name, self.arguments(plan, environment))
        raise AssertionError(kind)

    def arguments(self, plan, environment):
        arguments = [None] * len(plan)
        for slot, mutable, value in plan:
            arguments[slot] = environment[value.text] if mutable else self.expression(value, environment)
        return arguments


def display(value, *, literal=False):
    if value is None:
        return "unit"
    if isinstance(value, bool):
        return "true" if value else "false"
    if literal and isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Result):
        return f"{value.tag}({display(value.value, literal=literal)})"
    if isinstance(value, Variant):
        tag = f"{value.name}::{value.tag}"
        return tag if value.value is None else f"{tag}({display(value.value, literal=literal)})"
    if isinstance(value, Record):
        fields = ", ".join(f"{name}: {display(field, literal=literal)}" for name, field in value.fields)
        return f"{value.name}({fields})"
    if isinstance(value, Failure):
        if literal and value.code in EXAMPLE_ERROR_MESSAGES:
            message = display(value.message, literal=True) if EXAMPLE_ERROR_MESSAGES[value.code] else ""
            return f"{value.code}({message})"
        return f"{value.code}: {value.message}" if value.message else value.code
    if isinstance(value, tuple):
        return "[" + ", ".join(display(item, literal=literal) for item in value) + "]"
    if isinstance(value, (Console, Filesystem)):
        return type(value).__name__.lower()
    if isinstance(value, RandomSource):
        return "random"
    return str(value)
