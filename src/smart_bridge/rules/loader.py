"""Regeln laden und validieren: JSON Schema, dann Pydantic."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import Rule, RulesFile


def rules_json_schema() -> dict[str, Any]:
    schema = RulesFile.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "smart-bridge rules file"
    return schema


@dataclass(frozen=True)
class FileResult:
    path: Path
    file_hash: str
    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_rules_text(text: str) -> tuple[list[Rule], list[str]]:
    """Gemeinsame Pipeline für Dateien und (später) MCP. Gibt nie eine Teilmenge zurück."""
    try:
        data = YAML(typ="safe").load(StringIO(text))
    except YAMLError as exc:
        return [], [f"YAML: {exc}"]
    if not isinstance(data, dict):
        return [], ["top level must be a mapping with key 'rules'"]

    validator = jsonschema.Draft202012Validator(rules_json_schema())
    errors = [
        f"schema: {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.path)))
    ]
    if errors:
        return [], errors
    try:
        return RulesFile.model_validate(data).rules, []
    except ValidationError as exc:
        return [], [
            f"model: {'/'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]


def load_file(path: Path) -> FileResult:
    raw = path.read_bytes()
    rules, errors = parse_rules_text(raw.decode("utf-8"))
    return FileResult(path, hashlib.sha256(raw).hexdigest(), rules, errors)


def load_dir(directory: Path) -> list[FileResult]:
    files = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    return [load_file(f) for f in files]


def duplicate_ids(results: list[FileResult]) -> list[str]:
    seen: dict[str, Path] = {}
    errors: list[str] = []
    for res in results:
        for rule in res.rules:
            if rule.id in seen:
                errors.append(f"duplicate rule id {rule.id!r} in {res.path} and {seen[rule.id]}")
            seen[rule.id] = res.path
    return errors
