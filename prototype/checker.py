"""Static checking and lowering to an interpreter-ready program."""

from collections import defaultdict
from dataclasses import dataclass, field, replace

from .syntax import (
    BOOL, CAPABILITIES, CONSOLE, EXAMPLE_ERROR_MESSAGES, FILESYSTEM, FLOAT, GUARANTEES, HOST_ERROR,
    HOST_ERROR_KIND, INDEX_ERROR, INT, INTEGER_BOUNDS, OVERFLOW, PARSE_INT_ERROR, RANDOM, RANDOM_DRAW, SIMPLE_TYPES,
    STRING, UINT, UNIT, ZERO, Argument, EnumDeclaration, Function, LanguageError, Node, Parameter,
    RecordDeclaration, Token, Type, product_units, quotient_units, result_type,
)


def builtin(name, parameters, output, modifiers=frozenset(), commutative=()):
    if commutative:
        modifiers = modifiers | {"commutative"}
    return Function(Token(name, 0, 0), modifiers, tuple(
        Parameter(Token(label, 0, 0), parameter_type)
        for label, parameter_type in parameters
    ), output, commutative=commutative)


HOST_FUNCTIONS = {
    function.name: function for function in (
        builtin("print", (("console", CONSOLE), ("text", STRING)), result_type(UNIT, HOST_ERROR)),
        builtin("write", (("console", CONSOLE), ("text", STRING)), result_type(UNIT, HOST_ERROR)),
        builtin("input", (("console", CONSOLE),), result_type(STRING, HOST_ERROR)),
        builtin("enableKeyInput", (("console", CONSOLE),), result_type(BOOL, HOST_ERROR)),
        builtin("disableKeyInput", (("console", CONSOLE),), result_type(UNIT, HOST_ERROR)),
        builtin("readKeysFor", (("console", CONSOLE), ("timeout", Type("int32", units=(("ms", 1),)))),
                result_type(Type("list", (STRING,)), HOST_ERROR)),
        builtin("readFileUTF8", (("filesystem", FILESYSTEM), ("path", STRING)),
                result_type(STRING, HOST_ERROR)),
        builtin("writeFileUTF8", (("filesystem", FILESYSTEM), ("path", STRING), ("text", STRING)),
                result_type(UNIT, HOST_ERROR)),
        builtin("replaceFileUTF8", (("filesystem", FILESYSTEM), ("path", STRING), ("text", STRING)),
                result_type(UNIT, HOST_ERROR)),
        builtin("random", (("source", RANDOM), ("bound0", INT), ("bound1", INT)),
                result_type(INT, HOST_ERROR), commutative=("bound0", "bound1")),
    )
}
HOST_ERROR_CONSTRUCTOR = builtin("HostError", (("kind", HOST_ERROR_KIND), ("message", STRING)),
                                 HOST_ERROR, GUARANTEES)
PURE_FUNCTIONS = {
    function.name: function for function in (
        builtin("concat", (("left", STRING), ("right", STRING)), STRING, GUARANTEES),
        builtin("splitLines", (("text", STRING),), Type("list", (STRING,)), GUARANTEES),
        builtin("parseInt32", (("text", STRING),), result_type(INT, PARSE_INT_ERROR), GUARANTEES),
        builtin("formatInt32", (("value", INT),), STRING, GUARANTEES),
        builtin("hostErrorKind", (("error", HOST_ERROR),), HOST_ERROR_KIND, GUARANTEES),
        builtin("describeHostError", (("error", HOST_ERROR),), STRING, GUARANTEES),
        builtin("pseudoRandom", (("state", INT), ("bound0", INT), ("bound1", INT)),
                RANDOM_DRAW, GUARANTEES, ("bound0", "bound1")),
    )
}
GENERIC_FUNCTIONS = {"add", "subtract", "min", "max", "nonzero", "at", "prepend", "dropLast", "toUint32"}
CONSTRUCTORS = {"Ok", "Err"}
BUILTIN_NAMES = HOST_FUNCTIONS.keys() | PURE_FUNCTIONS.keys() | GENERIC_FUNCTIONS | CONSTRUCTORS
LITERAL_VALUE_KINDS = {"value", "integer", "decimal", "list", "variant", "call"}


@dataclass(frozen=True)
class Binding:
    token: Token
    type: Type
    mutable: bool
    borrowed: bool = False


