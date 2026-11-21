"""Operation names, modes, and result variants shared by checking and execution."""

from dataclasses import dataclass

from ..values import Result, Variant


@dataclass(frozen=True)
class Operation:
    action: str
    mode: str = ""
    closable: bool = False
    error_type: str | None = None
    cases: tuple = ()

    @property
    def carries_message(self):
        return self.action == "send" and bool(self.cases)

    def failure(self, case, message=None):
        error = Variant(self.error_type, case, message, has_payload=self.carries_message)
        return Result("Err", error)


OPERATIONS = {
    "create_channel": Operation("create"),
    "create_closable_channel": Operation("create", closable=True),
    "close": Operation("close", closable=True),
    "try_send": Operation("send", "try", False, "TrySendError", ("Full",)),
    "try_receive": Operation("receive", "try", False, "Empty", ("Empty",)),
    "send_blocking": Operation("send", "blocking", False, "Never"),
    "receive_blocking": Operation("receive", "blocking"),
    "send_timeout": Operation("send", "timed", False, "SendTimeout", ("Timeout",)),
    "receive_timeout": Operation("receive", "timed", False, "Timeout", ("Timeout",)),
    "try_send_closable": Operation("send", "try", True, "TrySendClosableError", ("Full", "Closed")),
    "try_receive_closable": Operation("receive", "try", True, "TryReceiveClosableError", ("Empty", "Closed")),
    "send_blocking_closable": Operation("send", "blocking", True, "ClosedSend", ("Closed",)),
    "receive_blocking_closable": Operation("receive", "blocking", True, "Closed", ("Closed",)),
    "send_timeout_closable": Operation("send", "timed", True, "TimedSendClosableError", ("Timeout", "Closed")),
    "receive_timeout_closable": Operation("receive", "timed", True, "TimedReceiveClosableError", ("Timeout", "Closed")),
}


def handle_name(closable):
    return "ClosableChannel" if closable else "Channel"

HANDLES = {handle_name(closable) for closable in (False, True)}
ERROR_TYPES = {operation.error_type: operation for operation in OPERATIONS.values() if operation.error_type}
