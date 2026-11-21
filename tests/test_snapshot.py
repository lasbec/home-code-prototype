import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from prototype.snapshot import export_program, save_program
from prototype.syntax import LanguageError


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROWS = (
    "SELECT * FROM modules ORDER BY path",
    "SELECT module, ordinal, kind, text, type_angle, line_break FROM tokens ORDER BY module, ordinal",
    "SELECT module, ordinal, before_token, inline, text FROM comments ORDER BY module, ordinal",
    "SELECT * FROM imports ORDER BY module, token",
    "SELECT * FROM declarations ORDER BY module, ordinal",
    "SELECT * FROM semantic_facts ORDER BY module, token, kind",
    "SELECT * FROM calls ORDER BY module, token",
    "SELECT * FROM call_arguments ORDER BY module, token, evaluation_order",
    "SELECT * FROM layout ORDER BY path",
)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "-m", "prototype", *map(str, arguments)], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def assert_snapshots_equal(self, before, after):
        with sqlite3.connect(before) as first, sqlite3.connect(after) as second:
            for query in SNAPSHOT_ROWS:
                with self.subTest(query=query):
                    self.assertEqual(first.execute(query).fetchall(), second.execute(query).fetchall())

    def test_runner_round_trip(self):
        before, after = self.work / "before.sqlite", self.work / "after.sqlite"
        self.cli("save", "examples/runner/main.proto", before)
        entry = Path(self.cli("export", before, self.work / "generated"))
        self.assertEqual(entry.name, "main.proto")
        self.assertIn("7 passed, 0 failed", self.cli("test", "-r", entry))
        self.cli("save", entry, after)
        self.assert_snapshots_equal(before, after)

    def test_comments_examples_and_resolved_facts_without_original_files(self):
        app = self.work / "source/app"
        shared = self.work / "source/shared"
        app.mkdir(parents=True)
        shared.mkdir()
        types = shared / "types.proto"
        helper = shared / "helper.proto"
        main = app / "main.proto"
        types.write_text("export record Tag { text: string }\n", encoding="utf-8")
        helper.write_text(
            'import "./types.proto"\n'
            'export pure ending fn text(count: int32, tag: types.Tag) -> string { return tag.text; }\n',
            encoding="utf-8",
        )
        main.write_text(
            '// header\n'
            'import "../shared/helper.proto" // import\n'
            'import "../shared/types.proto"\n'
            'example "kept" { expect "yes"; } // example\n'
            'pure deterministic ending fn go() -> string {\n'
            '  let tag = types.Tag( // constructor\n'
            '      text: "yes"\n'
            '  );\n'
            '  return helper.text(tag, // first evaluated\n'
            '      // count comes second\n'
            '      2);\n'
            '} // end\n'
            '// footer\n',
            encoding="utf-8",
        )
        before, after = self.work / "before.sqlite", self.work / "after.sqlite"
        save_program(main, before)
        with sqlite3.connect(before) as database:
            guarantees, = database.execute(
                "SELECT effective_guarantees FROM declarations WHERE name = 'text'"
            ).fetchone()
            self.assertEqual(json.loads(guarantees), ["deterministic", "ending", "pure"])
            actual = database.execute("""
                SELECT calls.target_module, call_arguments.parameter
                FROM calls JOIN call_arguments USING (module, token)
                WHERE calls.target_name = 'text' ORDER BY call_arguments.evaluation_order
            """).fetchall()
            self.assertEqual(actual, [("shared/helper.proto", "tag"), ("shared/helper.proto", "count")])
            local_type, = database.execute("""
                SELECT value FROM semantic_facts JOIN tokens USING (module)
                WHERE semantic_facts.token = tokens.ordinal AND semantic_facts.kind = 'local_type'
                  AND tokens.text = 'tag' AND module = 'app/main.proto'
            """).fetchone()
            self.assertEqual(json.loads(local_type)["module"], "shared/types.proto")
        for path in (main, helper, types):
            path.unlink()

        entry = export_program(before, self.work / "generated")
        self.assertEqual(entry, self.work / "generated/app/main.proto")
        self.assertIn("1 passed, 0 failed", self.cli("test", "-r", entry))
        save_program(entry, after)
        self.assert_snapshots_equal(before, after)

    def test_alias_import_preserves_module_identity(self):
        app = self.work / "app"
        app.mkdir()
        types = app / "types.proto"
        types.write_text("export record Tag { text: string }\n", encoding="utf-8")
        (app / "alias.proto").symlink_to(types.name)
        main = app / "main.proto"
        main.write_text(
            'import "./alias.proto"\nimport "./types.proto"\n'
            'fn make() -> alias.Tag { return types.Tag(text: "yes"); }\n',
            encoding="utf-8",
        )
        before, after = self.work / "before.sqlite", self.work / "after.sqlite"
        save_program(main, before)
        output = self.work / "generated"
        entry = export_program(before, output)
        self.assertEqual((output / "alias.proto").resolve(), output / "types.proto")
        save_program(entry, after)
        self.assert_snapshots_equal(before, after)

    def test_invalid_source_leaves_no_database(self):
        destination = self.work / "program.sqlite"
        with self.assertRaises(LanguageError):
            save_program(ROOT / "examples/rejected/divisor.proto", destination)
        self.assertFalse(destination.exists())

    def test_existing_destinations_are_left_intact(self):
        source = ROOT / "examples/numeric.proto"
        database = self.work / "program.sqlite"
        save_program(source, database)
        original = database.read_bytes()
        with self.assertRaises(FileExistsError):
            save_program(source, database)
        self.assertEqual(database.read_bytes(), original)
        output = self.work / "generated"
        export_program(database, output)
        original = (output / "numeric.proto").read_bytes()
        with self.assertRaises(FileExistsError):
            export_program(database, output)
        self.assertEqual((output / "numeric.proto").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
