"""Save checked source graphs to SQLite and regenerate their .proto files."""

from bisect import bisect_left
from collections import defaultdict
from contextlib import closing
from dataclasses import fields, is_dataclass
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .modules import ModuleLoader
from .syntax import Function, GUARANTEES, Token, Type


SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE modules (
    path TEXT PRIMARY KEY,
    is_entry INTEGER NOT NULL CHECK (is_entry IN (0, 1)),
    mock_environment TEXT
);
CREATE UNIQUE INDEX entry_module ON modules (is_entry) WHERE is_entry = 1;
CREATE TABLE tokens (
    module TEXT NOT NULL REFERENCES modules,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    line INTEGER NOT NULL,
    column_number INTEGER NOT NULL,
    type_angle INTEGER NOT NULL,
    line_break INTEGER NOT NULL,
    PRIMARY KEY (module, ordinal)
);
CREATE TABLE comments (
    module TEXT NOT NULL REFERENCES modules,
    ordinal INTEGER NOT NULL,
    before_token INTEGER NOT NULL,
    inline INTEGER NOT NULL,
    text TEXT NOT NULL,
    line INTEGER NOT NULL,
    column_number INTEGER NOT NULL,
    PRIMARY KEY (module, ordinal)
);
CREATE TABLE imports (
    module TEXT NOT NULL REFERENCES modules,
    token INTEGER NOT NULL,
    spelling TEXT NOT NULL,
    namespace TEXT NOT NULL,
    target TEXT NOT NULL REFERENCES modules,
    PRIMARY KEY (module, token)
);
CREATE TABLE declarations (
    module TEXT NOT NULL REFERENCES modules,
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    exported INTEGER NOT NULL,
    syntax TEXT NOT NULL,
    effective_guarantees TEXT,
    commutative_parameters TEXT,
    PRIMARY KEY (module, ordinal),
    UNIQUE (module, name)
);
CREATE TABLE semantic_facts (
    module TEXT NOT NULL REFERENCES modules,
    token INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (module, token, kind),
    FOREIGN KEY (module, token) REFERENCES tokens (module, ordinal)
);
CREATE TABLE calls (
    module TEXT NOT NULL REFERENCES modules,
    token INTEGER NOT NULL,
    target_module TEXT REFERENCES modules,
    target_name TEXT NOT NULL,
    PRIMARY KEY (module, token),
    FOREIGN KEY (module, token) REFERENCES tokens (module, ordinal)
);
CREATE TABLE call_arguments (
    module TEXT NOT NULL,
    token INTEGER NOT NULL,
    evaluation_order INTEGER NOT NULL,
    parameter TEXT NOT NULL,
    mutable INTEGER NOT NULL,
    binding_token INTEGER,
    PRIMARY KEY (module, token, evaluation_order),
    FOREIGN KEY (module, token) REFERENCES calls,
    FOREIGN KEY (module, binding_token) REFERENCES tokens (module, ordinal)
);
CREATE TABLE layout (
    path TEXT PRIMARY KEY,
    link_target TEXT
);
"""


def import_layout(sources):
    """Capture directory traversal and aliases needed by the original import spellings."""
    directories = {path.parent for path in sources}
    links = {}
    for path, module in sources.items():
        for _, spelling, _, target in module.imports:
            location = path.parent
            for part in Path(spelling).parts:
                if part == "..":
                    location = location.parent
                    directories.add(location)
                    continue
                location /= part
                if location.is_symlink():
                    resolved = location.resolve()
                    links[location] = resolved
                    location = resolved
                if location != target:
                    directories.add(location)
            if location != target:
                raise ValueError(f"import changed while saving {path}: {spelling}")
    root = Path(os.path.commonpath([*directories, *(path.parent for path in links)]))
    return root, directories, links


def json_data(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ModuleRows:
    def __init__(self, path, parsed, paths):
        self.path = path
        self.parsed = parsed
        self.paths = paths
        self.tokens = parsed.parser.tokens[:-1]
        self.positions = [(token.line, token.column) for token in self.tokens]
        self.offsets = {position: index for index, position in enumerate(self.positions)}

    def offset(self, token):
        return self.offsets[token.line, token.column]

    def data(self, value):
        if isinstance(value, Token):
            return {"token": self.offset(value)}
        if isinstance(value, Type):
            return {
                "name": value.name, "module": self.paths[value.module] if value.module else None,
                "arguments": self.data(value.arguments), "units": value.units,
                "nonzero": value.nonzero, "shared": value.shared,
            }
        if is_dataclass(value):
            return {"class": type(value).__name__, **{
                field.name: self.data(getattr(value, field.name)) for field in fields(value)
            }}
        if isinstance(value, tuple):
            return [self.data(item) for item in value]
        if isinstance(value, frozenset):
            return sorted(value)
        return value

    def write(self, database):
        path, parser = self.path, self.parsed.parser
        database.executemany("INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            (path, index, token.kind, token.text, token.line, token.column,
             index in parser.type_angles, index in parser.line_breaks)
            for index, token in enumerate(self.tokens)
        ))
        for index, comment in enumerate(parser.comments):
            before = bisect_left(self.positions, (comment.line, comment.column))
            inline = before > 0 and self.tokens[before - 1].line == comment.line
            database.execute("INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?)", (
                path, index, before, inline, comment.text, comment.line, comment.column,
            ))
        database.executemany("INSERT INTO imports VALUES (?, ?, ?, ?, ?)", (
            (path, self.offset(token), spelling, namespace, self.paths[str(target)])
            for token, spelling, namespace, target in self.parsed.imports
        ))
        for index, declaration in enumerate(self.parsed.declarations):
            guarantees = commutative = None
            if isinstance(declaration, Function):
                effective = declaration.modifiers & GUARANTEES
                if "pure" in effective:
                    effective |= {"deterministic"}
                guarantees = json_data(sorted(effective))
                slots = declaration.commutative_slots
                commutative = json_data([parameter.name for slot, parameter in enumerate(declaration.parameters)
                                         if slot in slots])
            database.execute("INSERT INTO declarations VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                path, index, declaration.token.text, type(declaration).__name__,
                declaration.exported, json_data(self.data(declaration)), guarantees, commutative,
            ))
        self.write_facts(database)

    def write_facts(self, database):
        facts = {(self.offset(token), kind): value
                 for token, kind, value in self.parsed.checker.facts}
        for (token, kind), value in facts.items():
            if kind != "call":
                database.execute("INSERT INTO semantic_facts VALUES (?, ?, ?, ?)",
                                 (self.path, token, kind, json_data(self.data(value))))
                continue
            target, bindings = value
            module, name = target if isinstance(target, tuple) else (None, target)
            database.execute("INSERT INTO calls VALUES (?, ?, ?, ?)",
                             (self.path, token, self.paths[module] if module else None, name))
            database.executemany("INSERT INTO call_arguments VALUES (?, ?, ?, ?, ?, ?)", (
                (self.path, token, index, parameter, mutable,
                 self.offset(reference) if reference is not None else None)
                for index, (parameter, mutable, reference) in enumerate(bindings)
            ))


def save_program(source, destination):
    loader = ModuleLoader(capture=True)
    program = loader.load(source)
    root, directories, links = import_layout(loader.sources)
    paths = {str(path): path.relative_to(root).as_posix() for path in loader.sources}

    with NamedTemporaryFile(dir=destination.parent) as temporary:
        with closing(sqlite3.connect(temporary.name)) as database:
            database.execute("PRAGMA foreign_keys = ON")
            database.executescript(SCHEMA)
            with database:
                database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                for path, parsed in loader.sources.items():
                    mock = parsed.parser.mock
                    database.execute("INSERT INTO modules VALUES (?, ?, ?)", (
                        paths[str(path)], str(path) == program.entry, mock.text if mock else None,
                    ))
                for path, parsed in loader.sources.items():
                    ModuleRows(paths[str(path)], parsed, paths).write(database)
                database.executemany("INSERT INTO layout VALUES (?, NULL)", (
                    (path.relative_to(root).as_posix(),) for path in sorted(directories)
                ))
                database.executemany("INSERT INTO layout VALUES (?, ?)", (
                    (path.relative_to(root).as_posix(), target.relative_to(root).as_posix())
                    for path, target in sorted(links.items())
                ))
        os.link(temporary.name, destination)


def render_source(tokens, comments):
    """Format tokens while keeping comments in their original token gaps."""
    gaps = defaultdict(list)
    for before, inline, text in comments:
        gaps[before].append((inline, text))

    lines = []
    line = ""
    depth = angles = 0
    previous = previous_kind = ""
    previous_angle = pending_break = False

    def flush():
        nonlocal line
        if line:
            lines.append(line)
            line = ""

    for index, (text, kind, type_angle, line_break) in enumerate(tokens + [(None, "", False, False)]):
        for inline, comment in gaps[index]:
            if inline and line:
                line += " " + comment
            else:
                flush()
                line = "    " * depth + comment
            flush()
        if text is None:
            flush()
            break
        punct = kind == "punct"
        empty_block = punct and text == "}" and previous == "{"
        if pending_break and not (punct and text in {",", ";"}) and not empty_block and text != "else":
            flush()
        if punct and text == "}":
            if not empty_block:
                flush()
            depth -= 1

        call_open = text == "(" and previous_kind == "name" and previous not in {
            "if", "match", "return", "expect", "else",
        }
        tight = (punct and text in {")", "]", ",", ";", ":", ".", "::", "!0"}
                 or call_open or empty_block
                 or type_angle
                 or previous in {"(", "[", ".", "::", "-"}
                 or previous_angle and previous == "<"
                 or angles and (text in {"*", "/"} or previous in {"*", "/"}))
        if not line:
            line = "    " * depth
        elif not tight:
            line += " "
        line += text

        if punct and text == "{":
            depth += 1
        if type_angle:
            angles += 1 if text == "<" else -1
        pending_break = line_break or punct and text in {"{", "}", ";"}
        previous, previous_kind, previous_angle = text, kind, type_angle

    return "\n".join(lines) + ("\n" if lines else "")


def portable_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or "\0" in value:
        raise ValueError(f"invalid snapshot path: {value!r}")
    return path


def export_program(source, destination):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"export destination already exists: {destination}")
    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        version, = database.execute("PRAGMA user_version").fetchone()
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported program database schema version {version}")
        modules = [(portable_path(path), entry) for path, entry in
                   database.execute("SELECT path, is_entry FROM modules ORDER BY path")]
        entries = [path for path, is_entry in modules if is_entry]
        if len(entries) != 1:
            raise ValueError("expected one entry module in the program database")
        layout = [
            (portable_path(path), portable_path(target) if target is not None else None)
            for path, target in database.execute("SELECT path, link_target FROM layout ORDER BY path")
        ]
        links = {path for path, target in layout if target is not None}
        physical = {path for path, _ in modules} | {path for path, target in layout if target is None}
        if (links & physical or PurePosixPath(".") in links
                or any(links.intersection(path.parents) for path in links | physical)
                or any(target is not None and target not in physical for _, target in layout)):
            raise ValueError("invalid snapshot import layout")

        with TemporaryDirectory(dir=destination.parent) as temporary:
            stage = Path(temporary) / "program"
            stage.mkdir()
            for path, target in layout:
                if target is None:
                    (stage / path).mkdir(parents=True, exist_ok=True)
            for path, _ in modules:
                tokens = database.execute(
                    """SELECT text, kind, type_angle, line_break FROM tokens
                       WHERE module = ? ORDER BY ordinal""", (path.as_posix(),)
                ).fetchall()
                comments = database.execute(
                    """SELECT before_token, inline, text FROM comments
                       WHERE module = ? ORDER BY ordinal""", (path.as_posix(),)
                )
                output = stage / path
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(render_source(tokens, comments), encoding="utf-8")
            for path, target in layout:
                if target is not None:
                    link = stage / path
                    link.symlink_to(os.path.relpath(stage / target, link.parent))
            loader = ModuleLoader(root=stage.resolve())
            loader.load(stage / entries[0])
            if set(loader.scopes) != {(stage / path).resolve() for path, _ in modules}:
                raise ValueError("exported import graph differs from the program database")
            destination.mkdir()
            try:
                stage.rename(destination)
            except OSError:
                destination.rmdir()
                raise
    return destination / entries[0]
