"""Checking rules against real git history.

Every test here builds an actual repo and makes actual commits, because the
thing being tested is agreement with git -- what git thinks a rename is, what
it prints for a delete, which commits it puts in a range.
"""

from __future__ import annotations

from lockstep.core import check
from lockstep.rules import parse_rule

LOCKFILE = "package.json => package-lock.json"


def run(repo, *rule_texts, **kwargs):
    rules = []
    for text in rule_texts:
        rules.extend(parse_rule(text, source="test"))
    return check(repo.path, rules, source="test", **kwargs)


# --- the basic direction --------------------------------------------------


def test_both_sides_in_one_commit_is_clean(repo):
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    report = run(repo, LOCKFILE)

    assert report.ok
    assert report.commits_checked == 1


def test_trigger_alone_is_a_violation(repo):
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    repo.commit_file("package.json", "a\nb\n", "add a dependency")

    report = run(repo, LOCKFILE)

    assert not report.ok
    assert len(report.violations) == 1
    violation = report.violations[0]
    assert violation.changed == ("package.json",)
    assert violation.commits[0].subject == "add a dependency"


def test_required_alone_is_fine(repo):
    """Rules point one way on purpose. A lockfile bumped by itself is an
    `npm audit fix`, not a mistake."""
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    repo.commit_file("package-lock.json", "a\nb\n", "audit fix")

    assert run(repo, LOCKFILE).ok


def test_symmetric_rule_fires_both_ways(repo):
    repo.commit_files({"a.txt": "1\n", "b.txt": "1\n"}, "add both")
    repo.commit_file("a.txt", "2\n", "only a")
    repo.commit_file("b.txt", "2\n", "only b")

    report = run(repo, "a.txt <=> b.txt")

    assert len(report.violations) == 2
    assert {v.commits[0].subject for v in report.violations} == {
        "only a",
        "only b",
    }


def test_unrelated_commits_do_not_fire(repo):
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    repo.commit_file("README.md", "hello\n", "docs")

    assert run(repo, LOCKFILE).ok


# --- per commit vs squashed -----------------------------------------------


def test_split_across_commits_fails_per_commit_and_passes_squashed(repo):
    """The disagreement between the two scopes, in one repo."""
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit_file("package.json", "a\nb\n", "add a dependency")
    repo.commit_file("package-lock.json", "a\nb\n", "regenerate the lockfile")

    strict = run(repo, LOCKFILE, since=base)
    assert not strict.ok
    assert strict.violations[0].answered_in_range is True

    loose = run(repo, LOCKFILE, since=base, squash=True)
    assert loose.ok


def test_unanswered_violation_is_not_marked_answered(repo):
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit_file("package.json", "a\nb\n", "add a dependency")

    report = run(repo, LOCKFILE, since=base)

    assert report.violations[0].answered_in_range is False
    assert report.answered_count == 0


def test_squashed_names_every_commit_that_touched_the_trigger(repo):
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit_file("package.json", "a\nb\n", "first edit")
    repo.commit_file("package.json", "a\nb\nc\n", "second edit")

    report = run(repo, LOCKFILE, since=base, squash=True)

    assert len(report.violations) == 1
    subjects = {c.subject for c in report.violations[0].commits}
    assert subjects == {"first edit", "second edit"}


def test_squashed_reports_one_violation_per_rule_not_per_commit(repo):
    repo.commit_file("package.json", "a\n", "one")
    repo.commit_file("package.json", "b\n", "two")

    per_commit = run(repo, LOCKFILE)
    squashed = run(repo, LOCKFILE, squash=True)

    assert len(per_commit.violations) == 2
    assert len(squashed.violations) == 1


# --- what counts as a change ----------------------------------------------


def test_deleting_the_trigger_fires_the_rule(repo):
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    repo.git("rm", "-q", "package.json")
    repo.commit("drop the manifest")

    report = run(repo, LOCKFILE)

    assert not report.ok
    assert report.violations[0].commits[0].subject == "drop the manifest"


def test_a_rename_counts_as_a_change_to_both_names(repo):
    """Moving a file is a change to where it was as much as to where it is."""
    repo.commit_files({"api/v1.proto": "x\n", "gen/v1.ts": "x\n"}, "add api")
    repo.git("mv", "api/v1.proto", "api/v2.proto")
    repo.commit("rename the schema")

    report = run(repo, "api/*.proto => gen/**")

    assert not report.ok
    assert set(report.violations[0].changed) == {"api/v1.proto", "api/v2.proto"}


def test_glob_rule_over_a_directory(repo):
    repo.commit_files(
        {"api/a.proto": "x\n", "gen/a.ts": "x\n", "gen/b.ts": "y\n"}, "add api"
    )
    repo.commit_files({"api/b.proto": "y\n", "gen/b.ts": "z\n"}, "extend and gen")
    repo.commit_file("api/c.proto", "z\n", "extend without gen")

    report = run(repo, "api/**/*.proto => gen/**")

    assert len(report.violations) == 1
    assert report.violations[0].commits[0].subject == "extend without gen"


def test_a_merge_commit_does_not_fire_a_rule(repo):
    """Merges carry no authored change of their own, so they are skipped.

    Without this, every merge of a branch that touched only one side would be
    reported a second time, on top of the commit that actually did it.
    """
    repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, "add deps"
    )
    repo.git("checkout", "-q", "-b", "side")
    repo.commit_files(
        {"package.json": "a\nb\n", "package-lock.json": "a\nb\n"}, "bump on side"
    )
    repo.git("checkout", "-q", "main")
    repo.commit_file("README.md", "hi\n", "docs on main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")

    report = run(repo, LOCKFILE)

    assert report.ok
    assert report.commits_checked == 3  # the merge itself is not one of them


# --- range selection ------------------------------------------------------


def test_since_excludes_earlier_commits(repo):
    repo.commit_file("package.json", "a\n", "sin of the past")
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit_file("README.md", "hi\n", "docs")

    assert not run(repo, LOCKFILE).ok
    assert run(repo, LOCKFILE, since=base).ok


def test_an_empty_range_is_clean(repo):
    """Nothing to check is a pass. It says '0 commits checked' so a human can
    tell it apart from a check that found nothing wrong in real work."""
    repo.commit_file("package.json", "a\n", "add")
    head = repo.git("rev-parse", "HEAD").strip()

    report = run(repo, LOCKFILE, since=head)

    assert report.ok
    assert report.commits_checked == 0


# --- misconfiguration detection -------------------------------------------


def test_a_pattern_matching_nothing_is_reported(repo):
    repo.commit_file("package.json", "a\n", "add")

    report = run(repo, "package.json => yarn.lock")

    assert "yarn.lock" in report.unmatched
    assert "package.json" not in report.unmatched


def test_a_pattern_is_matched_against_history_as_well_as_the_tree(repo):
    """A file that existed and was deleted still counts as known: a rule that
    guards it is stale, not misspelt, and saying 'matches nothing' would be a
    lie about the range we just walked."""
    repo.commit_files({"old.txt": "a\n", "new.txt": "a\n"}, "add")
    repo.git("rm", "-q", "old.txt")
    repo.commit("delete old")

    report = run(repo, "new.txt => old.txt")

    assert report.unmatched == []
