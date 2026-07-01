"""git-prune-merged: prune deleted remotes, delete only the safe local branches.

Workflow, run inside any git repo:

  1. ``git remote prune <remote>`` removes stale remote-tracking refs (the
     ``origin/foo`` that GitHub deleted after a merge).
  2. Find local branches whose upstream was just pruned ("gone").
  3. Delete such a branch *only* if it is merged into main -- either a normal
     merge (ancestor of main) or a squash/rebase merge (its whole diff is
     already present in main). Never touch a branch that is not merged, and
     never touch a branch that was never pushed (those have no upstream to
     prune, so they are excluded by construction).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.theme import Theme

from . import __version__

_THEME = Theme(
    {
        "delete": "bold green",
        "keep": "yellow",
        "pruned": "dim",
        "info": "cyan",
        "reason": "dim",
        "error": "bold red",
        "dry": "bold magenta",
    }
)

# Fallback identity so `git commit-tree` (used for squash detection) works even
# in a repo without user.name / user.email configured. This never creates a
# real commit -- the object is dangling and only fed to `git cherry`.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "git-prune-merged",
    "GIT_AUTHOR_EMAIL": "git-prune-merged@localhost",
    "GIT_COMMITTER_NAME": "git-prune-merged",
    "GIT_COMMITTER_EMAIL": "git-prune-merged@localhost",
}


class GitError(RuntimeError):
    pass


def _git(args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        text=True,
        capture_output=True,
        env={**os.environ, **env} if env else None,
    )
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or f"git {' '.join(args)} failed").strip())
    return proc


def _out(args: list[str]) -> str:
    return _git(args).stdout.strip()


def ensure_git_repo() -> None:
    if _git(["rev-parse", "--git-dir"], check=False).returncode != 0:
        raise GitError("not inside a git repository")


def current_branch() -> str | None:
    proc = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    return proc.stdout.strip() or None


def ref_exists(ref: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False).returncode == 0


def detect_main(remote: str) -> str:
    """Best-effort main-branch name: remote HEAD, else main/master."""
    proc = _git(["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"], check=False)
    head = proc.stdout.strip()  # e.g. "origin/main"
    if head.startswith(f"{remote}/"):
        return head[len(remote) + 1 :]
    for candidate in ("main", "master"):
        if ref_exists(candidate) or ref_exists(f"{remote}/{candidate}"):
            return candidate
    raise GitError("could not detect the main branch; pass --main")


def main_refs(main: str, remote: str) -> list[str]:
    """Refs that count as 'main' history, remote first (it is the source of truth)."""
    refs = [f"{remote}/{main}", main]
    return [r for r in refs if ref_exists(r)]


def prune_dry_run(remote: str) -> set[str]:
    """Remote-tracking refs that prune would remove (without removing them)."""
    proc = _git(["remote", "prune", remote, "--dry-run"], check=False)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"cannot reach remote '{remote}'")
    pruned: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("*") and "prune" in line:
            pruned.add(line.split()[-1])  # trailing token is "origin/foo"
    return pruned


def do_prune(remote: str) -> None:
    _git(["remote", "prune", remote])


@dataclass
class Branch:
    name: str
    upstream: str


def gone_local_branches(remote: str) -> list[Branch]:
    """Local branches whose upstream on ``remote`` is gone.

    Keys off git's own ``[gone]`` tracking status, not just refs pruned in this
    run -- a plain ``git fetch --prune`` (or a prior run) can have already
    removed the remote-tracking ref while the local branch lingers. Branches
    that were never pushed have no upstream and are excluded by construction.
    """
    fmt = "%(refname:short)%09%(upstream:short)%09%(upstream:track)"
    out = _out(["for-each-ref", f"--format={fmt}", "refs/heads"])
    prefix = f"{remote}/"
    branches: list[Branch] = []
    for line in out.splitlines():
        parts = line.split("\t")
        name = parts[0]
        upstream = parts[1] if len(parts) > 1 else ""
        track = parts[2] if len(parts) > 2 else ""
        if upstream.startswith(prefix) and track == "[gone]":
            branches.append(Branch(name=name, upstream=upstream))
    return branches


def is_ancestor(branch: str, of: str) -> bool:
    return _git(["merge-base", "--is-ancestor", branch, of], check=False).returncode == 0


def is_squash_merged(branch: str, main: str) -> bool:
    """True if the branch's entire diff is already present in main.

    Detects squash- and rebase-merges that a plain ancestor check misses:
    build a throwaway commit with the branch's tree on top of the merge-base,
    then ask `git cherry` whether that patch is already in main ("-" prefix).
    """
    merge_base = _out(["merge-base", main, branch])
    if not merge_base:
        return False
    tree = _out(["rev-parse", f"{branch}^{{tree}}"])
    fake = _git(["commit-tree", tree, "-p", merge_base, "-m", "_"], env=_COMMIT_ENV).stdout.strip()
    cherry = _out(["cherry", main, fake])
    return cherry.startswith("-")


def is_merged(branch: str, mains: list[str]) -> bool:
    return any(is_ancestor(branch, m) for m in mains) or any(is_squash_merged(branch, m) for m in mains)


def delete_branch(name: str) -> None:
    # -D (not -d) because squash-merged branches look "unmerged" to git; we have
    # already verified the content is in main ourselves.
    _git(["branch", "-D", name])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-prune-merged",
        description=(
            "Prune deleted remote branches, then delete local branches whose upstream "
            "was pruned AND which are merged (incl. squash) to main. Branches that are "
            "not merged, or were never pushed, are always kept."
        ),
    )
    parser.add_argument("--remote", default="origin", help="remote to prune (default: origin)")
    parser.add_argument("--main", help="main branch name (default: auto-detect)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show what would happen; change nothing")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print branches that are deleted")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    console = Console(theme=_THEME)
    err = Console(theme=_THEME, stderr=True)

    def say(msg: str = "") -> None:
        if not args.quiet:
            console.print(msg)

    try:
        ensure_git_repo()
        main_branch = args.main or detect_main(args.remote)
        mains = main_refs(main_branch, args.remote)
        if not mains:
            raise GitError(f"no ref found for main branch '{main_branch}'")

        tag = " [dry](dry-run)[/dry]" if args.dry_run else ""
        say(f"[info]Pruning[/info] '{args.remote}' (main = {main_branch}){tag}…")
        pruned = prune_dry_run(args.remote)
        if not args.dry_run:
            do_prune(args.remote)

        if pruned:
            for ref in sorted(pruned):
                say(f"  [pruned]pruned remote ref: {ref}[/pruned]")
        else:
            say("  [pruned]no stale remote-tracking refs[/pruned]")

        candidates = gone_local_branches(args.remote)
        cur = current_branch()

        deleted: list[str] = []
        kept: list[tuple[str, str]] = []
        for b in candidates:
            if b.name == cur:
                kept.append((b.name, "checked out (cannot delete current branch)"))
                continue
            if not is_merged(b.name, mains):
                kept.append((b.name, "not merged to main"))
                continue
            if not args.dry_run:
                delete_branch(b.name)
            deleted.append(b.name)

        say()
        verb = "Would delete" if args.dry_run else "Deleted"
        if deleted:
            say(f"[delete]{verb} {len(deleted)} merged branch(es):[/delete]")
            for name in sorted(deleted):
                say(f"  [delete]✔ {name}[/delete]")
        else:
            say("[pruned]No local branches to delete.[/pruned]")

        if kept and not args.quiet:
            say()
            say(f"[keep]Kept {len(kept)} branch(es):[/keep]")
            for name, reason in sorted(kept):
                say(f"  [keep]•[/keep] {name} [reason]— {reason}[/reason]")

        return 0
    except GitError as exc:
        err.print(f"[error]git-prune-merged:[/error] {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
