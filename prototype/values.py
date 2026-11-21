"""Data values shared by the interpreter and host environments."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Result:
    tag: str
    value: object


@dataclass(frozen=True)
class Variant:
    name: str
    tag: str
    value: object = None
    has_payload: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class Failure:
    code: str
    message: str = ""
