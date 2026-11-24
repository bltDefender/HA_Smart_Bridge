"""Sandboxed Jinja2 für Regeln: Conditions, Alert-Texte, Aktionsparameter.

Verfügbar: `states(id)`, `is_state(id, wert|liste)`, `state_attr(id, attr)`, `has_value(id)`,
`now()` (lokal, tz-aware) plus die übergebenen Variablen (`entity_id`, `trigger`, ...).
Das Modul kennt weder Regeln noch HA und ist ohne Netzwerk testbar.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import StrictUndefined, TemplateSyntaxError, nodes
from jinja2.sandbox import SandboxedEnvironment

from .state import EntityState

UNAVAILABLE = ("unavailable", "unknown")
_ENTITY_FUNCS = {"states", "is_state", "state_attr", "has_value"}
_BARE = re.compile(r"^\s*\{\{(?P<expr>(?:(?!\}\}).)*)\}\}\s*$", re.DOTALL)
_ENTITY_VAR = re.compile(r"\{\{\s*entity_id\s*\}\}")

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


class TemplateError(ValueError):
    pass


def is_template(text: str) -> bool:
    return "{{" in text or "{%" in text


def check_syntax(text: str) -> None:
    """Für die Regel-Validierung: wirft `ValueError` bei Syntaxfehlern."""
    try:
        _env.parse(text)
    except TemplateSyntaxError as exc:
        raise ValueError(f"invalid template: {exc.message} (line {exc.lineno})") from exc


def bind_entity(value: Any, entity_id: str) -> Any:
    """`{{ entity_id }}` in allen Strings statisch ersetzen (für `per_entity`)."""
    if isinstance(value, str):
        return _ENTITY_VAR.sub(entity_id, value)
    if isinstance(value, list):
        return [bind_entity(v, entity_id) for v in value]
    if isinstance(value, dict):
        return {k: bind_entity(v, entity_id) for k, v in value.items()}
    return value


def referenced_entities(
    text: str, variables: Mapping[str, Any] | None = None
) -> tuple[set[str], bool]:
    """Entities, die `states()`/`is_state()`/... mit konstantem (oder gebundenem) Argument lesen.

    Zweiter Wert: `True`, wenn es Zugriffe mit nicht bestimmbarer Entity gibt.
    """
    variables = variables or {}
    found: set[str] = set()
    dynamic = False
    for call in _env.parse(text).find_all(nodes.Call):
        if not (isinstance(call.node, nodes.Name) and call.node.name in _ENTITY_FUNCS):
            continue
        arg = call.args[0] if call.args else None
        if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
            found.add(arg.value)
        elif isinstance(arg, nodes.Name) and isinstance(variables.get(arg.name), str):
            found.add(variables[arg.name])
        else:
            dynamic = True
    return found, dynamic


class Templater:
    def __init__(
        self,
        states: Callable[[str], EntityState | None],
        now: Callable[[], datetime],
        zone: ZoneInfo,
    ) -> None:
        self._get, self._now, self._zone = states, now, zone

    def _globals(self) -> dict[str, Any]:
        def state_of(entity_id: str) -> str:
            st = self._get(entity_id)
            return st.state if st else "unknown"

        def is_state(entity_id: str, wanted: str | list[str]) -> bool:
            options = [wanted] if isinstance(wanted, str) else wanted
            return state_of(entity_id) in options

        def state_attr(entity_id: str, attr: str) -> Any:
            st = self._get(entity_id)
            return st.attributes.get(attr) if st else None

        def has_value(entity_id: str) -> bool:
            return state_of(entity_id) not in UNAVAILABLE

        return {
            "states": state_of,
            "is_state": is_state,
            "state_attr": state_attr,
            "has_value": has_value,
            "now": lambda: self._now().astimezone(self._zone),
        }

    def render(self, text: str, variables: Mapping[str, Any] | None = None) -> str:
        try:
            template = _env.from_string(text)
            return template.render({**self._globals(), **(variables or {})}).strip()
        except Exception as exc:  # Syntax-, Undefined- und Laufzeitfehler
            raise TemplateError(f"{type(exc).__name__}: {exc}") from exc

    def render_value(self, text: str, variables: Mapping[str, Any] | None = None) -> Any:
        """Wie `render`; ein einzelner `{{ ausdruck }}` liefert Zahl/Bool/Liste statt String."""
        out = self.render(text, variables)
        if _BARE.match(text):
            try:
                return ast.literal_eval(out)
            except (ValueError, SyntaxError):
                return out
        return out

    def render_deep(self, value: Any, variables: Mapping[str, Any] | None = None) -> Any:
        if isinstance(value, str):
            return self.render_value(value, variables) if is_template(value) else value
        if isinstance(value, list):
            return [self.render_deep(v, variables) for v in value]
        if isinstance(value, dict):
            return {k: self.render_deep(v, variables) for k, v in value.items()}
        return value
