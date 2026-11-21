"""Source-level coverage of typed channel operations."""

import unittest

from prototype.channels.buffer import Channels
from prototype.checker import Checker
from prototype.runtime import Interpreter, display
from prototype.syntax import LanguageError, Parser
from prototype.threads.scheduler import Threads


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
