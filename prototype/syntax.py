"""Source grammar and shared representations for the throwaway prototype."""

from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    column: int
    kind: str = ""


class LanguageError(Exception):
    def __init__(self, token, message):
        super().__init__(message)
        self.token = token


@dataclass(frozen=True)
class Type:
    name: str
    arguments: tuple = ()
    units: tuple = ()
    nonzero: bool = False

    def __str__(self):
        name = self.name + ("!0" if self.nonzero else "")
        if self.arguments:
            return f"{name}<{', '.join(map(str, self.arguments))}>"
        if not self.units:
            return name
        numerator = [unit for unit, power in self.units for _ in range(max(0, power))]
        denominator = [unit for unit, power in self.units for _ in range(max(0, -power))]
        units = "*".join(numerator) or "1"
        if denominator:
            units += "/" + "/".join(denominator)
        return f"{name}<{units}>"

    def without_refinement(self):
        return Type(self.name, self.arguments, self.units)

    def refined(self):
        return Type(self.name, self.arguments, self.units, nonzero=True)


BOOL = Type("bool")
INT = Type("int32")
STRING = Type("string")
UNIT = Type("unit")
CONSOLE = Type("console")
FILESYSTEM = Type("filesystem")
RANDOM = Type("random")
RANDOM_DRAW = Type("RandomDraw")
OVERFLOW = Type("Overflow")
ZERO = Type("Zero")
INDEX_ERROR = Type("IndexError")
HOST_ERROR = Type("HostError")
HOST_ERROR_KIND = Type("HostErrorKind")
PARSE_INT_ERROR = Type("ParseIntError")
GUARANTEES = frozenset({"terminates", "deterministic", "pure"})
MODIFIERS = GUARANTEES | {"commutative"}
SIMPLE_TYPES = {
    item.name for item in (BOOL, STRING, UNIT, CONSOLE, FILESYSTEM, RANDOM, RANDOM_DRAW,
                           OVERFLOW, ZERO, INDEX_ERROR, HOST_ERROR, HOST_ERROR_KIND, PARSE_INT_ERROR)
}


def result_type(value, error):
    return Type("result", (value, error))


def quotient_units(numerator, denominator):
    powers = dict(numerator)
    for unit, power in denominator:
        powers[unit] = powers.get(unit, 0) - power
    return tuple(sorted((unit, power) for unit, power in powers.items() if power))


def product_units(left, right):
    return quotient_units(left, tuple((unit, -power) for unit, power in right))


@dataclass(frozen=True)
class Node:
    token: Token
    kind: str
    fields: tuple = ()


@dataclass(frozen=True)
class Parameter:
    token: Token
    type: Type
    mutable: bool = False

    @property
    def name(self):
        return self.token.text


@dataclass(frozen=True)
class Function:
    token: Token
    modifiers: frozenset
    parameters: tuple
    output: Type
    body: tuple = ()
    commutative: tuple = ()

    @property
    def name(self):
        return self.token.text

    @property
    def commutative_slots(self):
        if "commutative" not in self.modifiers:
            return frozenset()
        return frozenset(index for index, parameter in enumerate(self.parameters)
                         if not self.commutative or parameter.name in self.commutative)


@dataclass(frozen=True)
class RecordDeclaration:
    token: Token
    fields: tuple


@dataclass(frozen=True)
class EnumDeclaration:
    token: Token
    variants: tuple


@dataclass(frozen=True)
class Argument:
    token: Token
    expression: Node
    label: str | None = None
    mutable: bool = False


TOKEN = re.compile(
    r'(?P<skip>\s+|//[^\n]*)|(?P<string>"(?:[^"\\\n]|\\[^\n])*")|'
    r'(?P<integer>[0-9]+)|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|'
    r'(?P<punct>!0|->|::|==|!=|<=|>=|[{}()\[\],.:;=<>+*/-])|(?P<invalid>.)'
)
RESERVED = MODIFIERS | {
    "fn", "mut", "let", "as", "return", "if", "else", "for", "in",
    "match", "true", "false", "unit", "record", "enum", "loop",
}
PRECEDENCE = {"as": 0, "==": 1, "!=": 1, "<": 1, ">": 1, "<=": 1,
              ">=": 1, "+": 2, "-": 2, "*": 3, "/": 3}


