from __future__ import annotations

import subprocess

import pytest


class Repo:
    """A throwaway git repo you can drive from a test."""

    def __init__(self, path):
        self.path = str(path)

    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            check=check,
            text=True,
        )
        return proc.stdout

    def write(self, name: str, content: str) -> None:
        path = f"{self.path}/{name}"
        parent = path.rsplit("/", 1)[0]
        subprocess.run(["mkdir", "-p", parent], check=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def commit_files(self, files: dict[str, str], message: str) -> str:
        """Write several files and land them in one commit."""
        for name, content in files.items():
            self.write(name, content)
        return self.commit(message)

    def commit_file(self, name: str, content: str, message: str) -> str:
        return self.commit_files({name: content}, message)


@pytest.fixture
def repo(tmp_path) -> Repo:
    path = tmp_path / "repo"
    path.mkdir()
    r = Repo(path)
    r.git("init", "-q", "-b", "main")
    r.git("config", "user.name", "Test Person")
    r.git("config", "user.email", "test@example.invalid")
    r.git("config", "commit.gpgsign", "false")
    return r
