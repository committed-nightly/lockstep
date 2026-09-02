"""Thin wrappers over the ``git`` binary.

Everything here shells out. There is no libgit2, no GitPython, no vendored
object parser -- the whole point of this tool is that it agrees with what
``git log`` says about which files a commit touched, so it asks git.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

# Separators for our --format string. They are written as git's %xNN escapes
# rather than as literal control characters, because an argv element cannot
# contain a NUL byte -- passing "\x00" here fails before git even starts.
_REC = "\x01"
_FLD = "\x00"

_FORMAT = "%x01" + "%x00".join(["%H", "%P", "%an", "%aI", "%s"])


class GitError(RuntimeError):
    """git was missing, unhappy, or pointed at something that isn't a repo."""


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author: str
    date: str  # ISO 8601, as git formatted it
    subject: str
    # Every repo-relative path this commit touched. A rename contributes both
    # names: moving a file is a change to the old location as much as the new
    # one, and a rule keyed on either side should notice.
    paths: tuple[str, ...] = field(default=())

    @property
    def short(self) -> str:
        return self.sha[:9]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def run_git(args: list[str], cwd: str) -> str:
    """Run git and return stdout, or raise GitError."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - depends on the box
        raise GitError("git is not on PATH") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return proc.stdout.decode("utf-8", "replace")


def repo_root(cwd: str) -> str:
    """Absolute path of the working tree containing ``cwd``."""
    return run_git(["rev-parse", "--show-toplevel"], cwd).strip()


def resolve(rev: str, cwd: str) -> str:
    """Resolve a revision to a full sha, raising GitError if it is unknown."""
    return run_git(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], cwd).strip()


def tracked_paths(cwd: str) -> set[str]:
    """Every path in the current index, repo-relative.

    Used only to tell a rule that hasn't fired yet from one that names a file
    that does not exist -- never to decide whether a rule was satisfied.
    """
    out = run_git(["-c", "core.quotePath=false", "ls-files", "-z"], cwd)
    return {p for p in out.split("\0") if p}


def _parse_paths(lines: list[str]) -> tuple[str, ...]:
    """Pull the paths out of ``--name-status`` output for one commit."""
    paths: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in {"R", "C"} and len(parts) >= 3:
            # R  old  new. Both names go in: for a rename the old path really
            # did change (it stopped existing), and for a copy the source is
            # listed by git as touched too.
            paths.extend(parts[1:3])
        else:
            paths.append(parts[1])
    # dict.fromkeys rather than set(), so the order git reported is preserved
    # and the human output reads in the same order as `git show --stat`.
    return tuple(dict.fromkeys(paths))


def commits_in_range(cwd: str, *, since: str | None = None) -> list[Commit]:
    """Commits reachable from HEAD, newest first, with the paths they touched.

    Merges are excluded. A merge commit introduces no authored
    change of its own -- ``--name-status`` prints nothing for one unless you
    ask for a specific parent -- so including them would only ever add commits
    that touch no files, which no rule can fire on. The cost of that choice is
    real and is in the README: a merge that resolves a conflict by throwing
    away one side's regenerated file is invisible to this tool.
    """
    args = [
        # Without this git escapes non-ASCII paths into C-style quoted strings
        # and our tab split stops lining up with reality.
        "-c",
        "core.quotePath=false",
        "log",
        f"--format={_FORMAT}",
        "--name-status",
        "--no-merges",
    ]
    if since:
        args.append(f"{since}..HEAD")

    out = run_git(args, cwd)
    commits: list[Commit] = []
    for record in out.split(_REC):
        if not record.strip():
            continue
        head, _, rest = record.partition("\n")
        fields = head.split(_FLD)
        if len(fields) < 5:
            continue
        sha, parents, author, date, subject = fields[:5]
        commits.append(
            Commit(
                sha=sha,
                parents=tuple(p for p in parents.split() if p),
                author=author,
                date=date,
                subject=subject,
                paths=_parse_paths(rest.splitlines()),
            )
        )
    return commits
