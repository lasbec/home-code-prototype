"""Static checking and lowering to an interpreter-ready program."""

from dataclasses import dataclass, field

from .syntax import (
    BOOL, CONSOLE, FILESYSTEM, GUARANTEES, HOST_ERROR, HOST_ERROR_KIND, INDEX_ERROR,
    INT, OVERFLOW, PARSE_INT_ERROR, SIMPLE_TYPES, STRING, UNIT, ZERO, Argument,
    EnumDeclaration, Function, LanguageError, Node, Parameter, RecordDeclaration,
    Token, Type, quotient_units, result_type,
)


def builtin(name, parameters, output, modifiers=frozenset()):
    return Function(Token(name, 0, 0), modifiers, tuple(
        Parameter(Token(label, 0, 0), parameter_type)
        for label, parameter_type in parameters
    ), output)


HOST_FUNCTIONS = {
    function.name: function for function in (
        builtin("print", (("console", CONSOLE), ("text", STRING)), result_type(UNIT, HOST_ERROR)),
        builtin("input", (("console", CONSOLE),), result_type(STRING, HOST_ERROR)),
        builtin("readFileUTF8", (("filesystem", FILESYSTEM), ("path", STRING)),
                result_type(STRING, HOST_ERROR)),
        builtin("writeFileUTF8", (("filesystem", FILESYSTEM), ("path", STRING), ("text", STRING)),
                result_type(UNIT, HOST_ERROR)),
    )
}
PURE_FUNCTIONS = {
    function.name: function for function in (
        builtin("concat", (("left", STRING), ("right", STRING)), STRING, GUARANTEES),
        builtin("splitLines", (("text", STRING),), Type("list", (STRING,)), GUARANTEES),
        builtin("parseInt32", (("text", STRING),), result_type(INT, PARSE_INT_ERROR), GUARANTEES),
        builtin("formatInt32", (("value", INT),), STRING, GUARANTEES),
        builtin("hostErrorKind", (("error", HOST_ERROR),), HOST_ERROR_KIND, GUARANTEES),
        builtin("describeHostError", (("error", HOST_ERROR),), STRING, GUARANTEES),
    )
}
GENERIC_FUNCTIONS = {"add", "subtract", "nonzero", "at", "prepend", "dropLast"}
CONSTRUCTORS = {"Ok", "Err"}
BUILTIN_NAMES = HOST_FUNCTIONS.keys() | PURE_FUNCTIONS.keys() | GENERIC_FUNCTIONS | CONSTRUCTORS


@dataclass(frozen=True)
class Binding:
    type: Type
    mutable: bool
    parameter: bool = False


@dataclass
class Flow:
    bindings: dict = field(default_factory=dict)
    nonzero: set = field(default_factory=set)
    protected: frozenset = frozenset()

    def copy(self):
        return Flow(self.bindings.copy(), self.nonzero.copy(), self.protected)

    def type_of(self, name):
        declared = self.bindings[name].type
        return declared.refined() if name in self.nonzero else declared

    def may_alias(self, left, right):
        if left == right:
            return True
        first, second = self.bindings[left], self.bindings[right]
        return (first.mutable and second.mutable and first.parameter and second.parameter
                and first.type == second.type)

    def ensure_unprotected(self, token, name):
        for source in self.protected:
            if self.may_alias(source, name):
                raise LanguageError(token, f"loop source '{source}' is read-only in the loop body")

    def drop_proof(self, name):
        self.nonzero = {other for other in self.nonzero if not self.may_alias(name, other)}

    def define(self, token, value_type, mutable=False, parameter=False):
        name = token.text
        if name in self.bindings:
            raise LanguageError(token, f"data variable '{name}' is already defined in this scope")
        self.bindings[name] = Binding(value_type, mutable, parameter)

    def require(self, token, name):
        if name not in self.bindings:
            raise LanguageError(token, f"unknown data variable '{name}'")
        return self.bindings[name]


@dataclass(frozen=True)
class Checked:
    type: Type
    node: Node


def compatible(actual, expected):
    return expected is None or actual == expected or (
        actual.nonzero and actual.without_refinement() == expected
    )


def walk(value):
    if isinstance(value, Node):
        yield value
        yield from walk(value.fields)
    elif isinstance(value, Argument):
        yield value
        yield from walk(value.expression)
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk(item)


def merge_proofs(flow, branches):
    continuing = [branch.nonzero for branch, returns in branches if not returns]
    if continuing:
        flow.nonzero = set.intersection(*continuing) & flow.bindings.keys()


