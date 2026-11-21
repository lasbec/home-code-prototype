"""Thread operations exposed to the checker."""

from ..syntax import GUARANTEES, KILLED, UNIT, LanguageError, Type, builtin, result_type


LOCAL_OPERATIONS = {"join_ending", "kill_ending", "share_thread"}
OPERATIONS = {"join", "kill"} | LOCAL_OPERATIONS


def handle_name(*, ending):
    return "EndingThread" if ending else "Thread"


def handle_type(output, *, ending):
    return Type(handle_name(ending=ending), (output,))


def signature(node, handle):
    name = node.token.text
    ending = name in LOCAL_OPERATIONS
    if handle is None:
        expected = "local EndingThread<T>" if ending else "Thread<T>"
        raise LanguageError(node.token, f"{name} requires a {expected} handle")
    output, = handle.arguments
    guarantees = frozenset({"ending"}) if ending else frozenset()
    if name == "share_thread":
        return builtin(name, (("thread", handle),), handle_type(output, ending=False), GUARANTEES)
    result = UNIT if name in {"kill", "kill_ending"} else result_type(output, KILLED)
    return builtin(name, (("thread", handle),), result, guarantees)
