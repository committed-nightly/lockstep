# lockstep

Check that files which are supposed to change together actually did.

Some files are downstream of others. A lockfile of a manifest, generated
clients of a `.proto`, a migration of a schema, a golden file of the code that
produces it, a docs page of the flag it documents. The rule is always the same
and it is almost never written down anywhere a machine can read: *if you touch
this, you have to touch that.* Nothing enforces it. Somebody edits one side in
a hurry, review looks at the diff and sees a perfectly reasonable change,
because the problem isn't in what's there — it's in what isn't.

`lockstep` reads history and tells you every commit that moved one side without
the other.

```
$ lockstep --since origin/main --squash
.lockstep — 2 rules, 3 commits checked (squashed), 1 violation

  5611fc91d  Sam Okafor  2026-09-02  add message B
    api/**/*.proto => gen/**
      changed: api/a.proto
      but nothing matching 'gen/**' changed in this range
```

Your package manager already does this for its own two files, and only for its
own two files. This is the general version.

## Install

```
pip install git+https://github.com/committed-nightly/lockstep
```

Python 3.10+. No dependencies — it shells out to `git`, which you already have.

## Usage

```
lockstep [--rule RULE] [--rules FILE] [--since REV] [--squash] [--list]
```

Rules live in a `.lockstep` file at the root of the repo, one per line:

```
# The lockfile has to keep up with the manifest. Not the other way round:
# `npm audit fix` touches only the lockfile and that is fine.
package.json => package-lock.json

# Regenerate the client when the schema moves.
api/**/*.proto => gen/**

# These two are genuinely symmetric.
docs/api.md <=> docs/api.example.json
```

Read `A => B` as **if A changed, B must have changed too**. The arrow points at
the thing that has to keep up. Direction is the point: a manifest edited
without its lockfile is a bug, and a lockfile bumped on its own is a Tuesday.
`<=>` is shorthand for a rule each way, for the cases that really are
symmetric.

You can skip the file entirely and pass rules inline, which is what you want
for a one-off:

```
$ lockstep --rule 'package.json => package-lock.json' --since origin/main --squash
```

### Options

| | |
|---|---|
| `--rule RULE` | a rule on the command line. Repeatable, and combines with the rules file. |
| `--rules FILE` | read rules from `FILE` instead of `.lockstep`. |
| `--since REV` | only check commits after `REV`. `--since origin/main` is the fast path for a PR check. |
| `--squash` | treat the whole range as one changeset instead of checking each commit. See below. |
| `--list` | print the rules and what each side currently matches, then exit. |
| `-C DIR` | run as if started in `DIR`. |
| `--json` | machine-readable output. |
| `-q` | print nothing; report through the exit code only. |

Exit codes: **0** clean, **1** a rule was broken, **2** the check could not run.
`2` is deliberately not `0`: a missing rules file, a rules file that's all
comments, a `--since` that doesn't resolve because CI did a shallow clone — all
of those mean the gate did not happen, and a gate that reports clean when it
never ran is worse than no gate at all, because it comes with a tick next to it.

### Globs

Patterns are matched against the whole repo-relative path.

| | |
|---|---|
| `*` | anything within one path segment. Never crosses `/`. |
| `**` | anything, crossing `/`. `docs/**` is everything under `docs`. |
| `**/x` | `x` at any depth, *including* the root. |
| `?` | one character, not `/`. |
| `[abc]`, `[!abc]` | character class, and its negation. |
| `gen/` | trailing slash: the directory and everything in it. |

`Makefile` is the one at the root and nothing else. For every Makefile in the
repo, write `**/Makefile`. When a rule doesn't fire and you can't see why,
`--list` shows what each side matches right now:

```
$ lockstep --list
.lockstep — 2 rules

  package.json => package-lock.json
    package.json                 1 file tracked
    package-lock.json            1 file tracked

  api/**/*.proto => gen/**
    api/**/*.proto               1 file tracked
    gen/**                       nothing tracked matches this
```

## Per commit or squashed

This is the only real decision the tool asks you to make.

**Per commit** — the default — asks each commit on its own: you touched the
schema here, did you touch the migration here? It is the strict reading, and
it's what keeps every commit independently correct, which is the thing you care
about on the day you bisect, revert or cherry-pick one of them.

**Squashed** — `--squash` — treats the range as a single changeset: somewhere
in this branch you touched the schema, and somewhere in this branch you touched
the migration, so you're square. It's the right question to ask of a pull
request that's going to land as one commit anyway, and it forgives the entirely
normal habit of regenerating in a follow-up commit.

Per commit is the default because a gate that's too strict fails loudly and
gets configured, and a gate that's too loose passes silently and is worth
nothing. When a per-commit run finds violations that `--squash` would have
accepted, it says so rather than making you work it out:

```
  2 of 2 are answered elsewhere in the range — --squash would accept them.
```

## In CI

Check the branch, not the whole history. `fetch-depth: 0` is required —
`--since origin/main` cannot resolve in a shallow clone, and `lockstep` says so
and exits 2 rather than passing.

```yaml
name: lockstep
on: pull_request

jobs:
  lockstep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/committed-nightly/lockstep
      - run: |
          git fetch -q origin ${{ github.base_ref }}
          lockstep --since origin/${{ github.base_ref }} --squash
```

## Known limits

Worth reading before you rely on it.

**It checks that the other file changed, not that it changed correctly.** A
rule is satisfied by any edit at all to the required side, including a stray
newline. `lockstep` catches the case where you forgot; it cannot catch the case
where you regenerated from the wrong input. If you can re-run the generator in
CI and diff the result, do that instead — it is a strictly better check. This
is for the very large number of pairs where you can't.

**Merge commits are skipped.** A merge carries no authored change of its own —
`git log --name-status` prints nothing for one — so including them would only
add commits that no rule can fire on. The cost is real: a merge that resolves a
conflict by discarding one side's regenerated file is invisible here. The
commit that originally made the change is still reported, on whichever branch
it was made.

**A rename counts as a change to both names.** `git mv api/v1.proto
api/v2.proto` fires a rule keyed on either one. Moving a file is a change to
where it was as much as to where it is.

**A pattern that matches nothing is a warning, not a failure.** It's usually a
typo, but it's occasionally a rule written the day before the file it guards,
and guessing which and being wrong would be worse than saying what was seen. A
rule whose left side matches nothing can never fire, so read the warnings.

## Licence

MIT.
