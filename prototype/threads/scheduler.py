"""Deterministic cooperative scheduling for the threads environment."""

from collections import deque
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Generator

from ..values import Failure, Result
from .timers import Timers


class Cancelled(Exception):
    """Unwind a green thread at a scheduling checkpoint."""


@dataclass(eq=False)
class Thread:
    number: int
    code: Generator
    outcome: Result | None = None
    joiners: dict = field(default_factory=dict)
    waiting_for: str | None = None
    inbox: object = None
    remove_wait: object = None
    timer: int | None = None
    parent: "Thread | None" = None
    children: dict = field(default_factory=dict)
    cancellation_requested: bool = False
    unwinding: bool = False
    cleanup_depth: int = 0

    @property
    def can_interrupt(self):
        return self.cancellation_requested and not self.unwinding and self.cleanup_depth == 0

    def __str__(self):
        return f"Thread({self.number})"


class Threads:
    def start(self, scheduler, code):
        return scheduler.start(code)


class Scheduler:
    def __init__(self, *, clock=None, idle=None):
        self.clock = clock if clock is not None else monotonic
        self.idle = idle if idle is not None else sleep
        self.timers = Timers()
        self.ready = deque()
        self.active = {}
        self.current = None
        self.next_number = 1

    def start(self, code):
        task = Thread(self.next_number, code, parent=self.current)
        if task.parent is not None:
            task.parent.children[task] = None
        self.next_number += 1
        self.active[task.number] = task
        self.ready.append(task)
        return task

    def expire_timers(self):
        if self.timers.events:
            self.timers.expire(self.clock())

    def park(self, reason, *, remove=None, deadline=None, timeout_value=None):
        task = self.current
        task.waiting_for = reason
        task.remove_wait = remove
        if deadline is not None:
            task.timer = self.timers.register(deadline, lambda: self.wake(task, timeout_value))

    def clear_wait(self, task):
        self.timers.remove(task.timer)
        task.timer = None
        if task.remove_wait is not None:
            task.remove_wait()
            task.remove_wait = None
        task.waiting_for = None

    def wake(self, task, value):
        self.clear_wait(task)
        task.inbox = value
        self.ready.append(task)

    def join(self, target, *, action="join"):
        if target.outcome is not None:
            return target.outcome
        task = self.current
        target.joiners[task] = None
        self.park(f"{action} on {target}", remove=lambda: target.joiners.pop(task, None))
        return (yield)

    def request_cancellation(self, target):
        pending = deque([target])
        while pending:
            task = pending.popleft()
            pending.extend(task.children)
            if task.outcome is None:
                task.cancellation_requested = True
                if task.waiting_for is not None and task.can_interrupt:
                    self.wake(task, None)

    def kill(self, target):
        if target.outcome is not None:
            return
        self.request_cancellation(target)
        if target is self.current:
            if target.can_interrupt:
                target.unwinding = True
                raise Cancelled()
            return
        yield from self.join(target, action="kill")

    def finish(self, task, outcome):
        task.outcome = outcome
        del self.active[task.number]
        for joiner in tuple(task.joiners):
            self.wake(joiner, outcome)

        # A completed intermediate task stays in the tree while it has descendants.
        while task.outcome is not None and not task.children and task.parent is not None:
            parent, task.parent = task.parent, None
            del parent.children[task]
            task = parent

    def advance(self):
        task = self.ready.popleft()
        self.current = task
        value, task.inbox = task.inbox, None
        try:
            if task.can_interrupt:
                task.unwinding = True
                task.code.throw(Cancelled())
            else:
                task.code.send(value)
        except Cancelled:
            self.finish(task, Result("Err", Failure("Killed")))
        except StopIteration as completed:
            outcome = (Result("Err", Failure("Killed")) if task.cancellation_requested
                       else Result("Ok", completed.value))
            self.finish(task, outcome)
        else:
            if task.waiting_for is None:
                self.ready.append(task)
            elif task.can_interrupt:
                self.wake(task, None)
        finally:
            self.current = None

    def run(self, code):
        root = self.start(code)
        while self.active:
            self.expire_timers()
            if not self.ready:
                deadline = self.timers.next_deadline()
                if deadline is not None:
                    self.idle(max(0, deadline - self.clock()))
                    continue
                reasons = ", ".join(f"{task}: {task.waiting_for}" for task in self.active.values())
                raise RuntimeError(f"deadlock ({reasons})")
            self.advance()
        return root.outcome.value
