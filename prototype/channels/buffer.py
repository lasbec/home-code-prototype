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
            scheduler.wake(receiver, Result("Ok", message))
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
            scheduler.wake(sender, Result("Ok", None))
        return message

    def send(self, scheduler, message, timeout=None, *, blocking):
        scheduler.expire_timers()
        if self.offer(scheduler, message):
            return Result("Ok", None)
        if timeout is None:
            failure = Result("Err", Variant("TrySendError", "Full", message, has_payload=True))
        else:
            failure = Result("Err", Variant("SendTimeout", "Timeout", message, has_payload=True))
        if not (blocking or timeout):
            return failure
        task = scheduler.current
        self.senders[task] = message
        self.park(scheduler, "send", self.senders, timeout, failure)
        return (yield)

    def receive(self, scheduler, timeout=None, *, blocking):
        scheduler.expire_timers()
        if self.messages:
            result = Result("Ok", self.take(scheduler))
        else:
            code = "Empty" if timeout is None else "Timeout"
            failure = Result("Err", Variant(code, code))
            if not (blocking or timeout):
                return failure
            self.receivers[scheduler.current] = None
            self.park(scheduler, "receive", self.receivers, timeout, failure)
            result = yield
        return result.value if blocking else result

    def park(self, scheduler, operation, waiters, timeout, failure):
        task = scheduler.current
        deadline = None if timeout is None else scheduler.clock() + timeout / 1000
        scheduler.park(f"{operation} on {self}", remove=lambda: waiters.pop(task, None),
                       deadline=deadline, timeout_value=failure)


class Channels:
    def create(self, capacity):
        return Channel(capacity.value)


def invoke(name, arguments, scheduler):
    if name == "create_channel":
        channels, capacity = arguments
        return channels.create(capacity)
    channel, *payload = arguments
    blocking = name.endswith("_blocking")
    if "send" in name:
        return (yield from channel.send(scheduler, *payload, blocking=blocking))
    return (yield from channel.receive(scheduler, *payload, blocking=blocking))
