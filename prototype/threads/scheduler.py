"""Deterministic cooperative scheduling for the threads environment."""

from collections import deque
from dataclasses import dataclass, field
from typing import Generator

from ..values import Result


@dataclass(eq=False)
class Thread:
    number: int
    code: Generator
    outcome: Result | None = None
    joiners: list = field(default_factory=list)
    waiting_for: str | None = None
    inbox: object = None

    def __str__(self):
        return f"Thread({self.number})"


class Threads:
    def start(self, scheduler, code):
        return scheduler.start(code)


class Scheduler:
    def __init__(self):
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

    def park(self, reason):
        self.current.waiting_for = reason

    def wake(self, task, value):
        task.waiting_for = None
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
            if not self.ready:
                reasons = ", ".join(f"{task}: {task.waiting_for}" for task in self.active.values())
                raise RuntimeError(f"deadlock ({reasons})")
            self.advance()
        return root.outcome.value
