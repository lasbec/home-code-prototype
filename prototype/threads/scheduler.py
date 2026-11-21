"""Deterministic cooperative scheduling for the threads environment."""

from collections import deque
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Generator

from ..values import Result
from .timers import Timers


@dataclass(eq=False)
class Thread:
    number: int
    code: Generator
    outcome: Result | None = None
    joiners: list = field(default_factory=list)
    waiting_for: str | None = None
    inbox: object = None
    remove_wait: object = None
    timer: int | None = None

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
        task = Thread(self.next_number, code)
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

    def join(self, target):
        if target.outcome is not None:
            return target.outcome
        target.joiners.append(self.current)
        self.park(f"join on {target}")
        return (yield)

    def finish(self, task, value):
        task.outcome = Result("Ok", value)
        del self.active[task.number]
        for joiner in task.joiners:
            self.wake(joiner, task.outcome)
        task.joiners.clear()

    def advance(self):
        task = self.ready.popleft()
        self.current = task
        value, task.inbox = task.inbox, None
        try:
            task.code.send(value)
        except StopIteration as completed:
            self.finish(task, completed.value)
        else:
            if task.waiting_for is None:
                self.ready.append(task)
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
