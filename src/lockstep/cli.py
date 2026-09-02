"""Command line entry point.

Exit codes matter here, because the main use for this tool is as a CI gate:

    0  every rule held
    1  at least one rule was broken
    2  the tool could not run the check at all

2 is deliberately not 1, and it is very deliberately not 0. A rules file that
isn't there, a typo in a glob that makes a rule match nothing, a revision that
doesn't resolve because CI did a shallow clone -- all of those mean the gate
did not happen, and a gate that reports "clean" when it never ran is worse
than no gate, because it comes with a tick next to it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .core import Report, check
from .gitlog import GitError, repo_root, resolve, tracked_paths
from .rules import DIRECTED, Rule, RuleError, parse_rule, parse_rules

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

DEFAULT_RULES_FILE = ".lockstep"
CLI_SOURCE = "command line"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lockstep",
        description=(
            "Check that files which are supposed to change together "
            "actually did."
        ),
        epilog=(
            "Rules look like 'package.json => package-lock.json': if the left "
            "side changed, the right side must have changed too. "
            "Exit codes: 0 clean, 1 rules broken, 2 the check could not run."
        ),
    )
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="RULE",
        help=(
            f"a rule, e.g. --rule 'package.json {DIRECTED} package-lock.json'. "
            "Repeatable, and combines with the rules file"
        ),
    )
    parser.add_argument(
        "--rules",
        metavar="FILE",
        help=(
            f"read rules from FILE (default: {DEFAULT_RULES_FILE} at the repo "
            "root, if it exists)"
        ),
    )
    parser.add_argument(
        "--since",
        metavar="REV",
        help="only check commits after REV, e.g. --since origin/main",
    )
    parser.add_argument(
        "--squash",
        action="store_true",
        help=(
            "treat the whole range as one changeset instead of checking each "
            "commit: the right side may be touched in a different commit from "
            "the left. What you want for a pull request that lands squashed"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=(
            "print the parsed rules and what each side currently matches, then "
            "exit. Run this the first time a rule doesn't fire"
        ),
    )
    parser.add_argument(
        "-C",
        dest="directory",
        default=".",
        metavar="DIR",
        help="run as if started in DIR (default: .)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print nothing; report only through the exit code",
    )
    return parser


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _collect_rules(args, root: str) -> tuple[list[Rule], list[str]]:
    """Gather rules from the file and the command line, or raise RuleError.

    Returns the rules and the places they were read from. The sources are
    returned even when they yielded nothing, because "the rules file is all
    comments" and "there is no rules file" need different messages and the
    caller cannot tell them apart from an empty rule list.
    """
    rules: list[Rule] = []
    sources: list[str] = []

    label = args.rules
    if label is not None:
        # A relative --rules is relative to -C, so that `lockstep -C repo` and
        # `cd repo && lockstep` mean the same thing.
        path = (
            label if os.path.isabs(label) else os.path.join(args.directory, label)
        )
    else:
        candidate = os.path.join(root, DEFAULT_RULES_FILE)
        path = candidate if os.path.isfile(candidate) else None
        label = DEFAULT_RULES_FILE

    if path is not None:
        if not os.path.isfile(path):
            raise RuleError("no such rules file", source=label)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        rules.extend(parse_rules(text, source=label))
        sources.append(label)

    for raw in args.rule:
        parsed = parse_rule(raw, source=CLI_SOURCE)
        if not parsed:
            raise RuleError(f"empty rule {raw!r}", source=CLI_SOURCE)
        rules.extend(parsed)
    if args.rule:
        sources.append(CLI_SOURCE)

    return rules, sources


def _no_rules_message(sources: list[str], root: str) -> list[str]:
    if sources:
        return [
            f"lockstep: no rules found in {' + '.join(sources)} — every line "
            "was blank or a comment.",
            "  Nothing was checked.",
        ]
    return [
        f"lockstep: no rules. Expected {DEFAULT_RULES_FILE} at {root}, and no "
        "--rule was given.",
        "  Nothing was checked. A rules file is one rule per line:",
        f"    package.json {DIRECTED} package-lock.json",
        "  Read it as: if the left side changed, the right side must have too.",
    ]


def _print_list(rules: list[Rule], source: str, root: str, out) -> None:
    tracked = tracked_paths(root)
    print(f"{source} — {_plural(len(rules), 'rule')}", file=out)
    for rule in rules:
        print(file=out)
        print(f"  {rule.label}", file=out)
        for pattern in (rule.trigger, rule.required):
            hits = len(pattern.select(tracked))
            summary = (
                f"{_plural(hits, 'file')} tracked"
                if hits
                else "nothing tracked matches this"
            )
            print(f"    {pattern.text:<28} {summary}", file=out)


def _print_human(report: Report, out) -> None:
    count = len(report.violations)
    summary = "clean" if report.ok else _plural(count, "violation")
    scope = " (squashed)" if report.squash else ""
    print(
        f"{report.source} — {_plural(len(report.rules), 'rule')}, "
        f"{_plural(report.commits_checked, 'commit')} checked{scope}, {summary}",
        file=out,
    )

    for violation in report.violations:
        print(file=out)
        for commit in violation.commits:
            print(
                f"  {commit.short}  {commit.author}  {commit.date[:10]}  "
                f"{commit.subject}",
                file=out,
            )
        print(f"    {violation.rule.label}", file=out)
        for line in violation.describe().splitlines():
            print(f"      {line}", file=out)

    answered = report.answered_count
    if answered:
        was = "is" if answered == 1 else "are"
        it = "it" if answered == 1 else "them"
        print(file=out)
        print(
            f"  {answered} of {count} {was} answered elsewhere in the range — "
            f"--squash would accept {it}.",
            file=out,
        )


def _report_to_dict(report: Report) -> dict:
    return {
        "source": report.source,
        "squash": report.squash,
        "commits_checked": report.commits_checked,
        "rules": [r.label for r in report.rules],
        "unmatched_patterns": report.unmatched,
        "ok": report.ok,
        "violations": [
            {
                "rule": v.rule.label,
                "trigger": v.rule.trigger.text,
                "required": v.rule.required.text,
                "changed": list(v.changed),
                "answered_in_range": v.answered_in_range,
                "commits": [
                    {
                        "commit": c.sha,
                        "author": c.author,
                        "date": c.date,
                        "subject": c.subject,
                    }
                    for c in v.commits
                ],
            }
            for v in report.violations
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    out = sys.stdout

    if not os.path.isdir(args.directory):
        print(f"lockstep: no such directory: {args.directory}", file=sys.stderr)
        return EXIT_ERROR

    try:
        root = repo_root(args.directory)
    except GitError as exc:
        print(f"lockstep: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.since:
        try:
            resolve(args.since, args.directory)
        except GitError:
            print(
                f"lockstep: --since {args.since}: no such revision in this "
                "repository.",
                file=sys.stderr,
            )
            print(
                "  On CI this is usually a shallow clone — actions/checkout "
                "needs fetch-depth: 0 before origin/main resolves.",
                file=sys.stderr,
            )
            return EXIT_ERROR

    try:
        rules, sources = _collect_rules(args, root)
    except RuleError as exc:
        print(f"lockstep: {exc.where()}: {exc.message}", file=sys.stderr)
        return EXIT_ERROR

    if not rules:
        for line in _no_rules_message(sources, root):
            print(line, file=sys.stderr)
        return EXIT_ERROR

    source = " + ".join(sources)

    if args.list:
        if not args.quiet:
            _print_list(rules, source, root, out)
        return EXIT_OK

    try:
        report = check(
            args.directory,
            rules,
            source=source,
            since=args.since,
            squash=args.squash,
        )
    except GitError as exc:
        print(f"lockstep: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not args.quiet:
        for pattern in report.unmatched:
            print(
                f"lockstep: warning: '{pattern}' matches no tracked file and "
                "nothing that changed in this range.",
                file=sys.stderr,
            )
        if report.unmatched:
            print(
                "  A rule whose left side matches nothing can never fire. Check "
                "the glob with --list.",
                file=sys.stderr,
            )

        if args.json:
            print(json.dumps(_report_to_dict(report), indent=2), file=out)
        else:
            _print_human(report, out)

    return EXIT_OK if report.ok else EXIT_VIOLATIONS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
