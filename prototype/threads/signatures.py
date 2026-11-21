"""Thread operations exposed to the checker."""

from ..syntax import KILLED, LanguageError, builtin, result_type


OPERATIONS = {"join"}


def signature(node, handle):
    if handle is None:
        raise LanguageError(node.token, f"{node.token.text} requires a Thread<T> handle")
    return builtin(node.token.text, (("thread", handle),), result_type(handle.arguments[0], KILLED))
