"""Source-level coverage of typed channel operations."""

import unittest

from prototype.channels.api import OPERATIONS
from prototype.channels.buffer import Channel, Channels
from prototype.checker import Checker
from prototype.runtime import Interpreter, display
from prototype.syntax import LanguageError, Parser
from prototype.threads.scheduler import Scheduler, Threads


def compile_program(source):
    return Checker(Parser(source).program()).program()


class ChannelsTest(unittest.TestCase):
    def run_main(self, source, *capabilities):
        return display(Interpreter(compile_program(source)).call("main", (Channels(), *capabilities)))

    def test_bounded_fifo_full_and_empty(self):
        self.assertEqual(self.run_main("""
            fn main(channels: channels) -> list<int32> {
                let capacity: uint32!0 = 2;
                let channel: Channel<int32> = create_channel(channels, Capacity::Bounded(capacity));
                try_send(channel, 11);
                try_send(channel, 22);
                match try_send(channel, 33) {
                    Ok(value) { return []; }
                    Err(error) {
                        match error {
                            Full(payload) { return drain(channel, payload); }
                        }
                    }
                }
            }
            fn drain(channel: Channel<int32>, payload: int32) -> list<int32> {
                match try_receive(channel) {
                    Err(empty) { return []; }
                    Ok(first) {
                        match try_receive(channel) {
                            Err(empty) { return []; }
                            Ok(second) {
                                match try_receive(channel) {
                                    Err(empty) { return [first, second, payload]; }
                                    Ok(extra) { return []; }
                                }
                            }
                        }
                    }
                }
            }
        """), "[11, 22, 33]")

    def test_unbounded_channel_can_carry_shared_handles_and_exclusive_capabilities(self):
        self.assertEqual(self.run_main("""
            record Package { output: console, inner: Channel<int32> }
            fn main(channels: channels, output: own console) -> result<Package, Empty> {
                let mailbox: Channel<Package> = create_channel(channels, Capacity::Unbounded);
                let inner: Channel<int32> = create_channel(channels, Capacity::Unbounded);
                let package = Package(output: output, inner: inner);
                try_send(mailbox, package);
                try_send(inner, 9);
                return try_receive(mailbox);
            }
        """, "opaque console"), "Ok(Package(output: opaque console, inner: Channel))")

    def test_handle_passed_as_an_unnamed_message_before_destination(self):
        self.assertEqual(self.run_main("""
            fn main(channels: channels) -> result<int32, Empty> {
                let inner: Channel<int32> = create_channel(channels, Capacity::Unbounded);
                let outer: Channel<Channel<int32>> = create_channel(channels, Capacity::Unbounded);
                try_send(inner, outer);
                try_send(inner, 9);
                match try_receive(outer) {
                    Ok(received) { return try_receive(received); }
                    Err(empty) { return Err(empty); }
                }
            }
        """), "Ok(9)")

    def test_capacity_proof_and_message_move_are_required(self):
        source = """
            fn main(channels: channels) -> unit {
                let value = 7;
                let channel: Channel<int32> = create_channel(channels, Capacity::Bounded(CAPACITY));
                try_send(channel, value);
                value;
            }
        """
        with self.assertRaisesRegex(LanguageError, "value.*was moved"):
            compile_program(source.replace("CAPACITY", "1"))
        with self.assertRaisesRegex(LanguageError, "expected uint32!0"):
            compile_program(source.replace("CAPACITY", "0"))

    def test_blocked_senders_keep_fifo_order_ahead_of_later_calls(self):
        self.assertEqual(self.run_main("""
            fn produce(q: Channel<int32>, n: int32) -> unit { send_blocking(q, n); }
            fn main(channels: channels, workers: threads) -> list<int32> {
                let q: Channel<int32> = create_channel(channels, Capacity::Bounded(1));
                try_send(q, 1);
                let first = thread produce(q, 2);
                let second = thread produce(q, 3);
                let one = receive_blocking(q);
                let attempt = try_send(q, 4);
                let two = receive_blocking(q);
                let three = receive_blocking(q);
                join(first);
                join(second);
                match attempt {
                    Ok(value) { return []; }
                    Err(error) { return [one, two, three]; }
                }
            }
        """, Threads()), "[1, 2, 3]")

    def test_blocked_receivers_get_shared_messages_in_order(self):
        self.assertEqual(self.run_main("""
            fn receive(q: Channel<Channel<int32>>) -> int32 {
                return receive_blocking(receive_blocking(q));
            }
            fn main(channels: channels, workers: threads) -> list<int32> {
                let q: Channel<Channel<int32>> = create_channel(channels, Capacity::Bounded(1));
                let a: Channel<int32> = create_channel(channels, Capacity::Unbounded);
                let b: Channel<int32> = create_channel(channels, Capacity::Unbounded);
                let first = thread receive(q);
                let second = thread receive(q);
                try_send(q, a);
                send_blocking(q, b);
                try_send(a, 7);
                try_send(b, 8);
                match join(first) {
                    Err(killed) { return []; }
                    Ok(one) {
                        match join(second) {
                            Err(killed) { return []; }
                            Ok(two) { return [one, two]; }
                        }
                    }
                }
            }
        """, Threads()), "[7, 8]")

    def test_only_blocked_operations_report_deadlock(self):
        with self.assertRaisesRegex(RuntimeError, "deadlock.*send on Channel.*receive on Channel"):
            self.run_main("""
                fn receive(q: Channel<int32>) -> unit { receive_blocking(q); }
                fn main(channels: channels, workers: threads) -> unit {
                    let full: Channel<int32> = create_channel(channels, Capacity::Bounded(1));
                    let empty: Channel<int32> = create_channel(channels, Capacity::Bounded(1));
                    try_send(full, 1);
                    thread receive(empty);
                    send_blocking(full, 2);
                }
            """, Threads())

    def test_timed_wait_can_wake_the_only_task(self):
        clock = ControlledClock()
        program = compile_program("""
            fn main(channels: channels) -> result<int32, Timeout> {
                let q: Channel<int32> = create_channel(channels, Capacity::Bounded(1));
                return receive_timeout(q, 25);
            }
        """)
        interpreter = Interpreter(program, clock=clock.now, idle=clock.advance)
        self.assertEqual(display(interpreter.call("main", (Channels(),))), "Err(Timeout::Timeout)")
        self.assertEqual(clock.now(), 0.025)

    def test_zero_timeout_attempt_and_units(self):
        self.assertEqual(self.run_main("""
            fn main(channels: channels) -> result<int32, Timeout> {
                let q: Channel<int32> = create_channel(channels, Capacity::Bounded(1));
                send_timeout(q, message: 3, timeout: 0);
                return receive_timeout(q, 0);
            }
        """), "Ok(3)")
        with self.assertRaisesRegex(LanguageError, "expected uint32<ms>"):
            compile_program("""
                fn receive(q: Channel<int32>, wait: uint32) -> result<int32, Timeout> {
                    return receive_timeout(q, wait);
                }
            """)

    def test_transfer_before_deadline_and_expiry_at_deadline(self):
        for elapsed, outcome, next_value in (
            (0.009, "Ok(unit)", "Ok(2)"),
            (0.010, "Err(SendTimeout::Timeout(2))", "Err(Empty::Empty)"),
            (0.011, "Err(SendTimeout::Timeout(2))", "Err(Empty::Empty)"),
        ):
            with self.subTest(elapsed=elapsed):
                clock = ControlledClock()
                scheduler = Scheduler(clock=clock.now, idle=clock.advance)
                q = Channel(1)

                def exercise():
                    yield from q.send(scheduler, OPERATIONS["try_send"], 1)
                    sender = scheduler.start(q.send(scheduler, OPERATIONS["send_timeout"], 2, 10))
                    yield
                    clock.advance(elapsed)
                    self.assertEqual((yield from q.receive(scheduler, OPERATIONS["receive_blocking"])), 1)
                    finished = yield from scheduler.join(sender)
                    return display(finished.value), display((yield from q.receive(scheduler, OPERATIONS["try_receive"])))

                self.assertEqual(scheduler.run(exercise()), (outcome, next_value))
                self.assertEqual(q.senders, {})
                self.assertIsNone(scheduler.timers.next_deadline())

    def test_expired_receiver_cannot_take_later_message(self):
        clock = ControlledClock()
        scheduler = Scheduler(clock=clock.now, idle=clock.advance)
        q = Channel(1)

        def exercise():
            receiver = scheduler.start(q.receive(scheduler, OPERATIONS["receive_timeout"], 10))
            yield
            clock.advance(0.010)
            yield from q.send(scheduler, OPERATIONS["send_blocking"], 5)
            result = yield from scheduler.join(receiver)
            return display(result.value), (yield from q.receive(scheduler, OPERATIONS["receive_blocking"]))

        self.assertEqual(scheduler.run(exercise()), ("Err(Timeout::Timeout)", 5))
        self.assertEqual(q.receivers, {})

    def test_closable_channel_drains_fifo_and_code_distinguishes_empty(self):
        self.assertEqual(self.run_main("""
            fn main(channels: channels) -> list<int32> {
                let q: ClosableChannel<int32> = create_closable_channel(channels, Capacity::Bounded(2));
                match try_receive_closable(q) {
                    Err(error) {
                        match error {
                            Empty { return drain(q); }
                            Closed { return []; }
                        }
                    }
                    Ok(value) { return []; }
                }
            }
            fn drain(q: ClosableChannel<int32>) -> list<int32> {
                try_send_closable(q, 1);
                send_blocking_closable(q, 2);
                close(q);
                close(q);
                match receive_blocking_closable(q) {
                    Ok(first) {
                        match receive_timeout_closable(q, 0) {
                            Ok(second) {
                                match try_receive_closable(q) {
                                    Err(error) {
                                        match error {
                                            Empty { return []; }
                                            Closed { return [first, second]; }
                                        }
                                    }
                                    Ok(extra) { return []; }
                                }
                            }
                            Err(error) { return []; }
                        }
                    }
                    Err(error) { return []; }
                }
            }
        """), "[1, 2]")

    def test_close_wakes_pending_sender_and_receiver(self):
        for kind, expected in (
            ("send_blocking_closable", "Err(ClosedSend::Closed(2))"),
            ("receive_blocking_closable", "Err(Closed::Closed)"),
        ):
            with self.subTest(kind=kind):
                scheduler = Scheduler()
                q = Channel(1, closable=True)
                op = OPERATIONS[kind]

                def exercise():
                    if op.action == "send":
                        yield from q.send(scheduler, OPERATIONS["try_send_closable"], 1)
                        waiter = scheduler.start(q.send(scheduler, op, 2))
                    else:
                        waiter = scheduler.start(q.receive(scheduler, op))
                    yield
                    q.close(scheduler)
                    return (yield from scheduler.join(waiter)).value

                self.assertEqual(display(scheduler.run(exercise())), expected)
                self.assertFalse(q.senders or q.receivers)

    def test_closed_send_returns_payload_and_modes_are_distinct(self):
        self.assertEqual(self.run_main("""
            fn main(channels: channels) -> result<unit, TimedSendClosableError<int32>> {
                let q: ClosableChannel<int32> = create_closable_channel(channels, Capacity::Unbounded);
                close(q);
                return send_timeout_closable(q, message: 5, timeout: 0);
            }
        """), "Err(TimedSendClosableError::Closed(5))")
        with self.assertRaisesRegex(LanguageError, "requires a ClosableChannel"):
            compile_program("fn invalid(q: Channel<int32>) -> unit { close(q); }")

    def test_closure_and_timeout_resolve_once(self):
        for elapsed, expected in (
            (0.009, "Err(TimedSendClosableError::Closed(2))"),
            (0.010, "Err(TimedSendClosableError::Timeout(2))"),
        ):
            with self.subTest(elapsed=elapsed):
                clock = ControlledClock()
                scheduler = Scheduler(clock=clock.now, idle=clock.advance)
                q = Channel(1, closable=True)

                def exercise():
                    yield from q.send(scheduler, OPERATIONS["try_send_closable"], 1)
                    pending = scheduler.start(q.send(scheduler, OPERATIONS["send_timeout_closable"], 2, 10))
                    yield
                    clock.advance(elapsed)
                    q.close(scheduler)
                    self.assertEqual(list(q.messages), [1])
                    return (yield from scheduler.join(pending)).value

                self.assertEqual(display(scheduler.run(exercise())), expected)
                self.assertFalse(q.senders or scheduler.timers.events)


class ControlledClock:
    def __init__(self):
        self.time = 0.0

    def now(self):
        return self.time

    def advance(self, duration):
        self.time += duration
