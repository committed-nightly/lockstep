"""The rule language and the glob matcher.

This is the file that matters most. Every other kind of bug in this tool makes
it report the wrong commit; a bug here makes a rule quietly match nothing, and
a rule that matches nothing is a green tick on a check that never ran.
"""

from __future__ import annotations

import pytest

from lockstep.rules import RuleError, compile_pattern, parse_rule, parse_rules


def glob(text: str):
    return compile_pattern(text, source="test")


# --- glob semantics -------------------------------------------------------


def test_star_does_not_cross_a_slash():
    pattern = glob("src/*.py")
    assert pattern.matches("src/cli.py")
    assert not pattern.matches("src/lockstep/cli.py")


def test_double_star_crosses_slashes():
    pattern = glob("src/**")
    assert pattern.matches("src/cli.py")
    assert pattern.matches("src/lockstep/deep/cli.py")


def test_double_star_slash_also_matches_nothing_at_all():
    """`**/x` has to match a bare `x` at the root, or every rule using it is
    subtly wrong for the top level of the repo."""
    pattern = glob("**/Makefile")
    assert pattern.matches("Makefile")
    assert pattern.matches("sub/Makefile")
    assert pattern.matches("a/b/c/Makefile")


def test_plain_name_is_rooted():
    pattern = glob("Makefile")
    assert pattern.matches("Makefile")
    assert not pattern.matches("sub/Makefile")


def test_trailing_slash_means_everything_underneath():
    pattern = glob("gen/")
    assert pattern.matches("gen/client.ts")
    assert pattern.matches("gen/a/b.ts")
    assert not pattern.matches("generated/x.ts")
    assert not pattern.matches("gen")


def test_leading_slash_is_stripped():
    assert glob("/package.json").matches("package.json")


def test_question_mark_is_one_non_slash_character():
    pattern = glob("v?/api.json")
    assert pattern.matches("v1/api.json")
    assert not pattern.matches("v10/api.json")
    assert not pattern.matches("v/api.json")


def test_character_class_and_negation():
    assert glob("v[12]/api.json").matches("v2/api.json")
    assert not glob("v[12]/api.json").matches("v3/api.json")
    assert glob("v[!12]/api.json").matches("v3/api.json")
    assert not glob("v[!12]/api.json").matches("v1/api.json")


def test_regex_metacharacters_are_literals():
    """A dot in a filename is a dot, not 'any character'."""
    pattern = glob("a.lock")
    assert pattern.matches("a.lock")
    assert not pattern.matches("axlock")
    assert glob("a+b.txt").matches("a+b.txt")
    assert not glob("a+b.txt").matches("ab.txt")


def test_pattern_must_match_the_whole_path():
    pattern = glob("package.json")
    assert not pattern.matches("vendor/package.json")
    assert not pattern.matches("package.json.bak")


def test_unclosed_bracket_is_an_error_not_a_silent_literal():
    with pytest.raises(RuleError):
        glob("v[12/api.json")


def test_empty_pattern_is_an_error():
    with pytest.raises(RuleError):
        glob("   ")
    with pytest.raises(RuleError):
        glob("/")


# --- rule parsing ---------------------------------------------------------


def test_directed_rule():
    (rule,) = parse_rule("package.json => package-lock.json", source="test")
    assert rule.trigger.text == "package.json"
    assert rule.required.text == "package-lock.json"
    assert rule.label == "package.json => package-lock.json"


def test_symmetric_rule_becomes_two_directed_rules():
    rules = parse_rule("a.txt <=> b.txt", source="test")
    assert [r.label for r in rules] == ["a.txt => b.txt", "b.txt => a.txt"]


def test_symmetric_arrow_wins_over_the_directed_one():
    """'<=>' contains '=>'; splitting on the wrong one leaves a stray '<'."""
    rules = parse_rule("a.txt <=> b.txt", source="test")
    assert rules[0].trigger.text == "a.txt"


def test_comments_and_blank_lines_are_skipped():
    rules = parse_rules(
        "# lockfiles\n\npackage.json => package-lock.json  # keep up\n",
        source=".lockstep",
    )
    assert len(rules) == 1
    assert rules[0].required.text == "package-lock.json"


def test_missing_arrow_reports_the_line_number():
    with pytest.raises(RuleError) as excinfo:
        parse_rules("a => b\nc and d\n", source=".lockstep")
    assert excinfo.value.lineno == 2
    assert excinfo.value.where() == ".lockstep:2"


def test_two_arrows_on_one_line_is_an_error():
    with pytest.raises(RuleError):
        parse_rule("a => b => c", source="test")


def test_empty_side_is_an_error():
    with pytest.raises(RuleError):
        parse_rule("package.json =>", source="test")
    with pytest.raises(RuleError):
        parse_rule("=> package-lock.json", source="test")
