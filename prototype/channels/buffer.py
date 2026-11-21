"""FIFO channel buffers and their waiting senders and receivers."""

from collections import deque
from dataclasses import dataclass, field

from ..values import Result, Variant


@dataclass(eq=False)
class Channel:
    capacity: int | None
    messages: deque = field(default_factory=deque)
    senders: dict = field(default_factory=dict)
    receivers: dict = field(default_factory=dict)

    def __str__(self):
        return "Channel"

    def offer(self, scheduler, message):
        if self.receivers:
            receiver = next(iter(self.receivers))
            del self.receivers[receiver]
            scheduler.wake(receiver, message)
        elif self.capacity is None or len(self.messages) < self.capacity:
            self.messages.append(message)
        else:
            return False
        return True

    def take(self, scheduler):
        message = self.messages.popleft()
        if self.senders:
            sender = next(iter(self.senders))
            self.messages.append(self.senders.pop(sender))
            scheduler.wake(sender, None)
        return message

    def send(self, scheduler, message, *, blocking):
        if not self.offer(scheduler, message):
            if not blocking:
                return Result("Err", Variant("TrySendError", "Full", message, has_payload=True))
            task = scheduler.current
            self.senders[task] = message
            scheduler.park(f"send on {self}")
            yield
        return Result("Ok", None)

    def receive(self, scheduler, *, blocking):
        if self.messages:
            message = self.take(scheduler)
        elif blocking:
            task = scheduler.current
            self.receivers[task] = None
            scheduler.park(f"receive on {self}")
            message = yield
        else:
            return Result("Err", Variant("Empty", "Empty"))
        return message if blocking else Result("Ok", message)


class Channels:
    def create(self, capacity):
        return Channel(capacity.value)


def invoke(name, arguments, scheduler):
    if name == "create_channel":
        channels, capacity = arguments
        return channels.create(capacity)
    channel, *payload = arguments
    blocking = name.endswith("_blocking")
    if name in {"try_send", "send_blocking"}:
        return (yield from channel.send(scheduler, *payload, blocking=blocking))
    return (yield from channel.receive(scheduler, blocking=blocking))