def tokenize(source):
    line, column = 1, 1
    for match in TOKEN.finditer(source):
        value = match.group()
        token = Token(value, line, column, match.lastgroup)
        if token.kind == "invalid":
            raise LanguageError(token, f"unexpected character {value!r}")
        if token.kind != "skip":
            yield token
        newlines = value.count("\n")
        if newlines:
            line += newlines
            column = len(value.rsplit("\n", 1)[1]) + 1
        else:
            column += len(value)
    yield Token("<end of file>", line, column)


class Parser:
    def __init__(self, source):
        self.tokens = list(tokenize(source))
        self.index = 0
        self.data_types = set()
        depth = 0
        for index, token in enumerate(self.tokens):
            if token.text == "{":
                depth += 1
            elif token.text == "}":
                depth -= 1
            elif not depth and token.text in {"record", "enum"}:
                self.data_types.add(self.tokens[index + 1].text)

    @property
    def current(self):
        return self.tokens[self.index]

    def take(self):
        token = self.current
        if self.index < len(self.tokens) - 1:
            self.index += 1
        return token

    def accept(self, text):
        if self.current.text == text:
            return self.take()
        return None

    def expect(self, text):
        token = self.take()
        if token.text != text:
            raise LanguageError(token, f"expected {text!r}, found {token.text!r}")
        return token

    def name(self):
        token = self.take()
        if token.kind != "name" or token.text in RESERVED:
            raise LanguageError(token, f"expected a name, found {token.text!r}")
        return token

    def program(self):
        declarations = []
        while self.current.kind:
            if self.accept("record"):
                name = self.name()
                self.expect("{")
                declarations.append(RecordDeclaration(name, self.separated("}", self.record_field)))
                continue
            if self.accept("enum"):
                name = self.name()
                self.expect("{")
                declarations.append(EnumDeclaration(name, self.separated("}", self.variant)))
                continue
            modifiers = set()
            commutative = []
            while self.current.text in MODIFIERS:
                token = self.take()
                if token.text in modifiers:
                    raise LanguageError(token, f"duplicate modifier '{token.text}'")
                modifiers.add(token.text)
                if token.text == "commutative":
                    while self.current.kind == "name" and self.current.text not in RESERVED:
                        commutative.append(self.name().text)
            self.expect("fn")
            name = self.name()
            self.expect("(")
            parameters = self.separated(")", self.parameter)
            self.expect("->")
            output = self.type()
            declarations.append(Function(name, frozenset(modifiers), parameters, output,
                                         self.block(), tuple(commutative)))
        return tuple(declarations)

    def separated(self, end, read):
        items = []
        while self.current.text != end:
            items.append(read())
            if not self.accept(","):
                break
        self.expect(end)
        return tuple(items)

    def parameter(self):
        name = self.name()
        self.expect(":")
        mutable = bool(self.accept("mut"))
        return Parameter(name, self.type(), mutable)

    def record_field(self):
        name = self.name()
        self.expect(":")
        return Parameter(name, self.type())

    def variant(self):
        name = self.name()
        payload = None
        if self.accept("("):
            payload = self.type()
            self.expect(")")
        return name, payload

    def type(self):
        token = self.take()
        name = token.text
        if name in SIMPLE_TYPES:
            return Type(name)
        if name == "list":
            self.expect("<")
            element = self.type()
            self.expect(">")
            return Type(name, (element,))
        if name == "result":
            self.expect("<")
            value = self.type()
            self.expect(",")
            error = self.type()
            self.expect(">")
            return result_type(value, error)
        if name in {"int32", "f64"}:
            nonzero = self.accept("!0")
            if name == "f64" and nonzero:
                raise LanguageError(nonzero, "only int32 supports !0 in this prototype")
            units = self.units() if self.accept("<") else ()
            return Type(name, units=units, nonzero=bool(nonzero))
        if name in self.data_types:
            return Type(name)
        raise LanguageError(token, f"unknown type '{name}'")

    def units(self):
        powers = {}
        sign = 1
        while True:
            token = self.take()
            if token.kind == "name":
                powers[token.text] = powers.get(token.text, 0) + sign
            elif token.text != "1":
                raise LanguageError(token, "expected a unit name or 1")
            if self.accept(">"):
                return tuple(sorted((unit, power) for unit, power in powers.items() if power))
            operator = self.take()
            if operator.text not in {"*", "/"}:
                raise LanguageError(operator, "expected '*', '/' or '>' in units")
            sign = 1 if operator.text == "*" else -1

    def block(self):
        self.expect("{")
        statements = []
        while not self.accept("}"):
            statements.append(self.statement())
        return tuple(statements)

    def statement(self):
        token = self.current
        if self.accept("let"):
            mutable = bool(self.accept("mut"))
            name = self.name()
            declared = self.type() if self.accept(":") else None
            self.expect("=")
            expression = self.expression()
            self.expect(";")
            return Node(token, "let", (name, declared, mutable, expression))
        if self.accept("return"):
            expression = self.expression()
            self.expect(";")
            return Node(token, "return", (expression,))
        if self.accept("if"):
            condition = self.expression()
            yes = self.block()
            no = self.block() if self.accept("else") else ()
            return Node(token, "if", (condition, yes, no))
        if self.accept("for"):
            item = self.name()
            self.expect("in")
            source = self.name()
            return Node(token, "for", (item, source, self.block()))
        if self.accept("loop"):
            return Node(token, "loop", (self.block(),))
        if self.accept("match"):
            subject = self.expression()
            self.expect("{")
            arms = []
            while not self.accept("}"):
                tag = self.name()
                binding = None
                if self.accept("("):
                    binding = self.name()
                    self.expect(")")
                arms.append((tag, binding, self.block()))
            return Node(token, "match", (subject, tuple(arms)))
        if token.kind == "name" and self.tokens[self.index + 1].text == "=":
            name = self.name()
            self.expect("=")
            expression = self.expression()
            self.expect(";")
            return Node(token, "assign", (name, expression))
        expression = self.expression()
        self.expect(";")
        return Node(token, "expression", (expression,))

    def expression(self, precedence=0):
        left = self.primary()
        while self.accept("."):
            field = self.name()
            left = Node(field, "field", (left,))
        while PRECEDENCE.get(self.current.text, -1) >= precedence:
            operator = self.take()
            if operator.text == "-":
                raise LanguageError(operator, "use subtract(left: ..., right: ...) with named operands")
            if operator.text == "as":
                left = Node(operator, "as", (left, self.type()))
            else:
                right = self.expression(PRECEDENCE[operator.text] + 1)
                left = Node(operator, "binary", (left, right))
        return left

    def primary(self):
        token = self.take()
        if token.text == "(":
            expression = self.expression()
            self.expect(")")
            return expression
        if token.text == "[":
            return Node(token, "list", self.separated("]", self.expression))
        if token.text == "-" or token.kind == "integer":
            digits = self.take() if token.text == "-" else token
            if digits.kind != "integer":
                raise LanguageError(digits, "expected an integer literal after '-'")
            value = digits.text.lstrip("0") or "0"
            if len(value) > 10:
                raise LanguageError(token, "integer literal is outside the int32 range")
            return Node(token, "integer", (-int(value) if token.text == "-" else int(value),))
        if token.kind == "string":
            try:
                value = json.loads(token.text)
                value.encode("utf-8")
            except (ValueError, UnicodeError) as error:
                raise LanguageError(token, "invalid UTF-8 string literal or escape") from error
            return Node(token, "value", (value, STRING))
        if token.text in {"true", "false"}:
            return Node(token, "value", (token.text == "true", BOOL))
        if token.text == "unit":
            return Node(token, "value", (None, UNIT))
        if token.kind == "name" and token.text not in RESERVED:
            if self.accept("::"):
                tag = self.name()
                payload = None
                if self.accept("("):
                    payload = self.expression()
                    self.expect(")")
                return Node(tag, "variant", (token.text, payload))
            if self.accept("("):
                return Node(token, "call", self.separated(")", self.argument))
            return Node(token, "variable", (token.text,))
        raise LanguageError(token, f"expected an expression, found {token.text!r}")

    def argument(self):
        token = self.current
        label = None
        if token.kind == "name" and self.tokens[self.index + 1].text == ":":
            label = self.name().text
            self.expect(":")
        mutable = bool(self.accept("mut"))
        return Argument(token, self.expression(), label, mutable)