@dataclass
class Flow:
    bindings: dict = field(default_factory=dict)
    nonzero: set = field(default_factory=set)
    protected: frozenset = frozenset()
    literal_context: str | None = None
    moved: set = field(default_factory=set)
    loans: frozenset = frozenset()
    write_loans: frozenset = frozenset()

    def copy(self):
        return replace(self, bindings=self.bindings.copy(), nonzero=self.nonzero.copy(),
                       moved=self.moved.copy())

    def type_of(self, name):
        declared = self.bindings[name].type
        return declared.refined() if name in self.nonzero else declared

    def ensure_unprotected(self, token, name):
        if name in self.protected:
            raise LanguageError(token, f"loop source '{name}' is read-only in the loop body")
        if name in self.loans:
            raise LanguageError(token, f"'{name}' is already borrowed by this call")

    def drop_proof(self, name):
        self.nonzero.discard(name)

    def define(self, token, value_type, mutable=False, borrowed=False):
        name = token.text
        if name in self.bindings:
            raise LanguageError(token, f"data variable '{name}' is already defined in this scope")
        self.bindings[name] = Binding(token, value_type, mutable, borrowed)

    def require(self, token, name, *, read=True):
        if name not in self.bindings:
            raise LanguageError(token, f"unknown data variable '{name}'")
        if read and name in self.moved:
            raise LanguageError(token, f"'{name}' was moved and needs reassignment")
        return self.bindings[name]

    def move(self, token, name):
        binding = self.require(token, name)
        self.ensure_unprotected(token, name)
        if binding.borrowed:
            raise LanguageError(token, f"cannot move borrowed value '{name}'")
        self.moved.add(name)
        self.drop_proof(name)

    def commit(self, trial):
        self.nonzero = trial.nonzero
        self.moved = trial.moved


@dataclass(frozen=True)
class Checked:
    type: Type
    node: Node
    call: tuple | None = None


@dataclass(frozen=True)
class CheckedExample:
    token: Token
    description: str | None
    inputs: tuple
    output: Node
    after: tuple


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


def merge_flow(flow, branches):
    continuing = [branch for branch, returns in branches if not returns]
    if continuing:
        flow.nonzero = set.intersection(*(branch.nonzero for branch in continuing)) & flow.bindings.keys()
        flow.moved = set.union(*(branch.moved for branch in continuing)) & flow.bindings.keys()


def source_binding(node):
    while node.kind in {"field", "as"}:
        node = node.fields[0]
    return node.fields[0] if node.kind == "variable" else None


