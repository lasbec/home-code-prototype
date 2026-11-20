"""Interpreter for checked programs and live built-in host environments."""

from dataclasses import dataclass
from operator import eq, ge, gt, le, lt, ne, truediv
from pathlib import Path


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


@dataclass(frozen=True)
class Console:
    stdin: object
    stdout: object


@dataclass(frozen=True)
class Filesystem:
    pass


def print_text(console, text):
    try:
        print(text, file=console.stdout, flush=True)
        return Result("Ok", None)
    except (OSError, UnicodeError, ValueError) as error:
        return Result("Err", Failure("PrintError", str(error)))


def input_line(console):
    try:
        line = console.stdin.readline()
        if not line:
            return Result("Err", Failure("EndOfInput"))
        line = line.removesuffix("\n").removesuffix("\r")
        line.encode("utf-8")
        return Result("Ok", line)
    except (OSError, UnicodeError, ValueError) as error:
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
    "input": input_line,
    "readFileUTF8": read_file,
    "writeFileUTF8": write_file,
    "add": lambda left, right: checked_int(left + right),
    "subtract": lambda left, right: checked_int(left - right),
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
OPERATORS = {"==": eq, "!=": ne, "<": lt, ">": gt, "<=": le, ">=": ge, "/": truediv}


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

    def block(self, statements, environment):
        for node in statements:
            kind, fields = node.kind, node.fields
            if kind == "let":
                name, expression = fields
                environment[name] = Cell(self.expression(expression, environment))
            elif kind == "assign":
                name, expression = fields
                environment[name].value = self.expression(expression, environment)
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
            for item in environment[source].value:
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
            left, right = fields
            return OPERATORS[node.token.text](self.expression(left, environment),
                                              self.expression(right, environment))
        if kind == "invoke":
            name, plan = fields
            return self.call(name, self.arguments(plan, environment))
        raise AssertionError(kind)

    def arguments(self, plan, environment):
        arguments = [None] * len(plan)
        for slot, mutable, expression in plan:
            arguments[slot] = environment[expression] if mutable else self.expression(expression, environment)
        return arguments


def display(value):
    if value is None:
        return "unit"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Result):
        return f"{value.tag}({display(value.value)})"
    if isinstance(value, Variant):
        tag = f"{value.name}::{value.tag}"
        return tag if value.value is None else f"{tag}({display(value.value)})"
    if isinstance(value, Record):
        fields = ", ".join(f"{name}: {display(field)}" for name, field in value.fields)
        return f"{value.name}({fields})"
    if isinstance(value, Failure):
        return f"{value.code}: {value.message}" if value.message else value.code
    if isinstance(value, tuple):
        return "[" + ", ".join(map(display, value)) + "]"
    if isinstance(value, (Console, Filesystem)):
        return type(value).__name__.lower()
    return str(value)