class Checker:
    def __init__(self, declarations):
        self.functions = {}
        self.records = {}
        self.enums = {HOST_ERROR_KIND.name: {"EndOfInput": None, "FileNotFound": None, "Other": None}}
        self.current_function = None
        used_names = set(BUILTIN_NAMES)
        builtin_types = SIMPLE_TYPES | {"int32", "f64", "result", "list"}
        for declaration in declarations:
            token = declaration.token
            name = token.text
            if name in used_names or (not isinstance(declaration, Function) and name in builtin_types):
                raise LanguageError(token, f"name '{name}' is already in use")
            used_names.add(name)
            if isinstance(declaration, RecordDeclaration):
                fields = [field.name for field in declaration.fields]
                if len(fields) != len(set(fields)):
                    raise LanguageError(token, f"record '{name}' has duplicate fields")
                self.records[name] = builtin(name, ((f.name, f.type) for f in declaration.fields),
                                             Type(name), GUARANTEES)
            elif isinstance(declaration, EnumDeclaration):
                variants = {tag.text: payload for tag, payload in declaration.variants}
                if len(variants) != len(declaration.variants):
                    raise LanguageError(token, f"enum '{name}' has duplicate variants")
                if not variants:
                    raise LanguageError(token, f"enum '{name}' needs at least one variant")
                self.enums[name] = variants
            else:
                self.functions[name] = declaration
                self.check_signature(declaration)

    def check_signature(self, function):
        if "pure" in function.modifiers and any(p.mutable for p in function.parameters):
            raise LanguageError(function.token, "a pure function cannot accept a mut parameter")
        if "commutative" in function.modifiers:
            types = [parameter.type for parameter in function.parameters]
            if (not {"pure", "deterministic"} <= function.modifiers or len(types) != 2
                    or types[0] != types[1]):
                raise LanguageError(function.token,
                                    "commutative requires pure, deterministic and two equally typed parameters")
        if function.name == "main":
            capabilities = [parameter.type for parameter in function.parameters]
            if (any(parameter.mutable for parameter in function.parameters)
                    or not set(capabilities) <= {CONSOLE, FILESYSTEM}
                    or len(capabilities) != len(set(capabilities))):
                raise LanguageError(function.token,
                                    "main may accept one console and one filesystem, both read-only")

    def program(self):
        checked = {}
        for function in self.functions.values():
            self.current_function = function
            flow = Flow()
            for parameter in function.parameters:
                flow.define(parameter.token, parameter.type, parameter.mutable, parameter=True)
            body, returns = self.block(function.body, flow)
            if not returns and function.output != UNIT:
                raise LanguageError(function.token, f"some paths do not return {function.output}")
            checked[function.name] = Function(
                function.token, function.modifiers, function.parameters, function.output, body
            )
        self.check_termination(checked)
        return checked

    def check_termination(self, checked):
        active = set()
        proved = set()

        def visit(function):
            if function.name in proved:
                return
            active.add(function.name)
            for node in walk(function.body):
                if not isinstance(node, Node) or node.kind != "invoke":
                    continue
                name, _ = node.fields
                if name in active:
                    raise LanguageError(node.token,
                                        f"recursive call to '{name}' cannot guarantee termination")
                if name in checked:
                    visit(checked[name])
            active.remove(function.name)
            proved.add(function.name)

        for function in checked.values():
            if "terminates" in function.modifiers:
                visit(function)

    def block(self, statements, flow):
        checked = []
        returns = False
        for statement in statements:
            lowered, ends = self.statement(statement, flow)
            checked.append(lowered)
            returns = returns or ends
        return tuple(checked), returns

    def statement(self, node, flow):
        kind, fields = node.kind, node.fields
        if kind == "let":
            name, declared, mutable, expression = fields
            value = self.expression(expression, flow, declared)
            flow.define(name, declared or value.type.without_refinement(), mutable)
            if value.type.nonzero:
                flow.nonzero.add(name.text)
            lowered = (name.text, value.node)
        elif kind == "assign":
            name, expression = fields
            binding = self.writable(flow, name, name.text)
            value = self.expression(expression, flow, binding.type)
            flow.drop_proof(name.text)
            if value.type.nonzero:
                flow.nonzero.add(name.text)
            lowered = (name.text, value.node)
        elif kind == "return":
            value = self.expression(fields[0], flow, self.current_function.output)
            return Node(node.token, kind, (value.node,)), True
        elif kind == "expression":
            lowered = (self.expression(fields[0], flow).node,)
        elif kind == "if":
            condition, yes, no = fields
            value = self.expression(condition, flow, BOOL)
            yes_flow, no_flow = flow.copy(), flow.copy()
            self.narrow(condition, yes_flow, no_flow)
            yes_body, yes_returns = self.block(yes, yes_flow)
            no_body, no_returns = self.block(no, no_flow)
            merge_proofs(flow, ((yes_flow, yes_returns), (no_flow, no_returns)))
            return Node(node.token, kind, (value.node, yes_body, no_body)), yes_returns and no_returns
        elif kind == "match":
            return self.match_statement(node, flow)
        elif kind == "for":
            item, source, body = fields
            binding = flow.require(source, source.text)
            if binding.type.name != "list":
                raise LanguageError(source, "for requires a list variable")
            loop = flow.copy()
            loop.protected |= {source.text}
            loop.define(item, binding.type.arguments[0])
            lowered_body = self.loop_body(body, loop)
            flow.nonzero = loop.nonzero
            lowered = (item.text, source.text, lowered_body)
        elif kind == "loop":
            if "terminates" in self.current_function.modifiers:
                raise LanguageError(node.token, "an unbounded loop cannot guarantee termination")
            lowered_body = self.loop_body(fields[0], flow)
            return Node(node.token, kind, (lowered_body,)), True
        else:
            raise AssertionError(kind)
        return Node(node.token, kind, lowered), False

    def loop_body(self, body, flow):
        # A proof at the loop head must hold on entry and on every backedge.
        while True:
            iteration = flow.copy()
            lowered, ends = self.block(body, iteration)
            if ends or flow.nonzero <= iteration.nonzero:
                return lowered
            flow.nonzero &= iteration.nonzero

    def match_statement(self, node, flow):
        subject, arms = node.fields
        value = self.expression(subject, flow)
        variants = self.enums.get(value.type.name)
        if value.type.name == "result":
            variants = dict(zip(("Ok", "Err"), value.type.arguments))
        if variants is None:
            raise LanguageError(subject.token, "match requires a result or enum value")
        branches = []
        lowered = {}
        for tag, binding, body in arms:
            if tag.text not in variants:
                raise LanguageError(tag, f"'{tag.text}' is not a case of {value.type}")
            if tag.text in lowered:
                raise LanguageError(tag, f"duplicate match arm '{tag.text}'")
            payload = variants[tag.text]
            if (binding is None) != (payload is None):
                requirement = "cannot have" if payload is None else "needs"
                raise LanguageError(tag, f"'{tag.text}' {requirement} a payload binding")
            branch = flow.copy()
            if binding is not None:
                branch.define(binding, payload)
            lowered_body, ends = self.block(body, branch)
            branches.append((branch, ends))
            lowered[tag.text] = (binding.text if binding is not None else None, lowered_body)
        missing = variants.keys() - lowered.keys()
        if missing:
            raise LanguageError(node.token, f"match is missing arm(s): {', '.join(sorted(missing))}")
        merge_proofs(flow, branches)
        return Node(node.token, node.kind, (value.node, lowered)), all(ends for _, ends in branches)

    def narrow(self, condition, yes, no):
        if condition.kind != "binary" or condition.token.text not in {"==", "!="}:
            return
        variable, zero = condition.fields
        if variable.kind != "variable":
            variable, zero = zero, variable
        if variable.kind == "variable" and zero.kind == "as":
            zero = zero.fields[0]
        if variable.kind == "variable" and zero.kind == "integer" and zero.fields == (0,):
            target = yes if condition.token.text == "!=" else no
            target.nonzero.add(variable.fields[0])

    def writable(self, flow, token, name):
        binding = flow.require(token, name)
        if not binding.mutable:
            raise LanguageError(token, f"'{name}' needs a mut declaration")
        flow.ensure_unprotected(token, name)
        return binding

    def expression(self, node, flow, expected=None):
        checked = self.infer(node, flow, expected)
        if not compatible(checked.type, expected):
            raise LanguageError(node.token, f"expected {expected}, found {checked.type}")
        return checked

    def infer(self, node, flow, expected):
        kind, fields = node.kind, node.fields
        if kind == "value":
            value, value_type = fields
            return Checked(value_type, Node(node.token, kind, (value,)))
        if kind == "integer":
            value, = fields
            if not -(2**31) <= value < 2**31:
                raise LanguageError(node.token, "integer literal is outside the int32 range")
            base = expected.without_refinement() if expected and expected.name == "int32" else INT
            return Checked(base.refined() if value else base, Node(node.token, "value", (value,)))
        if kind == "variable":
            name, = fields
            if name not in flow.bindings and (name in self.functions or name in BUILTIN_NAMES):
                raise LanguageError(node.token, "a function cannot be used as a data value")
            flow.require(node.token, name)
            return Checked(flow.type_of(name), node)
        if kind == "as":
            expression, annotation = fields
            if expression.kind == "binary" and expression.token.text == "/":
                return self.division(expression, flow, annotation)
            value = self.expression(expression, flow, annotation)
            return Checked(annotation, value.node)
        if kind == "list":
            return self.list_expression(node, flow, expected)
        if kind == "field":
            record = self.expression(fields[0], flow)
            signature = self.records.get(record.type.name)
            if signature is not None:
                for index, field in enumerate(signature.parameters):
                    if field.name == node.token.text:
                        return Checked(field.type, Node(node.token, kind, (record.node, index)))
            raise LanguageError(node.token, f"{record.type} has no field '{node.token.text}'")
        if kind == "variant":
            name, expression = fields
            tag = node.token.text
            variants = self.enums.get(name, {})
            if tag not in variants:
                raise LanguageError(node.token, f"unknown enum case '{name}::{tag}'")
            payload = variants[tag]
            if (expression is None) != (payload is None):
                requirement = "cannot have" if payload is None else "needs"
                raise LanguageError(node.token, f"'{name}::{tag}' {requirement} a payload")
            value = None if expression is None else self.expression(expression, flow, payload).node
            return Checked(Type(name), Node(node.token, kind, (name, tag, value)))
        if kind == "call":
            return self.call(node, flow, expected)
        if kind == "binary":
            operator = node.token.text
            if operator == "/":
                raise LanguageError(node.token, "division requires an explicit 'as f64' result type")
            if operator == "+":
                arguments = tuple(Argument(operand.token, operand) for operand in fields)
                return self.call(Node(node.token, "call", arguments), flow, expected, name="add")
            return self.comparison(node, flow)
        raise AssertionError(kind)

    def list_expression(self, node, flow, expected):
        if expected and expected.name == "list":
            element = expected.arguments[0]
        else:
            element = self.type_hint(node.fields, flow)
            if element is None:
                if not node.fields:
                    raise LanguageError(node.token, "an empty list needs an explicit list type")
                element = INT
            element = element.without_refinement()
        values = [self.expression(item, flow, element).node for item in node.fields]
        return Checked(Type("list", (element,)), Node(node.token, "list", tuple(values)))

    def type_hint(self, nodes, flow, type_name=None):
        for node in nodes:
            if node.kind == "integer":
                continue
            try:
                inferred = self.expression(node, flow.copy()).type
            except LanguageError:
                continue
            if type_name is None or inferred.name == type_name:
                return inferred
        return None

    def comparison(self, node, flow):
        left_node, right_node = node.fields
        equality = node.token.text in {"==", "!="}
        hint = self.type_hint(node.fields, flow, None if equality else "int32") or INT
        base = hint.without_refinement()
        if base.name != "int32" and base not in {STRING, BOOL}:
            variants = self.enums.get(base.name)
            if variants is None or any(payload is not None for payload in variants.values()):
                raise LanguageError(node.token, f"comparison is unavailable for {base}")
        left = self.expression(left_node, flow, base)
        right = self.expression(right_node, flow, base)
        return Checked(BOOL, Node(node.token, "binary", (left.node, right.node)))

    def division(self, node, flow, annotation):
        if annotation.name != "f64":
            raise LanguageError(node.token, "division requires an explicit f64 result type")
        numerator, divisor = node.fields
        # The annotation and either operand can supply units for a bare literal.
        actual = [self.type_hint((expression,), flow, "int32") for expression in node.fields]
        left_units = actual[0].units if actual[0] else ()
        right_units = actual[1].units if actual[1] else ()
        if actual[0] is None and numerator.kind == "integer":
            left_units = quotient_units(annotation.units, quotient_units((), right_units))
        elif actual[1] is None and divisor.kind == "integer":
            right_units = quotient_units(left_units, annotation.units)
        left = self.expression(numerator, flow, Type("int32", units=left_units))
        try:
            right = self.expression(divisor, flow, Type("int32", units=right_units, nonzero=True))
        except LanguageError as error:
            if str(error).startswith("expected int32!0"):
                raise LanguageError(
                    error.token,
                    f"division needs a proven nonzero divisor ({error}); use 'if value != 0' or nonzero(value)",
                ) from error
            raise
        derived = Type("f64", units=quotient_units(left.type.units, right.type.units))
        if derived != annotation:
            raise LanguageError(node.token, f"division produces {derived}, found annotation {annotation}")
        return Checked(annotation, Node(node.token, "binary", (left.node, right.node)))

    def generic_signature(self, name, arguments, flow, expected):
        if name in {"at", "dropLast"}:
            items = self.type_hint(
                (arg.expression for arg in arguments if arg.label in {None, "items"}), flow, "list"
            )
            if name == "at" and items is None and expected and expected.name == "result":
                items = Type("list", (expected.arguments[0],))
            if name == "dropLast" and items is None and expected and expected.name == "list":
                items = expected
            if items is None:
                items = Type("list", (INT,))
            if name == "dropLast":
                return builtin(name, (("items", items),), items, GUARANTEES)
            return builtin(name, (("items", items), ("index", INT)),
                           result_type(items.arguments[0], INDEX_ERROR), GUARANTEES)
        hint = self.type_hint((arg.expression for arg in arguments), flow, "int32")
        if hint is None and expected and expected.name == "result":
            if expected.arguments[0].name == "int32":
                hint = expected.arguments[0]
        number = (hint or INT).without_refinement()
        if name == "nonzero":
            return builtin(name, (("value", number),), result_type(number.refined(), ZERO), GUARANTEES)
        modifiers = GUARANTEES | {"commutative"} if name == "add" else GUARANTEES
        return builtin(name, (("left", number), ("right", number)),
                       result_type(number, OVERFLOW), modifiers)

    def call(self, node, flow, expected, name=None):
        name = name or node.token.text
        arguments = node.fields
        if name in CONSTRUCTORS:
            if len(arguments) != 1 or arguments[0].mutable or arguments[0].label:
                raise LanguageError(node.token, f"{name} takes one unnamed data value")
            if expected is None or expected.name != "result":
                raise LanguageError(node.token, f"{name} needs an explicit result type")
            value = self.expression(arguments[0].expression, flow, expected.arguments[name == "Err"])
            return Checked(expected, Node(node.token, "result", (name, value.node)))
        if name in self.records:
            return self.construct_record(node, name, flow)
        if name == "prepend":
            return self.prepend(node, flow, expected)
        signature = self.functions.get(name) or HOST_FUNCTIONS.get(name) or PURE_FUNCTIONS.get(name)
        if name in GENERIC_FUNCTIONS:
            signature = self.generic_signature(name, arguments, flow, expected)
        if signature is None:
            raise LanguageError(node.token, f"unknown function '{name}'")
        missing = (self.current_function.modifiers & GUARANTEES) - signature.modifiers
        if missing:
            guarantees = ", ".join(sorted(missing))
            raise LanguageError(node.token, f"'{name}' lacks required guarantee(s): {guarantees}")
        return self.bind_arguments(node, name, signature, flow)

    def construct_record(self, node, name, flow):
        signature = self.records[name]
        if any(argument.label is None for argument in node.fields):
            raise LanguageError(node.token, f"'{name}' requires named fields")
        value = self.bind_arguments(node, name, signature, flow)
        _, plan = value.node.fields
        fields = tuple(parameter.name for parameter in signature.parameters)
        return Checked(signature.output, Node(node.token, "record", (name, fields, plan)))

    def prepend(self, node, flow, expected):
        candidates = []
        for argument in node.fields:
            inferred = self.type_hint((argument.expression,), flow)
            if inferred is None:
                continue
            if argument.label in {None, "items"} and inferred.name == "list":
                candidates.append(inferred)
            if argument.label in {None, "item"}:
                candidates.append(Type("list", (inferred.without_refinement(),)))
        if expected and expected.name == "list":
            candidates = [expected]
        candidates = list(dict.fromkeys(candidates or [Type("list", (INT,))]))
        solutions = []
        errors = []
        for items in candidates:
            signature = builtin("prepend", (("item", items.arguments[0]), ("items", items)),
                                items, GUARANTEES)
            trial = flow.copy()
            try:
                value = self.bind_arguments(node, "prepend", signature, trial)
            except LanguageError as error:
                if str(error).startswith("ambiguous call"):
                    raise
                errors.append(error)
            else:
                solutions.append((value, trial))
        if not solutions:
            raise errors[0]
        if len(solutions) > 1:
            raise LanguageError(node.token, "prepend has multiple possible list types; add an explicit type")
        value, trial = solutions[0]
        flow.nonzero = trial.nonzero
        return value

    def bind_arguments(self, node, name, signature, flow):
        arguments, parameters = node.fields, signature.parameters
        if len(arguments) != len(parameters):
            raise LanguageError(node.token, f"'{name}' expects {len(parameters)} arguments, got {len(arguments)}")
        slots = {parameter.name: index for index, parameter in enumerate(parameters)}
        reserved = set()
        for argument in arguments:
            if argument.label is None:
                continue
            if argument.label not in slots:
                raise LanguageError(argument.token, f"'{name}' has no parameter named '{argument.label}'")
            if argument.label in reserved:
                raise LanguageError(argument.token, f"argument '{argument.label}' is supplied twice")
            reserved.add(argument.label)
        open_slots = tuple(index for label, index in slots.items() if label not in reserved)
        solutions = []
        error = None
        error_depth = -1
        limit = 1 if "commutative" in signature.modifiers else 2

        def search(position, available, trial, plan, mutable_arguments):
            nonlocal error, error_depth
            if len(solutions) >= limit:
                return
            if position == len(arguments):
                try:
                    self.finish_mutable_arguments(trial, mutable_arguments)
                except LanguageError as failure:
                    error, error_depth = failure, position
                    return
                solutions.append((trial, plan))
                return
            argument = arguments[position]
            candidates = available if argument.label is None else (slots[argument.label],)
            for index in candidates:
                parameter = parameters[index]
                candidate = trial.copy()
                try:
                    value = self.check_argument(argument, parameter, candidate)
                except LanguageError as failure:
                    if position > error_depth:
                        error, error_depth = failure, position
                    continue
                search(position + 1, tuple(slot for slot in available if slot != index),
                       candidate, plan + ((index, argument.mutable, value),),
                       mutable_arguments + ((argument, parameter, value),) if argument.mutable
                       else mutable_arguments)
                if len(solutions) >= limit:
                    return

        search(0, open_slots, flow, (), ())
        if not solutions:
            if error is not None:
                raise error
            raise LanguageError(node.token, f"no argument binding fits '{name}'")
        if len(solutions) > 1:
            first, second = (solution[1] for solution in solutions)
            ambiguous = sorted({parameters[plan[position][0]].name for plan in (first, second)
                                for position in range(len(arguments))
                                if first[position][0] != second[position][0]})
            labels = ", ".join(ambiguous)
            raise LanguageError(node.token, f"ambiguous call to '{name}'; name arguments for {labels}")
        resolved, plan = solutions[0]
        flow.nonzero = resolved.nonzero
        return Checked(signature.output, Node(node.token, "invoke", (name, plan)))

    def check_argument(self, argument, parameter, flow):
        if argument.mutable != parameter.mutable:
            required = "requires" if parameter.mutable else "does not accept"
            raise LanguageError(argument.token, f"parameter '{parameter.name}' {required} a mut argument")
        if not argument.mutable:
            return self.expression(argument.expression, flow, parameter.type).node
        if argument.expression.kind != "variable":
            raise LanguageError(argument.token, "a mut argument must be a variable")
        name, = argument.expression.fields
        binding = self.writable(flow, argument.token, name)
        if (binding.type.without_refinement() != parameter.type.without_refinement()
                or (binding.type.nonzero and not parameter.type.nonzero)):
            raise LanguageError(argument.token, f"cannot pass mut {binding.type} to mut {parameter.type}")
        return name

    def finish_mutable_arguments(self, flow, arguments):
        for index, (argument, parameter, name) in enumerate(arguments):
            if not compatible(flow.type_of(name), parameter.type):
                raise LanguageError(argument.token, f"'{name}' needs a nonzero proof for mut {parameter.type}")
            for _, other, alias in arguments[:index]:
                if parameter.type.nonzero != other.type.nonzero and flow.may_alias(name, alias):
                    raise LanguageError(argument.token, f"'{name}' and '{alias}' may alias with different refinements")
        for _, parameter, name in arguments:
            if not parameter.type.nonzero:
                flow.drop_proof(name)
