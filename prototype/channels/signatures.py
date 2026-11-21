"""Channel types and operations exposed to the checker."""

from dataclasses import replace

from ..syntax import CHANNELS, UINT, UNIT, LanguageError, Type, builtin, result_type


CAPACITY = Type("Capacity")
EMPTY = Type("Empty")
NEVER = Type("Never")
HANDLES = {"Channel"}
OPERATIONS = {"create_channel", "try_send", "try_receive", "send_blocking", "receive_blocking"}
ENDING = frozenset({"ending"})
ENUMS = {
    CAPACITY: {"Bounded": UINT.refined(), "Unbounded": None},
    EMPTY: {"Empty": None},
    NEVER: {},
}


def variants(value_type):
    if value_type.name == "TrySendError":
        return {"Full": value_type.arguments[0]}
    return ENUMS.get(value_type)


def signatures(node, handles, expected):
    name = node.token.text
    if name == "create_channel":
        if expected is None or expected.name != "Channel":
            raise LanguageError(node.token, "create_channel needs an explicit Channel<T> result type")
        return (builtin(name, (("channels", CHANNELS), ("capacity", CAPACITY)), expected, ENDING),)
    if not handles:
        raise LanguageError(node.token, f"{name} requires a Channel<T> handle")
    return tuple(operation(name, handle) for handle in handles)


def operation(name, handle):
    message, = handle.arguments
    blocking = name.endswith("_blocking")
    guarantees = frozenset() if blocking else ENDING
    if name in {"try_receive", "receive_blocking"}:
        output = message if blocking else result_type(message, EMPTY)
        return builtin(name, (("channel", handle),), output, guarantees)
    error = NEVER if blocking else Type("TrySendError", (message,))
    signature = builtin(name, (("channel", handle), ("message", message)),
                        result_type(UNIT, error), guarantees)
    channel, payload = signature.parameters
    return replace(signature, parameters=(channel, replace(payload, owning=not message.is_shared)))
