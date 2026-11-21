"""Source-level coverage of typed channel operations."""

import unittest

from prototype.channels.buffer import Channels
from prototype.checker import Checker
from prototype.runtime import Interpreter, display
from prototype.syntax import LanguageError, Parser


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
