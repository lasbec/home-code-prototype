"""Thread operations exposed to the checker."""

from ..syntax import KILLED, UNIT, LanguageError, builtin, result_type


OPERATIONS = {"join", "kill"}


def signature(node, handle):
    if handle is None:
        raise LanguageError(node.token, f"{node.token.text} requires a Thread<T> handle")
    output = UNIT if node.token.text == "kill" else result_type(handle.arguments[0], KILLED)
    return builtin(node.token.text, (("thread", handle),), output)
