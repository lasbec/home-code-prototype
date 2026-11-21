"""Green-thread checking and scheduling."""

from io import StringIO
import unittest

from prototype.channels.api import OPERATIONS
from prototype.channels.buffer import Channel, Channels
from prototype.checker import Checker
from prototype.runtime import Console, Interpreter, display
from prototype.syntax import LanguageError, Parser
from prototype.threads.scheduler import Scheduler, Threads


def compile_program(source):
    return Checker(Parser(source).program()).program()


class GreenThreads(unittest.TestCase):
    def test_source_order_joiners_and_parent_return(self):
        program = compile_program("""
            fn logged(output: console, label: string) -> string {
                print(output, label);
                return label;
            }
            fn task(left: string, right: string) -> string {
                return concat(left: left, right: right);
            }
            fn report(output: shared console, target: Thread<string>) -> unit {
                match join(target) {
                    Ok(text) { print(output, text); }
                    Err(error) { return unit; }
                }
            }
            fn main(workers: shared threads, output: shared console) -> result<string, Killed> {
                let target = thread task(right: logged(output, "B"), left: logged(output, "A"));
                thread report(output, target);
                thread report(output, target);
                return join(target);
            }
        """)
        for _ in range(2):
            output = StringIO()
            outcome = Interpreter(program).call("main", (Threads(), Console(StringIO(), output)))
            self.assertEqual(display(outcome), "Ok(AB)")
            self.assertEqual(output.getvalue(), "B\nA\nAB\nAB\n")

    def test_loop_back_edge_allows_ready_threads_to_run(self):
        program = compile_program("""
            fn busy() -> unit { loop {} }
            fn run() -> unit { loop {} }
        """)
        interpreter = Interpreter(program)
        scheduler = Scheduler()
        steps = []

        def observe():
            steps.append("ran")
            yield

        scheduler.start(interpreter.invoke("busy", (), scheduler))
        scheduler.start(observe())
        scheduler.advance()
        scheduler.advance()
        self.assertEqual(steps, ["ran"])

    def test_self_join_reports_blocked_task(self):
        scheduler = Scheduler()

        def self_join():
            yield from scheduler.join(scheduler.current)

        with self.assertRaisesRegex(RuntimeError, r"deadlock \(Thread\(1\): join on Thread\(1\)\)"):
            scheduler.run(self_join())

    def test_thread_checks(self):
        cases = (
            ("fn main() -> unit { thread task(); } fn task() -> unit {}", "exactly one visible"),
            ("""fn twice(a: threads, b: threads) -> unit { thread task(); }
                fn task() -> unit {}""", "exactly one visible"),
            ("""pure fn helper(workers: threads) -> unit { thread task(); }
                fn task() -> unit {}""", "cannot start a thread"),
            ("""ending fn helper(t: Thread<unit>) -> unit { join(t); }""",
             "lacks required guarantee"),
            ("""fn main(w: threads, c: own console) -> unit { thread task(c); }
                fn task(c: console) -> unit {}""", "requires own"),
            ("""fn main(w: threads, c: own console) -> unit { thread task(c); }
                fn task(c: own console) -> console { return c; }""", "shareable value"),
            ("""fn main(w: threads) -> int32 { let n = 5; thread task(n); return n; }
                fn task(n: int32) -> unit {}""", "n.*was moved"),
        )
        for source, diagnostic in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(LanguageError, diagnostic):
                    compile_program(source)
        compile_program("""
            ending fn start(w: threads) -> Thread<unit> { return thread task(); }
            fn task() -> unit {}
        """)

    def test_defer_captures_values_and_runs_lifo_at_each_block_exit(self):
        program = compile_program("""
            fn log(output: console, number: int32) -> unit {
                print(output, formatInt32(number));
            }
            fn update(number: mut int32) -> unit { number = 4; }
            fn main(output: own console) -> int32 {
                let mut number = 1;
                defer log(output, number);
                number = 2;
                if true {
                    defer update(mut number);
                    defer log(output, number);
                    defer log(output, 3);
                }
                return number;
            }
        """)
        output = StringIO()
        result = Interpreter(program).call("main", (Console(StringIO(), output),))
        self.assertEqual(result, 4)
        self.assertEqual(output.getvalue(), "3\n2\n1\n")

    def test_kill_interrupts_a_loop_and_waits_for_cleanup(self):
        program = compile_program("""
            fn busy(output: shared console) -> unit {
                defer print(output, "cleanup");
                loop {}
            }
            fn main(workers: threads, output: shared console) -> result<unit, Killed> {
                let child = thread busy(output);
                unit;
                kill(child);
                print(output, "joined cleanup");
                kill(child);
                return join(child);
            }
        """)
        output = StringIO()
        value = Interpreter(program).call("main", (Threads(), Console(StringIO(), output)))
        self.assertEqual(display(value), "Err(Killed)")
        self.assertEqual(output.getvalue(), "cleanup\njoined cleanup\n")

    def test_self_kill_runs_cleanup_once_and_all_joiners_agree(self):
        program = compile_program("""
            fn task(own_handle: Channel<Thread<unit>>, output: shared console) -> unit {
                defer print(output, "cleanup");
                kill(receive_blocking(own_handle));
                print(output, "unreachable at runtime");
            }
            fn report(target: Thread<unit>, output: shared console) -> unit {
                match join(target) {
                    Ok(value) { print(output, "returned"); }
                    Err(error) { print(output, "killed"); }
                }
            }
            fn main(workers: threads, channels: channels, output: shared console) -> result<unit, Killed> {
                let q: Channel<Thread<unit>> = create_channel(channels, Capacity::Bounded(1));
                let child = thread task(q, output);
                thread report(child, output);
                thread report(child, output);
                send_blocking(q, child);
                return join(child);
            }
        """)
        output = StringIO()
        value = Interpreter(program).call("main", (Threads(), Channels(), Console(StringIO(), output)))
        self.assertEqual(display(value), "Err(Killed)")
        self.assertEqual(output.getvalue(), "cleanup\nkilled\nkilled\n")

    def test_kill_removes_parked_join_and_uncommitted_send(self):
        scheduler = Scheduler()
        queue = Channel(1)
        children = []

        def parent():
            child = scheduler.start(queue.send(scheduler, OPERATIONS["send_blocking"], 2))
            children.append(child)
            yield from scheduler.join(child)

        def exercise():
            yield from queue.send(scheduler, OPERATIONS["try_send"], 1)
            task = scheduler.start(parent())
            yield
            yield
            self.assertTrue(queue.senders)
            yield from scheduler.kill(task)
            self.assertEqual(task.outcome.tag, "Err")
            self.assertEqual(children[0].outcome.tag, "Err")
            self.assertEqual(children[0].joiners, {})
            self.assertFalse(queue.senders)
            return list(queue.messages)

        self.assertEqual(scheduler.run(exercise()), [1])

    def test_cancellation_after_commit_keeps_message_and_terminal_result(self):
        scheduler = Scheduler()
        queue = Channel(1)

        def exercise():
            yield from queue.send(scheduler, OPERATIONS["try_send"], 1)
            sender = scheduler.start(queue.send(scheduler, OPERATIONS["send_blocking"], 2))
            yield
            self.assertEqual((yield from queue.receive(scheduler, OPERATIONS["receive_blocking"])), 1)
            yield from scheduler.kill(sender)
            self.assertEqual(sender.outcome.tag, "Err")
            self.assertEqual(list(queue.messages), [2])
            finished = scheduler.start(queue.receive(scheduler, OPERATIONS["receive_blocking"]))
            result = yield from scheduler.join(finished)
            yield from scheduler.kill(finished)
            self.assertIs((yield from scheduler.join(finished)), result)
            return result.value

        self.assertEqual(scheduler.run(exercise()), 2)

    def test_cleanup_in_progress_finishes_on_cancellation(self):
        program = compile_program("""
            fn cleanup(gate: Channel<int32>, output: shared console) -> unit {
                print(output, "cleanup starts");
                receive_blocking(gate);
                print(output, "cleanup finishes");
            }
            fn task(gate: Channel<int32>, output: shared console) -> int32 {
                defer cleanup(gate, output);
                return 1;
            }
            fn cancel(target: Thread<int32>) -> unit { kill(target); }
            fn main(workers: threads, channels: channels, output: shared console) -> result<int32, Killed> {
                let gate: Channel<int32> = create_channel(channels, Capacity::Bounded(1));
                let child = thread task(gate, output);
                unit;
                unit;
                unit;
                unit;
                let killer = thread cancel(child);
                unit;
                unit;
                try_send(gate, 1);
                join(killer);
                return join(child);
            }
        """)
        output = StringIO()
        value = Interpreter(program).call("main", (Threads(), Channels(), Console(StringIO(), output)))
        self.assertEqual(output.getvalue(), "cleanup starts\ncleanup finishes\n")
        self.assertEqual(display(value), "Err(Killed)")

    def test_ending_cannot_defer_an_ordinary_kill(self):
        with self.assertRaisesRegex(LanguageError, "lacks required guarantee.*ending"):
            compile_program("ending fn stop(target: Thread<unit>) -> unit { defer kill(target); }")
