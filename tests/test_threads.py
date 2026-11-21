"""Green-thread checking and scheduling."""

from io import StringIO
import unittest

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
