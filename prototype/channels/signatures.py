"""Channel types and operations exposed to the checker."""

from dataclasses import replace

from ..syntax import CHANNELS, UINT, UNIT, LanguageError, Type, builtin, result_type


CAPACITY = Type("Capacity")
EMPTY = Type("Empty")
NEVER = Type("Never")
TIMEOUT = Type("Timeout")
MILLISECONDS = Type(UINT.name, units=(("ms", 1),))
HANDLES = {"Channel"}
OPERATIONS = {
    "create_channel", "try_send", "try_receive", "send_blocking", "receive_blocking",
    "send_timeout", "receive_timeout",
}
ENDING = frozenset({"ending"})
ENUMS = {
    CAPACITY: {"Bounded": UINT.refined(), "Unbounded": None},
    EMPTY: {"Empty": None},
    NEVER: {},
    TIMEOUT: {"Timeout": None},
}


def variants(value_type):
    if value_type.name in {"TrySendError", "SendTimeout"}:
        case = "Full" if value_type.name == "TrySendError" else "Timeout"
        return {case: value_type.arguments[0]}
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
    timed = name.endswith("_timeout")
    guarantees = frozenset() if blocking else ENDING
    timeout = (("timeout", MILLISECONDS),) if timed else ()
    if "receive" in name:
        output = message if blocking else result_type(message, TIMEOUT if timed else EMPTY)
        return builtin(name, (("channel", handle), *timeout), output, guarantees)
    error = NEVER if blocking else Type("SendTimeout" if timed else "TrySendError", (message,))
    signature = builtin(name, (("channel", handle), ("message", message), *timeout),
                        result_type(UNIT, error), guarantees)
    channel, payload, *rest = signature.parameters
    return replace(signature, parameters=(channel, replace(payload, owning=not message.is_shared), *rest))
