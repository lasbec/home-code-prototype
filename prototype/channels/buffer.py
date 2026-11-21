"""FIFO channel buffers and their waiting senders and receivers."""

from collections import deque
from dataclasses import dataclass, field

from ..values import Result
from .api import OPERATIONS, handle_name


@dataclass(eq=False)
class Channel:
    capacity: int | None
    closable: bool = False
    closed: bool = False
    messages: deque = field(default_factory=deque)
    senders: dict = field(default_factory=dict)
    receivers: dict = field(default_factory=dict)

    def __str__(self):
        return handle_name(self.closable)

    def offer(self, scheduler, message):
        if self.receivers:
            receiver = next(iter(self.receivers))
            scheduler.wake(receiver, Result("Ok", message))
        elif self.capacity is None or len(self.messages) < self.capacity:
            self.messages.append(message)
        else:
            return False
        return True

    def take(self, scheduler):
        message = self.messages.popleft()
        if self.senders:
            sender, (pending, _) = next(iter(self.senders.items()))
            self.messages.append(pending)
            scheduler.wake(sender, Result("Ok", None))
        return message

    def send(self, scheduler, operation, message, timeout=None):
        scheduler.expire_timers()
        if self.closed:
            return operation.failure("Closed", message)
        if self.offer(scheduler, message):
            return Result("Ok", None)
        if operation.mode == "try":
            return operation.failure("Full", message)
        failure = operation.failure("Timeout", message) if timeout is not None else None
        if timeout == 0:
            return failure
        self.senders[scheduler.current] = (message, operation)
        self.park(scheduler, "send", self.senders, timeout, failure)
        return (yield)

    def receive(self, scheduler, operation, timeout=None):
        scheduler.expire_timers()
        if self.messages:
            result = Result("Ok", self.take(scheduler))
        elif self.closed:
            return operation.failure("Closed")
        elif operation.mode == "try":
            return operation.failure("Empty")
        else:
            failure = operation.failure("Timeout") if timeout is not None else None
            if timeout == 0:
                return failure
            self.receivers[scheduler.current] = operation
            self.park(scheduler, "receive", self.receivers, timeout, failure)
            result = yield
        return result if operation.error_type else result.value

    def park(self, scheduler, action, waiters, timeout, failure):
        task = scheduler.current
        deadline = None if timeout is None else scheduler.clock() + timeout / 1000
        scheduler.park(f"{action} on {self}", remove=lambda: waiters.pop(task, None),
                       deadline=deadline, timeout_value=failure)

    def close(self, scheduler):
        scheduler.expire_timers()
        if self.closed:
            return
        self.closed = True
        while self.senders:
            task, (message, operation) = next(iter(self.senders.items()))
            scheduler.wake(task, operation.failure("Closed", message))
        # A receive only parks when the buffer is empty.
        while self.receivers:
            task, operation = next(iter(self.receivers.items()))
            scheduler.wake(task, operation.failure("Closed"))


class Channels:
    def create(self, capacity, *, closable=False):
        return Channel(capacity.value, closable=closable)


def invoke(name, arguments, scheduler):
    operation = OPERATIONS[name]
    if operation.action == "create":
        channels, capacity = arguments
        return channels.create(capacity, closable=operation.closable)
    channel, *payload = arguments
    if operation.action == "close":
        return channel.close(scheduler)
    if operation.action == "send":
        return (yield from channel.send(scheduler, operation, *payload))
    return (yield from channel.receive(scheduler, operation, *payload))
