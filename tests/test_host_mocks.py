"""Exercise self-contained host mocks through the CLI."""

import json
import os
from pathlib import Path
from select import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = """
    mock console
    enum Marker { Absent, Present(unit) }
    record State { lines: list<string>, next: int32, output: list<string>,
                   marker: Marker, error: HostError }

    pure ending fn init() -> State {
        return State(lines: ["default"], next: 0, output: [], marker: Marker::Present(unit),
                     error: HostError(HostErrorKind::EndOfInput, "no more lines"));
    }
    export fn print(state: mut State, text: string) -> result<unit, HostError> {
        return write(mut state, concat(left: text, right: "\\n"));
    }
    export fn write(state: mut State, text: string) -> result<unit, HostError> {
        state = State(lines: state.lines, next: state.next, output: prepend(text, state.output),
                      marker: state.marker, error: state.error);
        return Ok(unit);
    }
    export fn input(state: mut State) -> result<string, HostError> {
        match at(state.lines, state.next) {
            Ok(line) {
                match state.next + 1 {
                    Ok(next) {
                        state = State(lines: state.lines, next: next, output: state.output,
                                      marker: state.marker, error: state.error);
                        return Ok(line);
                    }
                    Err(error) { return Err(state.error); }
                }
            }
            Err(error) { return Err(state.error); }
        }
    }
    export fn enableKeyInput(state: mut State) -> result<bool, HostError> { return Ok(false); }
    export fn disableKeyInput(state: mut State) -> result<unit, HostError> { return Ok(unit); }
    export fn readKeysFor(state: mut State, timeout: int32<ms>) -> result<list<string>, HostError> {
        return Ok([]);
    }
"""
RANDOM = """
    mock random
    export commutative bound0 bound1
    fn random(state: mut unit, bound0: int32, bound1: int32) -> result<int32, HostError> {
        return Ok(min(bound0, bound1));
    }
"""


