"""Load and check the source files reachable from an entry file."""

from dataclasses import dataclass
from pathlib import Path
import re

from .checker import Checker
from .syntax import LanguageError, NAME_PATTERN, Parser, RESERVED


@dataclass(frozen=True)
class Program:
    entry: str
    functions: dict
    mock: Checker | None = None

    def load_state(self, path):
        parser = Parser(path.read_text(encoding="utf-8"), filename=str(path))
        return self.mock.literal(parser.literal(), self.mock.state_type)


@dataclass(frozen=True)
class ParsedModule:
    parser: Parser
    imports: tuple
    declarations: tuple
    checker: Checker


class ModuleLoader:
    def __init__(self, *, capture=False, root=None):
        self.active = []
        self.scopes = {}
        self.checked = {}
        self.sources = {} if capture else None
        self.root = root

    def load(self, source):
        path = source.resolve()
        scope = self.visit(source, path)
        functions = {
            key: function for module in self.checked.values() for key, function in module.items()
        }
        return Program(str(path), functions, scope if scope.mock_header else None)

    def visit(self, source, path=None, import_token=None):
        try:
            if path is None:
                path = source.resolve()
            if self.root is not None and not path.is_relative_to(self.root):
                raise ValueError(f"module is outside the program directory: {path}")
            if path in self.active:
                cycle = self.active[self.active.index(path):] + [path]
                chain = " -> ".join(item.name for item in cycle)
                raise LanguageError(import_token, f"circular import: {chain}")
            if path in self.scopes:
                scope = self.scopes[path]
                if import_token and scope.mock_header:
                    raise LanguageError(import_token, "mock files cannot be imported")
                return scope
            contents = path.read_text(encoding="utf-8")
        except (OSError, ValueError, RuntimeError) as error:
            if import_token is None:
                raise
            raise LanguageError(import_token, f"cannot load import {import_token.text}: {error}") from error

        self.active.append(path)
        self.checked[path] = {}
        capture = self.sources is not None
        parser = Parser(contents, filename=str(source), module=str(path), capture=capture)
        mock = parser.mock_header()
        if mock and import_token:
            raise LanguageError(import_token, "mock files cannot be imported")
        if mock and source.suffix != ".proto":
            raise LanguageError(mock, "mock source must be a .proto file")
        if mock and parser.current.text == "import":
            raise LanguageError(parser.current, "mock files cannot import other files")
        imports = {}
        edges = []
        for token, text in parser.imports():
            relative = Path(text)
            if relative.is_absolute() or relative.suffix != ".proto":
                raise LanguageError(token, "expected a relative path to a .proto file")
            namespace = relative.stem
            if not re.fullmatch(NAME_PATTERN, namespace) or namespace in RESERVED:
                raise LanguageError(token, f"'{namespace}' is not a valid import namespace")
            if namespace in imports:
                raise LanguageError(token, f"duplicate import namespace '{namespace}'")
            location = path.parent / relative
            dependency = self.visit(location, import_token=token)
            imports[namespace] = dependency
            if capture:
                edges.append((token, text, namespace, location.resolve()))
            parser.data_types.update({
                f"{namespace}.{name}": value_type for name, value_type in dependency.public_types.items()
            })

        declarations = parser.program()
        scope = Checker(declarations, module=str(path), imports=imports,
                        entry=import_token is None, mock=mock, capture=capture)
        self.checked[path] = scope.program()
        if capture:
            self.sources[path] = ParsedModule(parser, tuple(edges), declarations, scope)
        self.scopes[path] = scope
        self.active.pop()
        return scope
