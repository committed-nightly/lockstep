"""Command line behaviour, with particular attention to exit codes.

A gate that cannot tell "the rule was broken" from "the check never ran" is
decorative, so each of 0, 1 and 2 gets its own test, and every way of failing
to run at all is pinned to 2.
"""

from __future__ import annotations

import json

from lockstep.cli import EXIT_ERROR, EXIT_OK, EXIT_VIOLATIONS, main

LOCKFILE = "package.json => package-lock.json"


def run(repo, *args):
    return main(["-C", repo.path, *args])


def paired(repo, message="add deps"):
    return repo.commit_files(
        {"package.json": "a\n", "package-lock.json": "a\n"}, message
    )


# --- exit codes -----------------------------------------------------------


def test_clean_exits_0(repo, capsys):
    paired(repo)

    assert run(repo, "--rule", LOCKFILE) == EXIT_OK
    assert "clean" in capsys.readouterr().out


def test_violation_exits_1(repo, capsys):
    paired(repo)
    repo.commit_file("package.json", "a\nb\n", "add a dependency")

    assert run(repo, "--rule", LOCKFILE) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "1 violation" in out
    assert "add a dependency" in out
    assert LOCKFILE in out


def test_no_rules_at_all_exits_2_not_0(repo, capsys):
    """The failure that would make the whole tool a liability: no rules is not
    the same as no violations."""
    paired(repo)

    assert run(repo) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no rules" in err
    assert ".lockstep" in err


def test_missing_rules_file_exits_2(repo, capsys):
    paired(repo)

    assert run(repo, "--rules", "nope.txt") == EXIT_ERROR
    assert "no such rules file" in capsys.readouterr().err


def test_unparseable_rule_exits_2_with_the_line_number(repo, capsys):
    paired(repo)
    repo.write(".lockstep", "package.json => package-lock.json\nnonsense\n")
    repo.commit("add rules")

    assert run(repo) == EXIT_ERROR
    assert ".lockstep:2" in capsys.readouterr().err


def test_rules_file_of_only_comments_exits_2(repo, capsys):
    paired(repo)
    repo.commit_file(".lockstep", "# nothing here yet\n", "add rules")

    assert run(repo) == EXIT_ERROR
    assert "no rules found" in capsys.readouterr().err


def test_not_a_git_repo_exits_2(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert main(["-C", str(plain), "--rule", LOCKFILE]) == EXIT_ERROR
    assert capsys.readouterr().err.strip()


def test_missing_directory_exits_2(capsys):
    assert main(["-C", "/no/such/place", "--rule", LOCKFILE]) == EXIT_ERROR
    assert "no such directory" in capsys.readouterr().err


def test_unknown_revision_exits_2_and_mentions_shallow_clones(repo, capsys):
    paired(repo)

    assert run(repo, "--rule", LOCKFILE, "--since", "origin/nope") == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no such revision" in err
    assert "fetch-depth" in err


# --- rules file discovery -------------------------------------------------


def test_dot_lockstep_is_picked_up_automatically(repo, capsys):
    paired(repo)
    repo.commit_file(".lockstep", LOCKFILE + "\n", "add rules")
    repo.commit_file("package.json", "a\nb\n", "add a dependency")

    assert run(repo) == EXIT_VIOLATIONS
    assert ".lockstep" in capsys.readouterr().out


def test_cli_rules_combine_with_the_file(repo, capsys):
    repo.commit_files({"a.txt": "1\n", "b.txt": "1\n"}, "add")
    repo.commit_file(".lockstep", "a.txt => b.txt\n", "add rules")
    repo.commit_file("a.txt", "2\n", "only a")
    repo.commit_file("b.txt", "2\n", "only b")

    assert run(repo, "--rule", "b.txt => a.txt") == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "2 rules" in out
    assert "2 violations" in out


# --- scope ----------------------------------------------------------------


def test_squash_accepts_what_per_commit_rejects(repo, capsys):
    paired(repo)
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit_file("package.json", "a\nb\n", "add a dependency")
    repo.commit_file("package-lock.json", "a\nb\n", "regenerate")

    assert run(repo, "--rule", LOCKFILE, "--since", base) == EXIT_VIOLATIONS
    assert run(repo, "--rule", LOCKFILE, "--since", base, "--squash") == EXIT_OK
    assert "(squashed)" in capsys.readouterr().out


def test_squashed_output_says_range_not_commit(repo, capsys):
    """Even when a squashed run turns up exactly one culprit commit, what it
    checked was the range, and the message has to say so."""
    repo.commit_file("package.json", "a\n", "add a dependency")

    run(repo, "--rule", LOCKFILE, "--squash")
    out = capsys.readouterr().out

    assert "changed in this range" in out
    assert "in this commit" not in out


def test_per_commit_output_says_commit(repo, capsys):
    repo.commit_file("package.json", "a\n", "add a dependency")

    run(repo, "--rule", LOCKFILE)

    assert "changed in this commit" in capsys.readouterr().out


def test_per_commit_says_when_squash_would_have_passed(repo, capsys):
    paired(repo)
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit_file("package.json", "a\nb\n", "add a dependency")
    repo.commit_file("package-lock.json", "a\nb\n", "regenerate")

    run(repo, "--rule", LOCKFILE, "--since", base)

    assert "--squash would accept it" in capsys.readouterr().out


# --- misconfiguration warnings --------------------------------------------


def test_a_pattern_matching_nothing_warns_on_stderr(repo, capsys):
    paired(repo)

    assert run(repo, "--rule", "src/*.proto => gen/**") == EXIT_OK
    err = capsys.readouterr().err
    assert "src/*.proto" in err
    assert "can never fire" in err


def test_a_matching_rule_warns_about_nothing(repo, capsys):
    paired(repo)

    assert run(repo, "--rule", LOCKFILE) == EXIT_OK
    assert capsys.readouterr().err == ""


# --- output modes ---------------------------------------------------------


def test_list_shows_what_each_side_matches(repo, capsys):
    repo.commit_files({"api/a.proto": "x\n", "api/b.proto": "y\n"}, "add")

    assert run(repo, "--rule", "api/*.proto => gen/**", "--list") == EXIT_OK
    out = capsys.readouterr().out
    assert "2 files tracked" in out
    assert "nothing tracked matches this" in out


def test_list_does_not_run_the_check(repo, capsys):
    """--list is for working out why a rule didn't fire, so it must not itself
    fail on the violation you are trying to understand."""
    repo.commit_file("package.json", "a\n", "add")

    assert run(repo, "--rule", LOCKFILE, "--list") == EXIT_OK


def test_json_output(repo, capsys):
    paired(repo)
    repo.commit_file("package.json", "a\nb\n", "add a dependency")

    assert run(repo, "--rule", LOCKFILE, "--json") == EXIT_VIOLATIONS
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["rules"] == [LOCKFILE]
    violation = payload["violations"][0]
    assert violation["changed"] == ["package.json"]
    assert violation["required"] == "package-lock.json"
    assert violation["commits"][0]["subject"] == "add a dependency"


def test_quiet_prints_nothing_but_still_exits_1(repo, capsys):
    paired(repo)
    repo.commit_file("package.json", "a\nb\n", "add a dependency")

    assert run(repo, "--rule", LOCKFILE, "-q") == EXIT_VIOLATIONS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