class HostMocks(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def file(self, name, contents):
        path = self.directory / name
        path.write_text(dedent(contents).lstrip(), encoding="utf-8")
        return path

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "prototype", *map(str, args)],
                              capture_output=True, text=True, cwd=ROOT, timeout=10)

    def test_fixture_precedes_init_and_final_state_can_seed_another_run(self):
        mock = self.file("console.proto", CONSOLE)
        initial = self.file("start.literal", '''
            State(lines: ["first", "second"], next: 0, output: [], marker: Marker::Present(unit),
                  error: HostError(HostErrorKind::EndOfInput, "fixture exhausted"))
        ''')
        final = self.directory / "final.literal"
        app = self.file("app.proto", """
            fn relay(terminal: console) -> unit {
                match input(terminal) {
                    Ok(line) { print(terminal, line); }
                    Err(error) {}
                }
            }
            fn main(terminal: console) -> result<string, HostError> {
                relay(terminal);
                relay(terminal);
                return input(terminal);
            }
        """)
        run = self.cli("run", app, f"--console={mock}", f"--console-state={initial}",
                       f"--console-state-out={final}")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "Err(EndOfInput: fixture exhausted)")
        snapshot = final.read_text(encoding="utf-8")
        self.assertEqual(snapshot, 'State(lines: ["first", "second"], next: 2, '
                         'output: ["second\\n", "first\\n"], marker: Marker::Present(unit), '
                         'error: HostError(HostErrorKind::EndOfInput, "fixture exhausted"))\n')
        for command in (("check", mock), ("run", app, f"--console={mock}")):
            checked = self.cli(*command, f"--console-state={final}")
            self.assertEqual(checked.returncode, 0, checked.stderr)

        default = self.cli("run", app, f"--console={mock}", f"--console-state-out={final}")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn('next: 1, output: ["default\\n"]', final.read_text(encoding="utf-8"))

    def test_mock_random_and_live_console_can_run_together(self):
        mock = self.file("random.proto", RANDOM)
        app = self.file("app.proto", """
            fn main(terminal: console, source: random) -> unit {
                match random(source, 9, -3) {
                    Ok(number) { print(terminal, formatInt32(number)); }
                    Err(error) {}
                }
            }
        """)
        final = self.directory / "final.literal"
        run = self.cli("run", "--console", app, f"--random={mock}", f"--random-state-out={final}")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "-3\n")
        self.assertEqual(final.read_text(encoding="utf-8"), "unit\n")

        conflict = self.cli("run", app, "--console", "--random", f"--random={mock}")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("select one provider for random", conflict.stderr)

        wrong_environment = self.cli("run", app, f"--console={mock}")
        self.assertNotEqual(wrong_environment.returncode, 0)
        self.assertIn("requires 'mock console'", wrong_environment.stderr)

    def test_filesystem_write_is_visible_to_later_reads(self):
        mock = self.file("filesystem.proto", """
            mock filesystem
            export fn readFileUTF8(state: mut string, path: string) -> result<string, HostError> {
                return Ok(state);
            }
            export fn writeFileUTF8(state: mut string, path: string, text: string)
                    -> result<unit, HostError> {
                state = text;
                return Ok(unit);
            }
            export fn replaceFileUTF8(state: mut string, path: string, text: string)
                    -> result<unit, HostError> {
                return writeFileUTF8(mut state, path: path, text: text);
            }
        """)
        disk_file = self.directory / "actual.txt"
        app = self.file("app.proto", """
            fn main(files: filesystem) -> result<string, HostError> {
                replaceFileUTF8(files, path: DISK_FILE, text: "updated");
                return readFileUTF8(files, DISK_FILE);
            }
        """.replace("DISK_FILE", json.dumps(str(disk_file))))
        missing = self.cli("run", app, f"--filesystem={mock}")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("mock requires --filesystem-state", missing.stderr)

        initial = self.file("initial.literal", '"initial"')
        run = self.cli("run", app, f"--filesystem={mock}", f"--filesystem-state={initial}")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "Ok(updated)\n")
        self.assertFalse(disk_file.exists())

    def test_mock_contract_checks_entire_file_and_disallows_imports(self):
        for source, error in (
            ("mock console", "missing export 'print'"),
            (RANDOM + "export record Extra {}", "unexpected export 'Extra'"),
            (RANDOM + "export fn extra() -> unit {}", "unexpected export 'extra'"),
            (RANDOM + RANDOM.replace("mock random", ""), "name 'random' is already in use"),
            (RANDOM.replace("commutative bound0 bound1", ""), "mock export must match"),
            (RANDOM.replace("state: mut unit", "state: unit"), "mock export must match"),
            (RANDOM + "fn idle() -> unit { input(unit); }", "cannot call live host function 'input'"),
            (RANDOM + "record Spare { value: list<console> }", "cannot use host capability type 'console'"),
            (RANDOM + "fn init() -> unit {}", "mock initializer must match"),
            (RANDOM.replace("mock random", 'mock random\nimport "other.proto"'), "cannot import other files"),
            (CONSOLE.replace("state: mut State, timeout", "state: mut unit, timeout"), "shared mock state type"),
        ):
            with self.subTest(error=error):
                result = self.cli("check", self.file("mock.proto", source))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)

        self.file("mock.proto", RANDOM)
        application = self.file("app.proto", 'import "mock.proto"\nfn main() -> unit {}')
        result = self.cli("check", application)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mock files cannot be imported", result.stderr)

    def test_invalid_fixtures_are_rejected_as_single_typed_literals(self):
        mock = self.file("random.proto", RANDOM)
        for fixture, error in (("true", "expected unit, found bool"),
                               ("unit unit", "expected '<end of file>'"),
                               ("init()", "ordinary calls are not allowed"),
                               ('"<end of file>"', "expected unit, found string")):
            with self.subTest(fixture=fixture):
                initial = self.file("bad.literal", fixture)
                result = self.cli("check", mock, f"--random-state={initial}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(initial), result.stderr)
                self.assertIn(error, result.stderr)

    def test_interpreter_failure_leaves_snapshot_untouched(self):
        mock = self.file("console.proto", CONSOLE)
        final = self.file("final.literal", "previous snapshot\n")
        app = self.file("app.proto", """
            fn forever() -> unit { forever(); }
            fn main(terminal: console) -> unit {
                print(terminal, "Starting");
                forever();
            }
        """)
        run = self.cli("run", app, f"--console={mock}", f"--console-state-out={final}")
        self.assertEqual(run.returncode, 1)
        self.assertIn("call stack limit", run.stderr)
        self.assertEqual(final.read_text(encoding="utf-8"), "previous snapshot\n")

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals and pipe polling")
    def test_interrupt_leaves_final_state_file_untouched(self):
        mock = self.file("random.proto", RANDOM)
        final = self.directory / "final.literal"
        app = self.file("app.proto", """
            fn main(terminal: console, source: random) -> unit {
                random(source, 1, 2);
                print(terminal, "Ready");
                loop {}
            }
        """)
        command = [sys.executable, "-B", "-m", "prototype", "run", str(app), "--console",
                   f"--random={mock}", f"--random-state-out={final}"]
        with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as run:
            try:
                ready, _, _ = select([run.stdout], [], [], 10)
                self.assertTrue(ready, "program did not announce readiness")
                self.assertEqual(run.stdout.readline(), b"Ready\n")
                run.send_signal(signal.SIGINT)
                _, error = run.communicate(timeout=10)
                self.assertEqual(run.returncode, 130, error.decode())
            finally:
                if run.poll() is None:
                    run.kill()
        self.assertFalse(final.exists())
