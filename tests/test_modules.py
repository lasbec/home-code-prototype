"""Exercise module scopes and recursive examples through the CLI."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Modules(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def source(self, path, contents):
        path = self.directory / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def cli(self, *args, input=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "prototype", *args],
            cwd=self.directory, env=dict(os.environ, PYTHONPATH=str(ROOT)),
            input=input, text=True, capture_output=True, timeout=10,
        )

    def test_qualified_functions_and_types_in_a_diamond(self):
        self.source("shared/core.proto", """
            export enum Kind { Bonus(int32), Empty }
            export record Score { kind: Kind }
            example { expect Score(kind: Kind::Bonus(3)); }
            export pure deterministic ending fn sample() -> Score {
                return Score(kind: Kind::Bonus(3));
            }
        """)
        self.source("team/left.proto", """
            import "../shared/core.proto"
            pure ending fn extra() -> int32 { return 1; }
            export pure ending fn score() -> core.Score {
                return core.Score(kind: core.Kind::Bonus(extra()));
            }
        """)
        self.source("team/right.proto", """
            import "../shared/core.proto"
            pure ending fn extra() -> int32 { return 9; }
            export pure ending fn describe(score: core.Score) -> int32 {
                match score.kind {
                    Bonus(points) { return points; }
                    Empty { return extra(); }
                }
            }
        """)
        self.source("main.proto", """
            import "./team/left.proto"
            import "./team/right.proto"
            example { expect 1; }
            pure deterministic ending fn main() -> int32 {
                return right.describe(left.score());
            }
        """)
        run = self.cli("run", "main.proto")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "1\n")
        for options, expected in (((), 1), (("-r",), 2)):
            examples = self.cli("test", *options, "main.proto")
            self.assertEqual(examples.returncode, 0, examples.stderr)
            self.assertIn(f"{expected} passed", examples.stdout)

    def test_private_declarations_and_globals_are_rejected(self):
        self.source("library.proto", "fn hidden() -> int32 { return 0; }")
        self.source("main.proto", 'import "./library.proto"\nfn main() -> int32 { return library.hidden(); }')
        self.assertIn("unknown function 'library.hidden'", self.cli("check", "main.proto").stderr)
        self.source("library.proto", "record Hidden {}\nexport fn leak() -> list<Hidden> { return []; }")
        error = self.cli("check", "main.proto").stderr
        self.assertIn("library.proto:2:11", error)
        self.assertIn("private type", error)
        self.source("main.proto", "export let mut counter = 0;")
        self.assertIn("expected 'fn', found 'let'", self.cli("check", "main.proto").stderr)

    def test_mutation_through_an_imported_function(self):
        self.source("library.proto", "export fn set(value: mut int32) -> unit { value = 2; }")
        self.source("main.proto", """
            import "./library.proto"
            fn main() -> int32 {
                let mut value = 1;
                library.set(mut value);
                return value;
            }
        """)
        run = self.cli("run", "main.proto")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "2\n")
        self.source("library.proto", "export fn set(value: mut int32) -> unit {}")
        error = self.cli("check", "main.proto").stderr
        self.assertIn("library.proto:", error)
        self.assertIn("mut parameter 'value' is never written", error)

    def test_types_with_the_same_name_remain_distinct(self):
        for name in ("first", "second"):
            self.source(f"{name}.proto", "export record Score { value: int32 }")
        self.source("main.proto", """
            import "./first.proto"
            import "./second.proto"
            fn main() -> first.Score { return second.Score(value: 1); }
        """)
        self.assertIn("expected first.Score, found second.Score", self.cli("check", "main.proto").stderr)

    def test_invalid_namespace_duplicate_namespace_and_cycle(self):
        self.source("main.proto", 'import "./high-scores.proto"')
        self.assertIn("not a valid import namespace", self.cli("check", "main.proto").stderr)
        self.source("a/shared.proto", "")
        self.source("main.proto", 'import "./a/shared.proto"\nimport "./b/shared.proto"')
        self.assertIn("duplicate import namespace 'shared'", self.cli("check", "main.proto").stderr)
        self.source("a/shared.proto", 'import "../main.proto"')
        self.assertIn("circular import: main.proto -> shared.proto -> main.proto",
                      self.cli("check", "main.proto").stderr)

    def test_recursive_example_failure_reports_the_imported_source(self):
        self.source("library.proto", """
            example { expect 0; }
            pure deterministic ending fn fails() -> int32 { return 1; }
        """)
        self.source("main.proto", 'import "./library.proto"')
        self.assertEqual(self.cli("test", "main.proto").returncode, 0)
        failures = self.cli("test", "-r", "main.proto")
        self.assertEqual(failures.returncode, 1)
        self.assertIn("library.proto:2:13: example for 'fails'", failures.stderr)

    def test_runner_saves_after_a_crash_and_reloads_the_leaderboard(self):
        source = str(ROOT / "examples" / "runner" / "main.proto")
        flags = ("--console", "--filesystem", "--random")
        run = self.cli("run", source, *flags, input="1\nAda\n" + "\n" * 20 + "3\n")
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.splitlines()
        frame_starts = [index for index, line in enumerate(lines) if line.startswith("┌")]
        self.assertGreater(len(frame_starts), 1)
        for start in frame_starts:
            frame = lines[start:start + 40]
            self.assertEqual([len(line) for line in frame], [80] * 40)
            self.assertTrue(frame[-1].startswith("└"))
            self.assertIn("COINS", frame[1])
            self.assertIn("DISTANCE", frame[1])
            self.assertIn("◉", "\n".join(frame))
        self.assertRegex(run.stdout, r"You crashed\. [23] coins")
        self.assertIn("Best score saved.", run.stdout)
        saved = (self.directory / "runner-scores.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(saved[0], "Ada")
        self.assertIn(saved[1], {"2", "3"})

        restarted = self.cli("run", source, *flags, input="2\n3\n")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertIn(f"Leaderboard\nAda: {saved[1]} coins, {saved[2]} tiles", restarted.stdout)
