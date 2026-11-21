"""Scheduler deadlines ordered by time and registration order."""

from heapq import heappop, heappush
from itertools import count


class Timers:
    def __init__(self):
        self.queue = []
        self.events = {}
        self.sequence = count()

    def register(self, deadline, callback):
        key = next(self.sequence)
        self.events[key] = callback
        heappush(self.queue, (deadline, key))
        return key

    def remove(self, key):
        if key is not None:
            self.events.pop(key, None)
        if not self.events:
            self.queue.clear()

    def next_deadline(self):
        while self.queue and self.queue[0][1] not in self.events:
            heappop(self.queue)
        return self.queue[0][0] if self.queue else None

    def expire(self, now):
        deadline = self.next_deadline()
        while deadline is not None and deadline <= now:
            _, key = heappop(self.queue)
            self.events.pop(key)()
            deadline = self.next_deadline()
