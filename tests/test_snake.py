"""Regression checks for the Snake example and the language constructs it uses."""

from io import StringIO
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from prototype.checker import Checker
from prototype.runtime import Console, Filesystem, Interpreter, Record, Variant
from prototype.syntax import LanguageError, Parser


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "snake.proto"


def compile_program(source):
    return Interpreter(Checker(Parser(source).program()).program())


def position(x, y):
    return Record("Position", (("x", x), ("y", y)))


def direction(tag):
    return Variant("Direction", tag)


def game(head, body, facing, food, score=0):
    return Record("Game", (
        ("head", head), ("body", body), ("direction", direction(facing)),
        ("food", Variant("Food", "Available", food)), ("score", score),
    ))


class SnakeRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interpreter = compile_program(SOURCE.read_text(encoding="utf-8"))

    def step(self, state, facing, outcome):
        result = self.interpreter.call("advance", (state, direction(facing)))
        self.assertEqual(result.tag, "Ok")
        self.assertEqual(result.value.tag, outcome)
        return dict(result.value.value.fields) if result.value.value is not None else None

    def test_movement_reversal_and_food(self):
        initial = self.interpreter.call("initialGame", ())
        self.step(initial, "Left", "Reversed")
        moved = self.step(initial, "Up", "Moved")
        self.assertEqual(moved["head"], position(3, 2))
        self.assertEqual(moved["body"], (position(3, 3), position(2, 3)))
        self.assertEqual(moved["score"], 0)

        grown = self.step(initial, "Right", "Moved")
        self.assertEqual(grown["head"], position(4, 3))
        self.assertEqual(len(grown["body"]), 3)
        self.assertEqual(grown["score"], 1)
        self.assertEqual(grown["food"].value, position(0, 0))

    def test_wall_and_body_collisions_and_departing_tail(self):
        edge = game(position(11, 4), (position(10, 4), position(9, 4)),
                    "Right", position(0, 0))
        self.step(edge, "Right", "HitWall")

        body = (position(2, 3), position(3, 3), position(3, 2))
        corner = game(position(2, 2), body, "Up", position(0, 0))
        moved = self.step(corner, "Right", "Moved")
        self.assertEqual(moved["head"], position(3, 2))
        self.assertNotIn(moved["head"], moved["body"])

        longer = game(position(2, 2), (*body, position(4, 2)), "Up", position(0, 0))
        self.step(longer, "Right", "HitBody")

    def test_last_free_cell_wins(self):
        path = [position(x, y) for y in range(8)
                for x in (range(12) if y % 2 == 0 else range(11, -1, -1))]
        nearly_full = game(path[-2], tuple(reversed(path[:-2])), "Left", path[-1], 92)
        finished = self.step(nearly_full, "Left", "Won")
        self.assertEqual(finished["score"], 93)
        self.assertEqual(finished["food"].tag, "Full")
        self.assertEqual(len(finished["body"]), 95)

    def test_write_failure_reports_score_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "scores.txt")
            target.write_text("7\n", encoding="utf-8")
            terminal = Console(StringIO("d\n" * 9), StringIO())
            with patch("prototype.runtime.Path.write_bytes", side_effect=PermissionError("read only")):
                result = self.interpreter.call("play", (terminal, Filesystem(), str(target)))
            output = terminal.stdout.getvalue()
            self.assertEqual(result.tag, "Ok")
            self.assertIn("Final score: 1", output)
            self.assertIn("Score not saved: WriteError: read only", output)
            self.assertEqual(target.read_bytes(), b"7\n")


class SnakePersistence(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = workspace.name
        self.scores = Path(self.directory, "snake-scores.txt")

    def play(self, commands):
        command = [sys.executable, "-B", "-m", "prototype", "run", str(SOURCE),
                   "--console", "--filesystem"]
        result = subprocess.run(command, input=commands, text=True, capture_output=True,
                                cwd=self.directory, env=dict(os.environ, PYTHONPATH=str(ROOT)),
                                timeout=10, check=True)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_completed_rounds_persist_between_runs(self):
        first = self.play("d\n" * 9)
        self.assertIn("Best score: 0", first)
        self.assertIn("You hit a wall. Final score: 1", first)
        self.assertIn("Score saved.", first)
        second = self.play("w\n" * 4)
        self.assertIn("Best score: 1", second)
        self.assertIn("You hit a wall. Final score: 0", second)
        self.assertEqual(self.scores.read_bytes(), b"1\n0\n")

    def test_reversal_invalid_command_quit_and_eof(self):
        result = self.play("?\na\nq\n")
        self.assertIn("Enter w, a, s, d, or q.", result)
        self.assertIn("The snake cannot reverse directly.", result)
        self.assertEqual(result.count("Score: 0"), 1)
        self.assertIn("Round abandoned.", result)
        self.assertIn("Round abandoned.", self.play("d\n"))
        self.assertFalse(self.scores.exists())

    def test_malformed_or_unreadable_file_is_preserved(self):
        for contents in (b"5\ninvalid\n", b"5\n-1\n", b"\xff", b"5\n\n"):
            with self.subTest(contents=contents):
                self.scores.write_bytes(contents)
                result = self.play("w\n" * 4)
                self.assertIn("Score saving disabled:", result)
                self.assertIn("Score not saved.", result)
                self.assertEqual(self.scores.read_bytes(), contents)

    def test_existing_history_can_end_without_newline(self):
        self.scores.write_bytes(b"12\r\n9")
        result = self.play("w\n" * 4)
        self.assertIn("Best score: 12", result)
        self.assertEqual(self.scores.read_bytes(), b"12\n9\n0\n")


class LanguageRules(unittest.TestCase):
    def test_guarantees_and_exhaustiveness(self):
        cases = (
            ("terminates fn main() -> unit { loop {} }", "cannot guarantee termination"),
            ("""pure fn advance(terminal: console) -> result<string, HostError> {
                   return input(terminal);
               }""", "lacks required guarantee(s): pure"),
            ("""enum Turn { Moved(int32), HitWall }
               fn check(turn: Turn) -> int32 {
                   match turn { Moved(score) { return score; } }
               }""", "match is missing arm(s): HitWall"),
            ("""fn main() -> unit {
                   let mut divisor = 2;
                   loop { 10 / divisor as f64; divisor = 0; }
               }""", "division needs a proven nonzero divisor"),
        )
        for source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaisesRegex(LanguageError, re.escape(diagnostic)):
                    compile_program(source)

    def test_nested_and_empty_lists(self):
        interpreter = compile_program("""
            pure deterministic terminates fn main() -> list<list<int32>> {
                let rows = prepend([1], [[2]]);
                let single = dropLast(rows);
                return prepend(item: [3], items: single);
            }
        """)
        self.assertEqual(interpreter.call("main", ()), ((3,), (1,)))

    def test_loop_runs_without_consuming_the_call_stack(self):
        interpreter = compile_program("""
            fn main() -> int32 {
                let mut count: int32 = 0;
                loop {
                    if count == 1500 { return count; }
                    match count + 1 {
                        Ok(next) { count = next; }
                        Err(error) { return -1; }
                    }
                }
            }
        """)
        self.assertEqual(interpreter.call("main", ()), 1500)
