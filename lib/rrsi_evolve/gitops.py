# Adapted from google-research/rrsi rrsi/gitops.py (Copyright 2026 Google LLC, Apache-2.0).
"""Git plumbing for candidate isolation.

Each domain evolves on its own branch `evolve/<domain>`. Every candidate
harness of a round is drafted and evaluated in its own git worktree checked out
on a branch `<domain>/r<t><variant>` from the incumbent, so variants cannot see
each other's edits and can be evaluated concurrently (Alg. 2, "in parallel").
Accepting a candidate fast-forwards `evolve/<domain>` to its commit. The
harness tree hash (`git rev-parse <commit>:<harness path>`) is what the
frontier records, so commits outside the harness never look like a change.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

AUTHOR = ["-c", "user.email=rrsi@localhost", "-c", "user.name=rrsi"]


def sh(cmd: list[str], cwd: Path | str, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed in {cwd}: {r.stderr[-800:]}")
    return r


def git(cwd: Path | str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return sh(["git", *AUTHOR, *args], cwd, check=check)


def head(cwd) -> str:
    return git(cwd, "rev-parse", "--short", "HEAD").stdout.strip()


def rev(cwd, ref: str) -> str:
    return git(cwd, "rev-parse", "--short", ref).stdout.strip()


def tree_hash(cwd, ref: str, path: str) -> str:
    return git(cwd, "rev-parse", f"{ref}:{path}").stdout.strip()[:12]


def branch_exists(cwd, name: str) -> bool:
    return git(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0


def ensure_branch(cwd, name: str, start: str = "HEAD") -> None:
    if not branch_exists(cwd, name):
        git(cwd, "branch", name, start, check=True)


def worktree_add(repo: Path, path: Path, branch: str, start: str) -> Path:
    """Fresh worktree on a NEW branch `branch` at `start` (old one removed)."""
    worktree_remove(repo, path, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), start, check=True)
    return path


def worktree_remove(repo: Path, path: Path, branch: str | None = None) -> None:
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune")
    if branch and branch_exists(repo, branch):
        git(repo, "branch", "-D", branch)


def exclude_path(repo: Path, rel: str) -> None:
    """Add `rel` to the repo's local exclude file (.git/info/exclude), so run
    artifacts (e.g. <repo>/.rrsi/) never show up as untracked harness edits.
    Safe to call from a worktree: the exclude file is shared through the
    common git dir."""
    r = git(repo, "rev-parse", "--git-common-dir")
    common = r.stdout.strip()
    if r.returncode != 0 or not common:
        return                      # not a git repo: nothing to exclude from
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = (Path(repo).resolve() / common_dir).resolve()
    exclude_dir = common_dir / "info"
    exclude_dir.mkdir(parents=True, exist_ok=True)
    exclude_file = exclude_dir / "exclude"
    entry = rel.rstrip("/")
    lines = []
    if exclude_file.exists():
        lines = [l.strip() for l in exclude_file.read_text().splitlines()]
    if entry in lines:
        return
    with open(exclude_file, "a") as f:
        if lines and lines[-1] != "":
            f.write("\n")
        f.write(f"{entry}\n")


def is_clean(cwd, path: str) -> bool:
    return git(cwd, "status", "--porcelain", "--", path).stdout.strip() == ""


def revert_path(cwd, path: str) -> None:
    git(cwd, "reset", "-q", "--", path)
    git(cwd, "checkout", "--", path)
    git(cwd, "clean", "-fdq", "--", path)


def diff_with_new_files(cwd: Path, path: str) -> str:
    """Working-tree diff of `path` plus the full text of untracked files, so a
    module the proposer just created is reviewed by the critic in full."""
    git(cwd, "add", "-N", "--", path)
    d = git(cwd, "diff", "--", path).stdout
    return d


def commit_path(cwd, path: str, msg: str) -> str:
    git(cwd, "add", "-A", "--", path)
    git(cwd, "commit", "-q", "--allow-empty", "-m", msg, check=True)
    return head(cwd)


def fast_forward(repo: Path, branch: str, commit: str) -> None:
    """Move refs/heads/<branch> to <commit> if it is a fast-forward."""
    r = git(repo, "merge-base", "--is-ancestor", branch, commit)
    if r.returncode != 0:
        raise RuntimeError(f"{commit} is not a fast-forward of {branch}")
    git(repo, "update-ref", f"refs/heads/{branch}", commit, check=True)
