"""Channel types and operations exposed to the checker."""

from dataclasses import replace

from ..syntax import CHANNELS, UINT, UNIT, LanguageError, Type, builtin, result_type
from .api import ERROR_TYPES, HANDLES, OPERATIONS, handle_name


CAPACITY = Type("Capacity")
MILLISECONDS = Type(UINT.name, units=(("ms", 1),))
ENDING = frozenset({"ending"})
ENUMS = {
    CAPACITY: {"Bounded": UINT.refined(), "Unbounded": None},
    **{Type(name): dict.fromkeys(op.cases) for name, op in ERROR_TYPES.items()
       if not op.carries_message},
}


def variants(value_type):
    operation = ERROR_TYPES.get(value_type.name)
    if operation is not None and value_type.arguments:
        return dict.fromkeys(operation.cases, value_type.arguments[0])
    return ENUMS.get(value_type)


def signatures(node, handles, expected):
    name = node.token.text
    operation = OPERATIONS[name]
    handle_type = handle_name(operation.closable)
    if operation.action == "create":
        if expected is None or expected.name != handle_type:
            raise LanguageError(node.token, f"{name} needs an explicit {handle_type}<T> result type")
        return (builtin(name, (("channels", CHANNELS), ("capacity", CAPACITY)), expected, ENDING),)
    candidates = [handle for handle in handles if handle.name == handle_type]
    if not candidates:
        raise LanguageError(node.token, f"{name} requires a {handle_type}<T> handle")
    return tuple(signature(name, operation, handle) for handle in candidates)


def signature(name, operation, handle):
    message, = handle.arguments
    parameters = (("channel", handle),)
    if operation.action == "close":
        return builtin(name, parameters, UNIT, ENDING)
    sending = operation.action == "send"
    if sending:
        parameters += (("message", message),)
    if operation.mode == "timed":
        parameters += (("timeout", MILLISECONDS),)
    output = UNIT if sending else message
    if operation.error_type:
        error_arguments = (message,) if operation.carries_message else ()
        output = result_type(output, Type(operation.error_type, error_arguments))
    guarantees = frozenset() if operation.mode == "blocking" else ENDING
    function = builtin(name, parameters, output, guarantees)
    if sending and not message.is_shared:
        channel, payload, *rest = function.parameters
        return replace(function, parameters=(channel, replace(payload, owning=True), *rest))
    return function
