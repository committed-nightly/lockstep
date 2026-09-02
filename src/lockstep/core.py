"""Applying rules to history.

Two scopes, and the difference between them is the only real decision this
tool asks you to make.

**Per commit** (the default) asks each commit on its own: you touched the
schema here, did you touch the migration here? It is the strict reading, and
it is the one that keeps every commit in the history independently correct --
which matters the day you bisect, cherry-pick or revert one of them.

**Squashed** (``--squash``) treats the whole range as a single changeset:
somewhere in this branch you touched the schema, and somewhere in this branch
you touched the migration, so we're square. It is the right question to ask of
a pull request that is going to land as one commit anyway, and it forgives the
extremely normal habit of regenerating in a follow-up commit.

Per commit is the default because a gate that is too strict fails loudly and
gets configured, and a gate that is too loose passes silently and is worth
nothing. When a per-commit run finds violations that ``--squash`` would have
accepted, it says so rather than letting you work it out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .gitlog import Commit, commits_in_range, tracked_paths
from .rules import Rule


@dataclass(frozen=True)
class Violation:
    rule: Rule
    # The commit that broke the rule. In squashed scope, every commit in the
    # range that touched the trigger -- the fault is the range's, not any one
    # commit's, and naming all of them is more use than naming none.
    commits: tuple[Commit, ...]
    changed: tuple[str, ...]  # the trigger paths that actually changed
    answered_in_range: bool = False

    def describe(self) -> str:
        listed = ", ".join(self.changed)
        where = "in this range" if len(self.commits) != 1 else "in this commit"
        return (
            f"changed: {listed}\n"
            f"but nothing matching '{self.rule.required.text}' changed {where}"
        )


@dataclass
class Report:
    rules: list[Rule]
    source: str
    commits_checked: int
    squash: bool
    violations: list[Violation] = field(default_factory=list)
    # Pattern text that matches no tracked file and nothing changed in the
    # range. Almost always a typo; occasionally a rule written ahead of the
    # file it guards. A warning either way, never a failure -- guessing which
    # one it is and being wrong would be worse than saying what we saw.
    unmatched: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def answered_count(self) -> int:
        return sum(1 for v in self.violations if v.answered_in_range)


def _check_per_commit(
    commits: list[Commit], rules: list[Rule], satisfied_in_range: dict[str, bool]
) -> list[Violation]:
    violations: list[Violation] = []
    for commit in commits:
        for rule in rules:
            changed = rule.trigger.select(commit.paths)
            if not changed:
                continue
            if any(rule.required.matches(p) for p in commit.paths):
                continue
            violations.append(
                Violation(
                    rule=rule,
                    commits=(commit,),
                    changed=tuple(changed),
                    answered_in_range=satisfied_in_range[rule.required.text],
                )
            )
    return violations


def _check_squashed(commits: list[Commit], rules: list[Rule]) -> list[Violation]:
    union: list[str] = list(
        dict.fromkeys(path for commit in commits for path in commit.paths)
    )
    violations: list[Violation] = []
    for rule in rules:
        changed = rule.trigger.select(union)
        if not changed:
            continue
        if any(rule.required.matches(p) for p in union):
            continue
        culprits = tuple(
            commit for commit in commits if rule.trigger.select(commit.paths)
        )
        violations.append(
            Violation(rule=rule, commits=culprits, changed=tuple(changed))
        )
    return violations


def check(
    cwd: str,
    rules: list[Rule],
    *,
    source: str,
    since: str | None = None,
    squash: bool = False,
) -> Report:
    """Run every rule over the history and collect what broke."""
    commits = commits_in_range(cwd, since=since)
    touched = {path for commit in commits for path in commit.paths}

    # Precomputed once per required pattern rather than per violation: with a
    # long history and a handful of rules this is the difference between one
    # pass and one pass per offending commit.
    satisfied_in_range = {
        rule.required.text: any(rule.required.matches(p) for p in touched)
        for rule in rules
    }

    if squash:
        violations = _check_squashed(commits, rules)
    else:
        violations = _check_per_commit(commits, rules, satisfied_in_range)

    known = touched | tracked_paths(cwd)
    unmatched: list[str] = []
    for rule in rules:
        for pattern in (rule.trigger, rule.required):
            if pattern.text in unmatched:
                continue
            if not any(pattern.matches(p) for p in known):
                unmatched.append(pattern.text)

    return Report(
        rules=rules,
        source=source,
        commits_checked=len(commits),
        squash=squash,
        violations=violations,
        unmatched=unmatched,
    )
