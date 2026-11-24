"""`bridge` CLI. M1: validate, schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .rules import duplicate_ids, load_dir, load_file, rules_json_schema
from .rules.loader import FileResult


def _validate(paths: list[Path], all_: bool, config_dir: Path) -> int:
    if all_:
        results: list[FileResult] = load_dir(config_dir / "rules")
    else:
        results = [load_file(p) for p in paths]
    if not results:
        print("no rule files found", file=sys.stderr)
        return 1
    failed = False
    for res in results:
        for err in res.errors:
            print(f"{res.path}: {err}", file=sys.stderr)
        failed |= not res.ok
        if res.ok:
            print(f"{res.path}: ok ({len(res.rules)} rules)")
    for err in duplicate_ids(results):
        print(err, file=sys.stderr)
        failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    val = sub.add_parser("validate", help="validate rule files")
    val.add_argument("files", nargs="*", type=Path)
    val.add_argument("--all", action="store_true", help="validate config/rules/*.yaml")
    val.add_argument("--config-dir", type=Path, default=Path("config"))

    sch = sub.add_parser("schema", help="print the rules JSON Schema")
    sch.add_argument("-o", "--output", type=Path)

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        if not args.all and not args.files:
            parser.error("give files or --all")
        return _validate(args.files, args.all, args.config_dir)
    text = json.dumps(rules_json_schema(), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
