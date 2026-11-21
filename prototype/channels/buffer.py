"""Bounded and unbounded FIFO channel buffers."""

from collections import deque
from dataclasses import dataclass, field

from ..values import Result, Variant


@dataclass(eq=False)
class Channel:
    capacity: int | None
    messages: deque = field(default_factory=deque)

    def __str__(self):
        return "Channel"

    def try_send(self, message):
        if self.capacity is not None and len(self.messages) == self.capacity:
            return Result("Err", Variant("TrySendError", "Full", message, has_payload=True))
        self.messages.append(message)
        return Result("Ok", None)

    def try_receive(self):
        if not self.messages:
            return Result("Err", Variant("Empty", "Empty"))
        return Result("Ok", self.messages.popleft())


class Channels:
    def create(self, capacity):
        return Channel(capacity.value)


OPERATIONS = {
    "create_channel": Channels.create,
    "try_send": Channel.try_send,
    "try_receive": Channel.try_receive,
}
