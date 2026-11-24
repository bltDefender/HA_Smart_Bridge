"""`bridge` CLI: validate, schema, pending, approve, reject."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .approvals import ApprovalStore, Decision
from .audit import AuditLog
from .clock import utc_now
from .policy import Policy
from .rules import duplicate_ids, load_dir, load_file, rules_json_schema
from .rules.loader import FileResult
from .rules.models import Rule


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


def _approval_context(args: argparse.Namespace) -> tuple[Policy, ApprovalStore, AuditLog]:
    audit = AuditLog(args.data_dir / "bridge.db", utc_now)
    approvals = ApprovalStore(
        args.config_dir / "approvals", args.data_dir / "bridge.db", b"cli", audit
    )
    return Policy.load(args.config_dir / "policy.yaml"), approvals, audit


def _tier2_rules(config_dir: Path, policy: Policy) -> dict[str, Rule]:
    results = load_dir(config_dir / "rules")
    return {r.id: r for res in results for r in res.rules if policy.tier_of(r) == 2}


def _pending(args: argparse.Namespace) -> int:
    policy, approvals, _ = _approval_context(args)
    waiting = [
        r for r in _tier2_rules(args.config_dir, policy).values() if not approvals.decision_for(r)
    ]
    for rule in waiting:
        print(f"{rule.id}\t{rule.title or ''}")
    if not waiting:
        print("no rules waiting for approval")
    return 0


def _decide(args: argparse.Namespace, decision: Decision) -> int:
    policy, approvals, audit = _approval_context(args)
    try:
        rules = _tier2_rules(args.config_dir, policy)
        rule = rules.get(args.rule_id)
        if rule is None:
            print(f"{args.rule_id}: not found or not a tier-2 rule", file=sys.stderr)
            return 1
        approvals.decide(rule, decision, "cli")
        print(f"{args.rule_id}: {decision}")
        return 0
    finally:
        approvals.close()
        audit.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    val = sub.add_parser("validate", help="validate rule files")
    val.add_argument("files", nargs="*", type=Path)
    val.add_argument("--all", action="store_true", help="validate config/rules/*.yaml")
    val.add_argument("--config-dir", type=Path, default=Path("config"))

    sch = sub.add_parser("schema", help="print the rules JSON Schema")
    sch.add_argument("-o", "--output", type=Path)

    sub.add_parser("pending", help="list tier-2 rules waiting for approval")
    for name, text in (("approve", "approve a tier-2 rule"), ("reject", "reject a tier-2 rule")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("rule_id")
    for cmd in (sub.choices["pending"], sub.choices["approve"], sub.choices["reject"]):
        cmd.add_argument("--config-dir", type=Path, default=Path("config"))
        cmd.add_argument("--data-dir", type=Path, default=Path("data"))

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        if not args.all and not args.files:
            parser.error("give files or --all")
        return _validate(args.files, args.all, args.config_dir)
    if args.cmd == "pending":
        return _pending(args)
    if args.cmd in ("approve", "reject"):
        return _decide(args, "approved" if args.cmd == "approve" else "rejected")
    text = json.dumps(rules_json_schema(), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
