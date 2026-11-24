"""Git-gestützte Regelablage: ein Commit je Änderung, Message = intent."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..rules import parse_rules_text

DELETED = ".deleted"
_AUTHORS = {"claude-mcp": "claude-mcp@smart-bridge.local", "file": "file@smart-bridge.local"}


class RepositoryError(Exception):
    pass


@dataclass(frozen=True)
class RuleVersion:
    version: int
    commit: str
    author: str
    at: str
    message: str
    data: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "commit": self.commit[:12],
            "author": self.author,
            "at": self.at,
            "message": self.message,
        }


def _yaml() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.width = 100
    return y


def dump_rules(items: list[Any]) -> str:
    buf = StringIO()
    _yaml().dump({"rules": items}, buf)
    return buf.getvalue()


def sha256(text: str | bytes) -> str:
    raw = text.encode() if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


class RuleRepository:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self._cache: tuple[str, dict[str, list[RuleVersion]]] | None = None

    # --- git ------------------------------------------------------------------

    def _git(self, *args: str, author: str = "file", check: bool = True) -> str:
        email = _AUTHORS.get(author, f"{author}@smart-bridge.local")
        env = {
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": __import__("os").environ.get("PATH", ""),
        }
        proc = subprocess.run(  # noqa: S603
            ["git", "-c", "commit.gpgsign=false", *args],  # noqa: S607
            cwd=self.dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise RepositoryError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout

    def ensure(self) -> None:
        """Legt das Repo an und übernimmt vorhandene Dateien als ersten Commit (Autor `file`)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / ".git").exists():
            self._git("init", "-q", "-b", "main")
        if not self._head():
            self.commit("initial import", "file", allow_empty=True)

    def _head(self) -> str:
        return self._git("rev-parse", "-q", "--verify", "HEAD", check=False).strip()

    def commit(
        self,
        message: str,
        author: str,
        *,
        paths: list[str] | None = None,
        allow_empty: bool = False,
    ) -> bool:
        """Committet `paths` (oder alles). `False`, wenn nichts zu committen war.

        Mit `paths` bleiben andere, ungeprüfte Dateien (z. B. ungültige Handedits) außen vor.
        """
        scope = ["--", *paths] if paths else []
        self._git("add", "-A", *scope)
        if not allow_empty and not self._git("status", "--porcelain", *scope).strip():
            return False
        args = ["commit", "-q", "--allow-empty-message", "-m", message or "(no intent)"]
        if allow_empty:
            args.append("--allow-empty")
        self._git(*args, *scope, author=author)
        self._cache = None
        return True

    def head_blob(self, name: str) -> str | None:
        out = self._git("show", f"HEAD:{name}", check=False)
        return out or None

    # --- Dateien --------------------------------------------------------------

    def files(self) -> list[Path]:
        return sorted([*self.dir.glob("*.yaml"), *self.dir.glob("*.yml")])

    def locate(self, rule_id: str) -> Path | None:
        for path in self.files():
            rules, _ = parse_rules_text(path.read_text(encoding="utf-8"))
            if any(r.id == rule_id for r in rules):
                return path
        return None

    def read_item(self, rule_id: str) -> Any | None:
        path = self.locate(rule_id)
        if path is None:
            return None
        doc = _yaml().load(path.read_text(encoding="utf-8"))
        return next((i for i in doc["rules"] if i.get("id") == rule_id), None)

    def write_rule(self, rule_id: str, item: Any) -> Path:
        """Ersetzt die Regel in ihrer Datei oder legt `<id>.yaml` an."""
        path = self.locate(rule_id)
        if path is None:
            path = self.dir / f"{rule_id}.yaml"
            path.write_text(dump_rules([item]), encoding="utf-8")
            return path
        doc = _yaml().load(path.read_text(encoding="utf-8"))
        items = doc["rules"]
        for idx, current in enumerate(items):
            if current.get("id") == rule_id:
                items[idx] = item
        buf = StringIO()
        _yaml().dump(doc, buf)
        path.write_text(buf.getvalue(), encoding="utf-8")
        return path

    def soft_delete(self, rule_id: str) -> None:
        """Verschiebt die Regel nach `.deleted/<id>.yaml` (wiederherstellbar)."""
        path = self.locate(rule_id)
        item = self.read_item(rule_id)
        if path is None or item is None:
            raise RepositoryError(f"rule {rule_id!r} not found")
        trash = self.dir / DELETED
        trash.mkdir(exist_ok=True)
        (trash / f"{rule_id}.yaml").write_text(dump_rules([item]), encoding="utf-8")
        doc = _yaml().load(path.read_text(encoding="utf-8"))
        rest = [i for i in doc["rules"] if i.get("id") != rule_id]
        if rest:
            doc["rules"] = rest
            buf = StringIO()
            _yaml().dump(doc, buf)
            path.write_text(buf.getvalue(), encoding="utf-8")
        else:
            path.unlink()

    # --- Versionen --------------------------------------------------------------

    def versions(self, rule_id: str) -> list[RuleVersion]:
        return self._all_versions().get(rule_id, [])

    def _all_versions(self) -> dict[str, list[RuleVersion]]:
        head = self._head()
        if not head:
            return {}
        if self._cache and self._cache[0] == head:
            return self._cache[1]
        result: dict[str, list[RuleVersion]] = {}
        last: dict[str, str | None] = {}
        fmt = "%H%x1f%an%x1f%aI%x1f%s"
        for line in self._git("log", "--reverse", f"--format={fmt}").splitlines():
            commit, author, at, subject = line.split("\x1f", 3)
            names = self._git(
                "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-z", commit
            )
            touched: dict[str, dict[str, Any]] = {}
            for name in filter(None, names.split("\0")):
                if "/" in name or not name.endswith((".yaml", ".yml")):
                    continue
                body = self._git("show", f"{commit}:{name}", check=False)
                rules, errors = parse_rules_text(body) if body else ([], ["missing"])
                if errors:
                    continue
                for rule in rules:
                    touched[rule.id] = rule.model_dump(
                        by_alias=True, exclude_none=True, mode="json"
                    )
            for rid, data in touched.items():
                key = json.dumps({k: v for k, v in data.items() if k != "version"}, sort_keys=True)
                if last.get(rid) == key:
                    continue
                last[rid] = key
                n = len(result.setdefault(rid, [])) + 1
                result[rid].append(RuleVersion(n, commit, author, at, subject, data))
        self._cache = (head, result)
        return result

    def version_text(self, rule_id: str, version: int) -> str:
        for entry in self.versions(rule_id):
            if entry.version == version:
                return dump_rules([entry.data])
        raise RepositoryError(f"rule {rule_id!r} has no version {version}")

    def deleted_ids(self) -> list[str]:
        trash = self.dir / DELETED
        return sorted(p.stem for p in trash.glob("*.yaml")) if trash.is_dir() else []

    def read_deleted(self, rule_id: str) -> str | None:
        path = self.dir / DELETED / f"{rule_id}.yaml"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def purge_deleted(self, rule_id: str) -> None:
        (self.dir / DELETED / f"{rule_id}.yaml").unlink(missing_ok=True)
