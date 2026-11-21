"""Exercise attached examples through the checker and CLI."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest

from prototype.checker import Checker
from prototype.syntax import LanguageError, Parser


ROOT = Path(__file__).resolve().parents[1]


class FunctionExamples(unittest.TestCase):
    def cli(self, command, source):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "examples.proto"
            path.write_text(dedent(source).lstrip(), encoding="utf-8")
            return subprocess.run([sys.executable, "-B", "-m", "prototype", command, str(path)],
                                  capture_output=True, text=True, cwd=ROOT, timeout=10)

    def test_nested_literals_and_independent_mutable_inputs(self):
        result = self.cli("test", """
            record Reading { distance: f64<meter>, labels: list<string> }
            enum ReadingState { Ready(Reading), Empty }

            example {
                input backup: ReadingState::Empty;
                input state: ReadingState::Empty;
                after backup: ReadingState::Empty;
                expect Ok(ReadingState::Empty);
                after state: ReadingState::Ready(Reading(labels: [], distance: -5e-1));
            }
            example {
                input backup: ReadingState::Ready(Reading(distance: 1.0, labels: ["saved"]));
                input state: ReadingState::Empty;
                expect Ok(ReadingState::Ready(Reading(distance: 1.0, labels: ["saved"])));
                after state: ReadingState::Ready(Reading(distance: -0.5, labels: []));
                after backup: ReadingState::Ready(Reading(distance: 1.0, labels: ["saved"]));
            }
            deterministic terminates fn configure(state: mut ReadingState, backup: mut ReadingState)
                    -> result<ReadingState, Zero> {
                state = ReadingState::Ready(Reading(distance: -0.5, labels: []));
                return Ok(backup);
            }

            example {
                input index: 3;
                expect Err(IndexError("index 3 is outside the list"));
            }
            deterministic terminates fn find(index: int32) -> result<int32, IndexError> {
                return at([10], index);
            }
        """)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3 passed, 0 failed (3 total)")

    def test_reports_every_mismatch_and_continues(self):
        result = self.cli("test", """
            example "first" { input n: 2; expect 0.5000000000000001; after n: 3; }
            example { input n: 8; expect 0.5; after n: 4; }
            example "third" { input n: 1; expect 0.0; after n: 4; }
            deterministic terminates fn reset(n: mut int32) -> f64 {
                n = 4;
                return 0.5;
            }
            example { expect Err(ParseIntError("wrong message")); }
            deterministic terminates fn parse() -> result<int32, ParseIntError> {
                return parseInt32("abc");
            }
            fn main(terminal: console) -> unit {}
        """)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "1 passed, 3 failed (4 total)")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("examples.proto:1:1: example for 'reset' \"first\": return value: "
                      "expected 0.5000000000000001, got 0.5", lines[0])
        self.assertIn("mut parameter 'n': expected 3, got 4", lines[1])
        self.assertIn("examples.proto:3:1: example for 'reset' \"third\"", lines[2])
        self.assertIn('expected Err(ParseIntError("wrong message")), '
                      'got Err(ParseIntError("expected a decimal integer"))', lines[3])

    def test_cases_are_checked_in_every_command_and_executed_only_by_test(self):
        source = "example { expect 1; } deterministic terminates fn main() -> int32 { return 2; }"
        for command, expected in (("check", "OK"), ("run", "2")):
            with self.subTest(command=command):
                result = self.cli(command, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.strip().endswith(expected))
        for command in ("check", "run", "test"):
            with self.subTest(command=command):
                result = self.cli(command, source.replace("expect 1", "expect true"))
                self.assertEqual(result.returncode, 1)
                self.assertIn("expected int32, found bool", result.stderr)
                self.assertEqual(result.stdout, "")
        empty = self.cli("test", "fn main(terminal: console) -> unit {}")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(empty.stdout.strip(), "0 passed, 0 failed (0 total)")

    def test_rejects_invalid_example_entries(self):
        signature = "deterministic terminates fn f(n: mut int32!0, read: int32) -> unit {}"
        for entries, error in (
            ("input read: 1; expect unit;", "missing an 'input' entry for 'n'"),
            ("input n: 1; input read: 1; expect unit;", "missing an 'after' entry for 'n'"),
            ("input n: 1; input read: 1; after n: 1;", "missing an 'expect' entry"),
            ("expect unit; expect unit;", "more than one 'expect' entry"),
            ("input n: 1; input n: 1;", "duplicate 'input' entry"),
            ("after n: 1; after n: 1;", "duplicate 'after' entry"),
            ("after read: 1;", "'read' is read-only"),
            ("input missing: 1;", "no parameter named 'missing'"),
            ("after n: 0;", "expected int32!0, found int32"),
            ("input n: read;", "values must be literals"),
            ("input n: 1 + 2;", "values must be literals"),
            ("input n: 1 as int32;", "values must be literals"),
            ("input n: min(1, 2);", "ordinary calls are not allowed"),
        ):
            with self.subTest(entries=entries):
                with self.assertRaisesRegex(LanguageError, error):
                    Checker(Parser(f"example {{ {entries} }} {signature}").program()).program()

    def test_checks_nested_values_and_function_guarantees(self):
        for expression in ("x", "min(1, 2)", "Pair(items: []).items"):
            with self.subTest(expression=expression):
                source = f"""
                    record Pair {{ items: list<int32> }}
                    example {{ expect Ok(Pair(items: [{expression}])); }}
                    deterministic terminates fn f() -> result<Pair, Overflow> {{ return Ok(Pair(items: [])); }}
                """
                with self.assertRaisesRegex(LanguageError, "values must be literals"):
                    Checker(Parser(source).program()).program()

        for modifiers in ("", "deterministic", "terminates"):
            with self.subTest(modifiers=modifiers):
                source = f"example {{ expect unit; }} {modifiers} fn f() -> unit {{}}"
                with self.assertRaisesRegex(LanguageError, "require a deterministic terminates function"):
                    Checker(Parser(source).program()).program()

    def test_rejects_nonfinite_decimal_literals(self):
        source = "fn main() -> f64 { return -1e999; }"
        with self.assertRaisesRegex(LanguageError, "outside the finite f64 range"):
            Parser(source).program()