class Checker:
    def __init__(self, declarations, *, module=None, imports=None, entry=True, mock=None, capture=False):
        self.facts = [] if capture else None
        self.mock_header = mock
        self.mock_operations = {} if mock is None else {
            name: function for name, function in HOST_FUNCTIONS.items()
            if function.parameters[0].type == CAPABILITIES[mock.text]
        }
        self.functions = {}
        self.imported_functions = {}
        self.record_names = {RANDOM_DRAW.name: RANDOM_DRAW}
        self.enum_names = {HOST_ERROR_KIND.name: HOST_ERROR_KIND}
        self.records = {RANDOM_DRAW: builtin(RANDOM_DRAW.name, (("value", INT), ("state", INT)),
                                             RANDOM_DRAW, GUARANTEES)}
        self.enums = {HOST_ERROR_KIND: {"EndOfInput": None, "FileNotFound": None, "Other": None}}
        self.public_types = {
            declaration.token.text: Type(declaration.token.text, module=module)
            for declaration in declarations
            if not isinstance(declaration, Function) and declaration.exported
        }
        self.private_types = {
            Type(declaration.token.text, module=module) for declaration in declarations
            if not isinstance(declaration, Function) and not declaration.exported
        }
        for namespace, dependency in (imports or {}).items():
            self.records.update(dependency.records)
            self.enums.update(dependency.enums)
            for name, value_type in dependency.public_types.items():
                names = self.record_names if value_type in self.records else self.enum_names
                names[f"{namespace}.{name}"] = value_type
            self.imported_functions.update({
                f"{namespace}.{name}": function for name, function in dependency.functions.items()
                if function.exported
            })
        self.current_function = None
        used_names = (BUILTIN_NAMES - self.mock_operations.keys()) | {RANDOM_DRAW.name}
        builtin_types = SIMPLE_TYPES | INTEGER_BOUNDS.keys() | {"Uint32", "f64", "result", "list"}
        for declaration in declarations:
            token = declaration.token
            name = token.text
            mock_export = (isinstance(declaration, Function) and name in self.mock_operations
                           and declaration.exported)
            if mock and declaration.exported and not mock_export:
                raise LanguageError(token, f"unexpected export '{name}' in {mock.text} mock")
            if name in self.mock_operations and not mock_export:
                raise LanguageError(token, f"mock operation '{name}' must be exported as a function")
            if mock and name == HOST_ERROR.name:
                raise LanguageError(token, f"name '{name}' is already in use")
            if name in used_names or (not isinstance(declaration, Function) and name in builtin_types):
                raise LanguageError(token, f"name '{name}' is already in use")
            used_names.add(name)
            value_type = Type(name, module=module)
            if isinstance(declaration, RecordDeclaration):
                fields = [field.name for field in declaration.fields]
                if len(fields) != len(set(fields)):
                    raise LanguageError(token, f"record '{name}' has duplicate fields")
                if declaration.exported:
                    for parameter in declaration.fields:
                        self.check_public_type(parameter.type, parameter.token)
                self.record_names[name] = value_type
                self.records[value_type] = builtin(name, ((f.name, f.type) for f in declaration.fields),
                                                   value_type, GUARANTEES)
            elif isinstance(declaration, EnumDeclaration):
                variants = {tag.text: payload for tag, payload in declaration.variants}
                if len(variants) != len(declaration.variants):
                    raise LanguageError(token, f"enum '{name}' has duplicate variants")
                if not variants:
                    raise LanguageError(token, f"enum '{name}' needs at least one variant")
                if declaration.exported:
                    for tag, payload in declaration.variants:
                        if payload is not None:
                            self.check_public_type(payload, tag)
                self.enum_names[name] = value_type
                self.enums[value_type] = variants
            else:
                function = replace(declaration, module=module)
                self.functions[name] = function
                self.check_signature(function, entry and mock is None)
                if function.exported and not mock_export:
                    self.check_public_type(function.output, token)
                    for parameter in function.parameters:
                        self.check_public_type(parameter.type, parameter.token)

        self.state_type = self.check_mock_contract() if mock else None

    def check_mock_contract(self):
        state_type = None
        for name, host in self.mock_operations.items():
            function = self.functions.get(name)
            if function is None:
                raise LanguageError(self.mock_header, f"{self.mock_header.text} mock is missing export '{name}'")
            parameters = function.parameters
            signature = tuple((p.name, p.type, p.mutable, p.owning) for p in parameters[1:])
            if (not parameters or parameters[0].name != "state" or not parameters[0].mutable
                    or signature != tuple((p.name, p.type, p.mutable, p.owning) for p in host.parameters[1:])
                    or function.output != host.output
                    or function.commutative_slots != host.commutative_slots):
                remaining = [f"{p.name}: {p.type}" for p in host.parameters[1:]]
                arguments = ", ".join(["state: mut <state type>", *remaining])
                commutative = f"commutative {' '.join(host.commutative)} " if host.commutative else ""
                raise LanguageError(function.token,
                                    f"mock export must match '{commutative}fn {name}({arguments}) -> {host.output}'")
            state = parameters[0]
            if state_type is None:
                state_type = state.type
            elif state.type != state_type:
                raise LanguageError(state.token, f"expected shared mock state type {state_type}, found {state.type}")
        init = self.functions.get("init")
        if init and (init.parameters or init.output != state_type or not {"pure", "ending"} <= init.modifiers):
            raise LanguageError(init.token, f"mock initializer must match 'pure ending fn init() -> {state_type}'")
        return state_type

    def check_public_type(self, value_type, token):
        if value_type in self.private_types:
            raise LanguageError(token, f"exported declaration uses private type '{value_type}'")
        for argument in value_type.arguments:
            self.check_public_type(argument, token)

    def contains_exclusive(self, value_type, seen=frozenset()):
        if value_type in seen:
            return False
        if value_type.name in CAPABILITIES and value_type.module is None:
            return not value_type.shared
        if value_type in self.records:
            members = (field.type for field in self.records[value_type].parameters)
        elif value_type in self.enums:
            members = (payload for payload in self.enums[value_type].values() if payload is not None)
        else:
            members = value_type.arguments
        return any(self.contains_exclusive(member, seen | {value_type}) for member in members)

    def check_signature(self, function, entry):
        if function.examples and not {"deterministic", "ending"} <= function.modifiers:
            raise LanguageError(function.examples[0].token,
                                "examples require explicit 'deterministic' and 'ending' modifiers")
        if "pure" in function.modifiers and any(p.mutable for p in function.parameters):
            raise LanguageError(function.token, "a pure function cannot accept a mut parameter")
        if "commutative" in function.modifiers:
            slots = function.commutative_slots
            group = [parameter for index, parameter in enumerate(function.parameters) if index in slots]
            if function.commutative:
                if len(slots) < 2 or len(slots) != len(function.commutative):
                    raise LanguageError(function.token, "commutative must name two or more distinct parameters")
            elif len(slots) != 2:
                raise LanguageError(function.token, "commutative must name its parameters on functions with arity other than two")
            if any(parameter.mutable or parameter.owning for parameter in group) or len({parameter.type for parameter in group}) != 1:
                raise LanguageError(function.token, "commutative parameters must be read-only and have the same type")
        if entry and function.name == "main":
            capabilities = [replace(parameter.type, shared=False) for parameter in function.parameters]
            if (any(parameter.mutable for parameter in function.parameters)
                    or not set(capabilities) <= set(CAPABILITIES.values())
                    or len(capabilities) != len(set(capabilities))):
                raise LanguageError(function.token,
                                    "main may accept one parameter for each host capability")

    def program(self):
        checked = {}
        for function in self.functions.values():
            self.current_function = function
            for parameter in function.parameters:
                if parameter.owning and parameter.type.shared:
                    raise LanguageError(parameter.token, "own cannot consume a shared value")
            flow = Flow()
            for parameter in function.parameters:
                borrowed = parameter.mutable or (
                    not parameter.owning and self.contains_exclusive(parameter.type)
                )
                flow.define(parameter.token, parameter.type, parameter.mutable, borrowed)
            body, returns = self.block(function.body, flow)
            if not returns and function.output != UNIT:
                raise LanguageError(function.token, f"some paths do not return {function.output}")
            self.check_commutativity(function, body)
            examples = tuple(self.check_example(function, example) for example in function.examples)
            checked[function.key] = replace(function, body=body, examples=examples)
        self.check_termination(checked)
        self.check_mutations(checked)
        if self.facts is not None:
            retained = set()
            for function in checked.values():
                roots = [function.body]
                for example in function.examples:
                    roots.extend((example.inputs, example.output, example.after))
                retained.update(id(node) for node in walk(tuple(roots)))
            self.facts = [(token, kind, value) for owner, token, kind, value in self.facts
                          if id(owner) in retained]
        return checked

    def remember(self, owner, token, kind, value):
        # Speculative checks can produce different facts for the same source token.
        # Keep the owners alive and publish only facts from the selected lowered tree.
        if self.facts is not None:
            self.facts.append((owner, token, kind, value))

    def check_mutations(self, checked):
        parameters = {
            (function.key, slot): parameter.token
            for function in checked.values()
            for slot, parameter in enumerate(function.parameters)
            if parameter.mutable
        }
        declarations = {}
        written = set()
        callers = defaultdict(set)
        for function in checked.values():
            for parameter in function.parameters:
                # The interface always passes state by reference, even for read-only operations.
                if parameter.mutable and function.name not in self.mock_operations:
                    declarations[parameter.token] = "parameter"
            for node in walk(function.body):
                if node.kind == "let" and node.fields[2]:
                    declarations[node.fields[0]] = "variable"
                elif node.kind == "assign":
                    written.add(node.fields[0])
                elif node.kind == "invoke":
                    callee, plan, _ = node.fields
                    for slot, mutable, binding in plan:
                        if not mutable:
                            continue
                        if callee in checked:
                            callers[parameters[callee, slot]].add(binding)
                        else:
                            # Imported functions have already passed this check.
                            written.add(binding)

        # Propagate writes backwards through resolved calls. Forwarding cycles
        # only qualify when a write can reach them.
        pending = list(written)
        while pending:
            for binding in callers[pending.pop()] - written:
                written.add(binding)
                pending.append(binding)
        for token, kind in declarations.items():
            if token not in written:
                raise LanguageError(token, f"mut {kind} '{token.text}' is never written")

    def check_example(self, function, example):
        parameters = {parameter.name: parameter for parameter in function.parameters}
        inputs = {}
        after = {}
        output = None
        flow = Flow(literal_context="example")
        for entry in example.entries:
            kind = entry.token.text
            if kind == "expect":
                if output is not None:
                    raise LanguageError(entry.token, "example has more than one 'expect' entry")
                output = self.expression(entry.value, flow, function.output).node
                continue
            name = entry.parameter.text
            parameter = parameters.get(name)
            if parameter is None:
                raise LanguageError(entry.parameter, f"'{function.name}' has no parameter named '{name}'")
            if kind == "after" and not parameter.mutable:
                raise LanguageError(entry.parameter, f"'after' requires a mut parameter, but '{name}' is read-only")
            values = inputs if kind == "input" else after
            if name in values:
                raise LanguageError(entry.parameter, f"duplicate '{kind}' entry for '{name}'")
            values[name] = self.expression(entry.value, flow, parameter.type).node
            self.remember(values[name], entry.parameter, "local_reference", parameter.token)

        for parameter in function.parameters:
            name = parameter.name
            if name not in inputs:
                raise LanguageError(example.token, f"example is missing an 'input' entry for '{name}'")
            if parameter.mutable and name not in after:
                raise LanguageError(example.token, f"example is missing an 'after' entry for '{name}'")
        if output is None:
            raise LanguageError(example.token, "example is missing an 'expect' entry")
        return CheckedExample(example.token, example.description, tuple(inputs[name] for name in parameters),
                              output, tuple((name, after[name]) for name in parameters if name in after))

    def check_commutativity(self, function, body):
        slots = function.commutative_slots
        if not slots:
            return
        names = tuple(parameter.name for index, parameter in enumerate(function.parameters) if index in slots)
        group = set(names)
        allowed = set()
        # Lowered calls carry their actual destination slots. Walk visits each call
        # before its operands, so only direct uses in a complete group are allowed.
        for node in walk(body):
            if node.kind == "invoke":
                _, plan, destinations = node.fields
            elif node.kind == "binary" and node.fields[2]:
                left, right, *_ = node.fields
                plan = ((0, False, left), (1, False, right))
                destinations = {0, 1}
            else:
                if node.kind == "variable" and node.fields[0] in group and id(node) not in allowed:
                    raise LanguageError(node.token,
                                        f"commutative parameters ({', '.join(names)}) must be passed together "
                                        "exactly once to a commutative parameter group")
                continue
            members = [(slot, value) for slot, mutable, value in plan
                       if not mutable and value.kind == "variable" and value.fields[0] in group]
            if (len(members) == len(names)
                    and {value.fields[0] for _, value in members} == group
                    and all(slot in destinations for slot, _ in members)):
                allowed.update(id(value) for _, value in members)

    def check_termination(self, checked):
        active = set()
        proved = set()

        def visit(function):
            if function.key in proved:
                return
            active.add(function.key)
            for node in walk(function.body):
                if not isinstance(node, Node) or node.kind != "invoke":
                    continue
                name, _, _ = node.fields
                if name in active:
                    raise LanguageError(node.token,
                                        f"recursive call to '{checked[name].name}' cannot guarantee termination")
                if name in checked:
                    visit(checked[name])
            active.remove(function.key)
            proved.add(function.key)

        for function in checked.values():
            if "ending" in function.modifiers:
                visit(function)

    def block(self, statements, flow, *, reachable=True):
        checked = []
        ends = not reachable
        for statement in statements:
            if ends:
                raise LanguageError(statement.token, "unreachable statement")
            lowered, ends = self.statement(statement, flow)
            checked.append(lowered)
        return tuple(checked), ends

    def statement(self, node, flow):
        kind, fields = node.kind, node.fields
        facts = []
        if kind == "let":
            name, declared, mutable, expression = fields
            value = self.expression(expression, flow, declared, transfer=True)
            flow.define(name, declared or value.type.without_refinement(), mutable)
            if value.type.nonzero:
                flow.nonzero.add(name.text)
            lowered = (name, value.node, mutable)
            facts.append((name, "local_type", flow.bindings[name.text].type))
        elif kind == "assign":
            name, expression = fields
            binding = self.writable(flow, name, name.text, read=False)
            value = self.expression(expression, flow, binding.type, transfer=True)
            flow.moved.discard(name.text)
            flow.drop_proof(name.text)
            if value.type.nonzero:
                flow.nonzero.add(name.text)
            lowered = (binding.token, value.node)
            facts.append((name, "local_reference", binding.token))
        elif kind == "return":
            value = self.expression(fields[0], flow, self.current_function.output, transfer=True)
            return Node(node.token, kind, (value.node,)), True
        elif kind == "expression":
            lowered = (self.expression(fields[0], flow).node,)
        elif kind == "if":
            condition, yes, no = fields
            value = self.expression(condition, flow, BOOL)
            yes_flow, no_flow = flow.copy(), flow.copy()
            self.narrow(condition, yes_flow, no_flow)
            constant = condition.fields[0] if condition.kind == "value" else None
            yes_body, yes_returns = self.block(yes, yes_flow, reachable=constant is not False)
            no_body, no_returns = self.block(no, no_flow, reachable=constant is not True)
            merge_flow(flow, ((yes_flow, yes_returns), (no_flow, no_returns)))
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
            element = binding.type.arguments[0]
            loop.define(item, element, borrowed=self.contains_exclusive(element))
            lowered_body = self.loop_body(body, loop)
            flow.commit(loop)
            flow.nonzero &= flow.bindings.keys()
            flow.moved &= flow.bindings.keys()
            lowered = (item.text, Node(source, "variable", (source.text,)), lowered_body)
            facts.extend(((item, "local_type", loop.bindings[item.text].type),
                          (source, "local_reference", binding.token)))
        elif kind == "loop":
            if "ending" in self.current_function.modifiers:
                raise LanguageError(node.token, "an unbounded loop cannot guarantee termination")
            lowered_body = self.loop_body(fields[0], flow)
            return Node(node.token, kind, (lowered_body,)), True
        else:
            raise AssertionError(kind)
        result = Node(node.token, kind, lowered)
        for token, fact, value in facts:
            self.remember(result, token, fact, value)
        return result, False

    def loop_body(self, body, flow):
        # A proof at the loop head must hold on entry and on every backedge.
        while True:
            iteration = flow.copy()
            lowered, ends = self.block(body, iteration)
            if ends:
                return lowered
            available_proofs = flow.nonzero & iteration.nonzero
            possible_moves = flow.moved | (iteration.moved & flow.bindings.keys())
            if available_proofs == flow.nonzero and possible_moves == flow.moved:
                return lowered
            flow.nonzero = available_proofs
            flow.moved = possible_moves

    def match_statement(self, node, flow):
        subject, arms = node.fields
        value = self.expression(subject, flow)
        source = source_binding(subject)
        borrowed = source is not None and flow.bindings[source].borrowed
        if self.contains_exclusive(value.type) and source is not None and not borrowed:
            flow.move(subject.token, source)
        variants = self.enums.get(value.type)
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
                branch.define(binding, payload, borrowed=borrowed and self.contains_exclusive(payload))
            lowered_body, ends = self.block(body, branch)
            branches.append((branch, ends))
            lowered[tag.text] = (binding.text if binding is not None else None, lowered_body)
        missing = variants.keys() - lowered.keys()
        if missing:
            raise LanguageError(node.token, f"match is missing arm(s): {', '.join(sorted(missing))}")
        merge_flow(flow, branches)
        result = Node(node.token, node.kind, (value.node, lowered))
        for tag, binding, _ in arms:
            self.remember(result, tag, "case_reference", (value.type, tag.text))
            if binding is not None:
                self.remember(result, binding, "local_type", variants[tag.text])
        return result, all(ends for _, ends in branches)

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

    def writable(self, flow, token, name, *, read=True):
        binding = flow.require(token, name, read=read)
        if not binding.mutable:
            raise LanguageError(token, f"'{name}' needs a mut declaration")
        flow.ensure_unprotected(token, name)
        return binding

    def expression(self, node, flow, expected=None, *, transfer=False, consume=False,
                   borrow_shared=False):
        checked = self.infer(node, flow, expected)
        actual = checked.type
        shared_borrow = borrow_shared and actual.shared and replace(actual, shared=False) == expected
        if consume and actual.shared:
            raise LanguageError(node.token, "own cannot consume a shared value")
        if not compatible(actual, expected) and not shared_borrow:
            raise LanguageError(node.token, f"expected {expected}, found {checked.type}")
        if consume or (transfer and self.contains_exclusive(actual)):
            source = source_binding(node)
            if source is not None:
                flow.move(node.token, source)
        self.remember(checked.node, node.token, "expression_type", checked.type)
        if checked.call is not None:
            _, bindings = checked.call
            self.remember(checked.node, node.token, "call", checked.call)
            if node.kind == "call":
                for argument, (_, _, reference) in zip(node.fields, bindings):
                    if reference is not None:
                        self.remember(checked.node, argument.expression.token, "local_reference", reference)
        return checked

    def literal(self, node, expected):
        return self.expression(node, Flow(literal_context="mock state"), expected).node

    def infer(self, node, flow, expected):
        kind, fields = node.kind, node.fields
        if flow.literal_context and kind not in LITERAL_VALUE_KINDS:
            raise LanguageError(node.token, f"{flow.literal_context} values must be literals")
        if kind == "value":
            value, value_type = fields
            return Checked(value_type, Node(node.token, kind, (value,)))
        if kind == "integer":
            value, = fields
            base = expected.without_refinement() if expected and expected.name in INTEGER_BOUNDS else INT
            lower, upper = INTEGER_BOUNDS[base.name]
            if not lower <= value <= upper or (base.name == UINT.name and node.token.text == "-"):
                raise LanguageError(node.token, f"integer literal is outside the {base.name} range")
            return Checked(base.refined() if value else base, Node(node.token, "value", (value,)))
        if kind == "decimal":
            value_type = expected if expected and expected.name == "f64" else FLOAT
            return Checked(value_type, Node(node.token, "value", fields))
        if kind == "variable":
            name, = fields
            if name not in flow.bindings and (name in self.functions or name in BUILTIN_NAMES):
                raise LanguageError(node.token, "a function cannot be used as a data value")
            binding = flow.require(node.token, name)
            lowered = replace(node) if self.facts is not None else node
            self.remember(lowered, node.token, "local_reference", binding.token)
            return Checked(flow.type_of(name), lowered)
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
            signature = self.records.get(record.type)
            if signature is not None:
                for index, field in enumerate(signature.parameters):
                    if field.name == node.token.text:
                        lowered = Node(node.token, kind, (record.node, index))
                        self.remember(lowered, node.token, "field_reference", (record.type, field.name))
                        return Checked(field.type, lowered)
            raise LanguageError(node.token, f"{record.type} has no field '{node.token.text}'")
        if kind == "variant":
            name, expression = fields
            tag = node.token.text
            value_type = self.enum_names.get(name)
            variants = self.enums.get(value_type, {})
            if tag not in variants:
                raise LanguageError(node.token, f"unknown enum case '{name}::{tag}'")
            payload = variants[tag]
            if (expression is None) != (payload is None):
                requirement = "cannot have" if payload is None else "needs"
                raise LanguageError(node.token, f"'{name}::{tag}' {requirement} a payload")
            value = None if expression is None else self.expression(expression, flow, payload, transfer=True).node
            lowered = Node(node.token, kind, (value_type.name, tag, value))
            self.remember(lowered, node.token, "case_reference", (value_type, tag))
            return Checked(value_type, lowered)
        if kind == "call":
            return self.call(node, flow, expected)
        if kind == "binary":
            operator = node.token.text
            if operator == "/":
                raise LanguageError(node.token, "division requires an explicit 'as f64' result type")
            if operator == "+":
                arguments = tuple(Argument(operand.token, operand) for operand in fields)
                return self.call(Node(node.token, "call", arguments), flow, expected, name="add")
            if operator == "*":
                return self.multiplication(node, flow, expected)
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
        values = [self.expression(item, flow, element, transfer=True).node for item in node.fields]
        return Checked(Type("list", (element,)), Node(node.token, "list", tuple(values)))

    def type_hint(self, nodes, flow, names=None):
        for node in nodes:
            if node.kind == "integer":
                continue
            try:
                inferred = self.expression(node, flow.copy()).type
            except LanguageError:
                continue
            if names is None or inferred.name in names:
                return inferred
        return None

    def comparison(self, node, flow):
        left_node, right_node = node.fields
        equality = node.token.text in {"==", "!="}
        hint = self.type_hint(node.fields, flow, None if equality else INTEGER_BOUNDS) or INT
        base = hint.without_refinement()
        if base.name not in INTEGER_BOUNDS and base not in {STRING, BOOL}:
            variants = self.enums.get(base)
            if variants is None or any(payload is not None for payload in variants.values()):
                raise LanguageError(node.token, f"comparison is unavailable for {base}")
        left = self.expression(left_node, flow, base)
        right = self.expression(right_node, flow, base)
        commutative = equality and base.name in INTEGER_BOUNDS
        return Checked(BOOL, Node(node.token, "binary", (left.node, right.node, commutative)))

    def multiplication(self, node, flow, expected):
        hint = self.type_hint(node.fields, flow, INTEGER_BOUNDS)
        if hint is None and expected and expected.name == "result":
            hint = expected.arguments[0]
        scalar = UINT if hint and hint.name == UINT.name else INT
        left, right = (
            self.expression(operand, flow, scalar if operand.kind == "integer" else None)
            for operand in node.fields
        )
        if left.type.name != scalar.name or right.type.name != scalar.name:
            raise LanguageError(node.token, f"multiplication expects two {scalar.name} operands")
        number = Type(scalar.name, units=product_units(left.type.units, right.type.units))
        return Checked(result_type(number, OVERFLOW),
                       Node(node.token, "binary", (left.node, right.node, True, scalar.name)))

    def division(self, node, flow, annotation):
        if annotation.name != "f64":
            raise LanguageError(node.token, "division requires an explicit f64 result type")
        numerator, divisor = node.fields
        # The annotation and either operand can supply units for a bare literal.
        actual = [self.type_hint((expression,), flow, INTEGER_BOUNDS) for expression in node.fields]
        integer = next((value.name for value in actual if value is not None), INT.name)
        left_units = actual[0].units if actual[0] else ()
        right_units = actual[1].units if actual[1] else ()
        if actual[0] is None and numerator.kind == "integer":
            left_units = quotient_units(annotation.units, quotient_units((), right_units))
        elif actual[1] is None and divisor.kind == "integer":
            right_units = quotient_units(left_units, annotation.units)
        left = self.expression(numerator, flow, Type(integer, units=left_units))
        try:
            right = self.expression(divisor, flow, Type(integer, units=right_units, nonzero=True))
        except LanguageError as error:
            if str(error).startswith(f"expected {integer}!0"):
                raise LanguageError(
                    error.token,
                    f"division needs a proven nonzero divisor ({error}); use 'if value != 0' or nonzero(value)",
                ) from error
            raise
        derived = Type("f64", units=quotient_units(left.type.units, right.type.units))
        if derived != annotation:
            raise LanguageError(node.token, f"division produces {derived}, found annotation {annotation}")
        lowered = Node(node.token, "binary", (left.node, right.node, False))
        self.remember(lowered, node.token, "expression_type", annotation)
        return Checked(annotation, lowered)

    def generic_signature(self, name, arguments, flow, expected):
        if name in {"at", "dropLast"}:
            items = self.type_hint(
                (arg.expression for arg in arguments if arg.label in {None, "items"}), flow, {"list"}
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
        values = (arg.expression for arg in arguments)
        if name == "toUint32":
            source = self.type_hint(values, flow, {INT.name})
            if source is None and expected and expected.name == "result":
                source = expected.arguments[0]
            units = source.units if source else ()
            return builtin(name, (("value", Type(INT.name, units=units)),),
                           result_type(Type(UINT.name, units=units), OVERFLOW), GUARANTEES)
        hint = self.type_hint(values, flow, INTEGER_BOUNDS)
        if hint is None and expected:
            hint = expected.arguments[0] if expected.name == "result" else expected
        number = hint.without_refinement() if hint and hint.name in INTEGER_BOUNDS else INT
        if name == "nonzero":
            return builtin(name, (("value", number),), result_type(number.refined(), ZERO), GUARANTEES)
        direct = name in {"min", "max"}
        group = () if name == "subtract" else ("left", "right")
        operation = f"uint32:{name}" if not direct and number.name == UINT.name else name
        return builtin(operation, (("left", number), ("right", number)),
                       number if direct else result_type(number, OVERFLOW), GUARANTEES, group)

    def call(self, node, flow, expected, name=None):
        name = name or node.token.text
        arguments = node.fields
        if name in CONSTRUCTORS:
            if len(arguments) != 1 or arguments[0].mutable or arguments[0].label:
                raise LanguageError(node.token, f"{name} takes one unnamed data value")
            if expected is None or expected.name != "result":
                raise LanguageError(node.token, f"{name} needs an explicit result type")
            value = self.expression(arguments[0].expression, flow, expected.arguments[name == "Err"],
                                    transfer=True)
            call = (name, (("value", False, None),)) if self.facts is not None else None
            return Checked(expected, Node(node.token, "result", (name, value.node)), call)
        if name in self.record_names:
            return self.construct_record(node, name, flow)
        if self.mock_header and name == HOST_ERROR.name:
            value = self.bind_arguments(node, name, HOST_ERROR_CONSTRUCTOR, flow)
            _, plan, _ = value.node.fields
            return Checked(HOST_ERROR, Node(node.token, "host_failure", (plan,)), value.call)
        if flow.literal_context:
            if name in EXAMPLE_ERROR_MESSAGES:
                return self.example_error(node, flow)
            raise LanguageError(node.token, f"{flow.literal_context} values must be literals; "
                                "ordinary calls are not allowed")
        if name == "prepend":
            return self.prepend(node, flow, expected)
        signature = (self.functions.get(name) or self.imported_functions.get(name)
                     or PURE_FUNCTIONS.get(name))
        if signature is None and name in HOST_FUNCTIONS:
            if self.mock_header:
                raise LanguageError(node.token, f"mock files cannot call live host function '{name}'")
            signature = HOST_FUNCTIONS[name]
        if name in GENERIC_FUNCTIONS:
            signature = self.generic_signature(name, arguments, flow, expected)
        if signature is None:
            raise LanguageError(node.token, f"unknown function '{name}'")
        missing = (self.current_function.modifiers & GUARANTEES) - signature.modifiers
        # A pure callee provides determinism, and a pure caller already requires purity.
        if "pure" in self.current_function.modifiers or "pure" in signature.modifiers:
            missing -= {"deterministic"}
        if missing:
            guarantees = ", ".join(sorted(missing))
            raise LanguageError(node.token, f"'{name}' lacks required guarantee(s): {guarantees}")
        return self.bind_arguments(node, name, signature, flow)

    def example_error(self, node, flow):
        name = node.token.text
        arguments = node.fields
        if not EXAMPLE_ERROR_MESSAGES[name]:
            if arguments:
                raise LanguageError(node.token, f"{name} takes no arguments in an example")
            message = None
        else:
            if len(arguments) != 1 or arguments[0].mutable or arguments[0].label is not None:
                raise LanguageError(node.token, f"{name} takes one unnamed string literal in an example")
            message = self.expression(arguments[0].expression, flow, STRING).node
        bindings = () if message is None else (("message", False, None),)
        call = (name, bindings) if self.facts is not None else None
        return Checked(Type(name), Node(node.token, "failure", (name, message)), call)

    def construct_record(self, node, name, flow):
        signature = self.records[self.record_names[name]]
        if any(argument.label is None for argument in node.fields):
            raise LanguageError(node.token, f"'{name}' requires named fields")
        value = self.bind_arguments(node, name, signature, flow, transfer=True)
        _, plan, _ = value.node.fields
        fields = tuple(parameter.name for parameter in signature.parameters)
        return Checked(signature.output, Node(node.token, "record", (signature.output.name, fields, plan)),
                       value.call)

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
                value = self.bind_arguments(node, "prepend", signature, trial, transfer=True)
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
        flow.commit(trial)
        return value

    def bind_arguments(self, node, name, signature, flow, *, transfer=False):
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
        group = signature.commutative_slots

        def search(position, available, trial, plan, mutable_arguments):
            nonlocal error, error_depth
            if len(solutions) == 2:
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
            # Open slots in the group have identical contracts. Try one representative
            # for each unnamed argument while still exploring slots outside the group.
            first = next((index for index in candidates if index in group), None)
            for index in candidates:
                if index in group and index != first:
                    continue
                parameter = parameters[index]
                candidate = trial.copy()
                try:
                    value = self.check_argument(argument, parameter, candidate,
                                                transfer=transfer or name in {"at", "dropLast"})
                except LanguageError as failure:
                    if position > error_depth:
                        error, error_depth = failure, position
                    continue
                search(position + 1, tuple(slot for slot in available if slot != index),
                       candidate, plan + ((index, argument.mutable, value),),
                       mutable_arguments + ((argument, parameter, value),) if argument.mutable
                       else mutable_arguments)
                if len(solutions) == 2:
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
        flow.commit(resolved)
        call = None
        if self.facts is not None:
            call = (name if signature.name.startswith("uint32:") else signature.key,
                    tuple((parameters[index].name, mutable, value if mutable else None)
                                         for index, mutable, value in plan))
        return Checked(signature.output, Node(node.token, "invoke", (signature.key, plan, group)), call)

    def check_argument(self, argument, parameter, flow, *, transfer=False):
        if argument.mutable != parameter.mutable:
            required = "requires" if parameter.mutable else "does not accept"
            raise LanguageError(argument.token, f"parameter '{parameter.name}' {required} a mut argument")
        if not argument.mutable:
            value = self.expression(argument.expression, flow, parameter.type,
                                    transfer=transfer, consume=parameter.owning,
                                    borrow_shared=not (transfer or parameter.owning))
            if not (transfer or parameter.owning) and self.contains_exclusive(value.type):
                source = source_binding(argument.expression)
                if source is not None:
                    if source in flow.write_loans:
                        raise LanguageError(argument.token, f"'{source}' is already mutably borrowed by this call")
                    flow.loans |= {source}
            return value.node
        if argument.expression.kind != "variable":
            raise LanguageError(argument.token, "a mut argument must be a variable")
        name, = argument.expression.fields
        if name in flow.loans:
            raise LanguageError(argument.token, f"mut argument '{name}' may alias another argument")
        binding = self.writable(flow, argument.token, name)
        flow.loans |= {name}
        flow.write_loans |= {name}
        if (binding.type.without_refinement() != parameter.type.without_refinement()
                or (binding.type.nonzero and not parameter.type.nonzero)):
            raise LanguageError(argument.token, f"cannot pass mut {binding.type} to mut {parameter.type}")
        return binding.token

    def finish_mutable_arguments(self, flow, arguments):
        for argument, parameter, binding in arguments:
            name = binding.text
            if not compatible(flow.type_of(name), parameter.type):
                raise LanguageError(argument.token, f"'{name}' needs a nonzero proof for mut {parameter.type}")
        for _, parameter, binding in arguments:
            if not parameter.type.nonzero:
                flow.drop_proof(binding.text)
