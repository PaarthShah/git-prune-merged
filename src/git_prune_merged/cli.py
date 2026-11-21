"""git-prune-merged: prune deleted remotes, delete only the safe local branches.

Workflow, run inside any git repo:

  1. ``git remote prune <remote>`` removes stale remote-tracking refs (the
     ``origin/foo`` that the forge deleted after a merge).
  2. Find local branches whose upstream is gone.
  3. Delete such a branch *only* if every commit on it is proven to be in main.
     Never touch a branch that is not provably merged, and never touch a branch
     that was never pushed (those have no upstream, so they are excluded by
     construction).

A branch counts as merged when any of these hold, cheapest first:

  - its tip is an ancestor of main (normal merge / fast-forward);
  - its whole diff matches a single commit on main (squash or rebase merge);
  - its tip is an ancestor of a *review head* -- the final head of a pull or
    merge request, fetched over plain git from the forge's review-ref namespace,
    or the upstream tip captured just before this run pruned it -- and that head
    is itself merged. This covers a local branch that is behind what was
    actually merged (commits pushed from elsewhere, "update branch" merges,
    review fixes) and stacked PRs;
  - its changes are already present in main at some commit on main's
    first-parent history (squash of a stack, or a squash whose later lines in
    main were edited again).
"""

lazy import argparse
lazy import contextlib
lazy import os
lazy import subprocess
lazy from collections import defaultdict, deque
lazy from collections.abc import Iterable
lazy from dataclasses import dataclass

lazy from rich.console import Console
lazy from rich.markup import escape
lazy from rich.theme import Theme

lazy from . import __version__

# Fallback identity so `git commit-tree` (used for squash detection) works even
# in a repo without user.name / user.email configured. This never creates a
# real commit -- the object is dangling and only fed to `git cherry`.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "git-prune-merged",
    "GIT_AUTHOR_EMAIL": "git-prune-merged@localhost",
    "GIT_COMMITTER_NAME": "git-prune-merged",
    "GIT_COMMITTER_EMAIL": "git-prune-merged@localhost",
}

# Where forges publish the head of every pull/merge request, readable by any
# git client. Patterns that a remote does not have simply match nothing.
REVIEW_REF_PATTERNS = (
    "refs/pull/*/head",  # GitHub, Gitea, Forgejo
    "refs/merge-requests/*/head",  # GitLab
    "refs/pull-requests/*/from",  # Bitbucket Data Center
)

# Review refs are fetched into this namespace only for the duration of a run,
# so their objects are available locally without leaving refs behind.
_SCRATCH_NS = "refs/git-prune-merged"

# Upper bound on main-history commits examined per branch by the
# "changes already in main" check, which dominates runtime for branches that
# are genuinely unmerged.
_MAX_LANDED_CANDIDATES = 400


def _theme() -> Theme:
    return Theme(
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


class GitError(RuntimeError):
    pass


def _git(
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        check=False,
        text=True,
        capture_output=True,
        input=stdin,
        env={**os.environ, **env} if env else None,
    )
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or f"git {' '.join(args)} failed").strip())
    return proc


def _out(args: list[str], *, stdin: str | None = None) -> str:
    return _git(args, stdin=stdin).stdout.strip()


def ensure_git_repo() -> None:
    if _git(["rev-parse", "--git-dir"], check=False).returncode != 0:
        raise GitError("not inside a git repository")


def current_branch() -> str | None:
    proc = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    return proc.stdout.strip() or None


def ref_exists(ref: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False).returncode == 0


def rev_parse(rev: str) -> str:
    return _out(["rev-parse", "--verify", f"{rev}^{{commit}}"])


def worktree_branches() -> dict[str, str]:
    """Map branch name -> worktree path for every branch checked out in a worktree.

    A branch checked out in a worktree cannot be deleted without disturbing that
    worktree, so we skip it rather than letting `git branch -D` abort the run.
    Detached-HEAD worktrees have no branch line and are ignored.
    """
    out = _out(["worktree", "list", "--porcelain"])
    held: dict[str, str] = {}
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]  # e.g. "refs/heads/foo"
            held[ref.removeprefix("refs/heads/")] = path
    return held


def protected_branches() -> set[str]:
    """Branches opted out of bulk cleanup with ``branch.<name>.deleteMerged = false``.

    Same setting that ``git branch --delete-merged`` (git 2.56+) honours.
    """
    proc = _git(
        ["config", "--type=bool", "--get-regexp", r"^branch\..*\.deletemerged$"],
        check=False,
    )
    protected: set[str] = set()
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value == "false":
            protected.add(key[len("branch.") : -len(".deletemerged")])
    return protected


def detect_main(remote: str) -> str:
    """Best-effort main-branch name: remote HEAD, else main/master."""
    proc = _git(
        ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"],
        check=False,
    )
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
    tip: str


def gone_local_branches(remote: str, pruning: set[str]) -> list[Branch]:
    """Local branches whose upstream on ``remote`` is gone or about to be pruned.

    Keys off git's own ``[gone]`` tracking status, so a branch whose upstream was
    removed by an earlier ``git fetch --prune`` is still found, plus any branch
    whose upstream is in ``pruning`` (needed for a dry run, where the prune has
    not happened yet). Branches that were never pushed have no upstream and are
    excluded by construction.
    """
    fmt = "%(refname:short)%09%(objectname)%09%(upstream:short)%09%(upstream:track)"
    out = _out(["for-each-ref", f"--format={fmt}", "refs/heads"])
    prefix = f"{remote}/"
    branches: list[Branch] = []
    for line in out.splitlines():
        name, tip, upstream, track = [*line.split("\t"), "", "", ""][:4]
        if upstream.startswith(prefix) and (track == "[gone]" or upstream in pruning):
            branches.append(Branch(name=name, upstream=upstream, tip=tip))
    return branches


def is_ancestor(rev: str, of: str) -> bool:
    return _git(["merge-base", "--is-ancestor", rev, of], check=False).returncode == 0


def is_squash_merged(rev: str, main: str) -> bool:
    """True if the branch's entire diff matches a single commit on main.

    Builds a throwaway commit with the branch's tree on top of the merge-base,
    then asks `git cherry` whether that patch is already in main ("-" prefix).
    """
    merge_base = _git(["merge-base", main, rev], check=False).stdout.strip()
    if not merge_base:
        return False
    tree = _out(["rev-parse", f"{rev}^{{tree}}"])
    fake = _git(["commit-tree", tree, "-p", merge_base, "-m", "_"], env=_COMMIT_ENV).stdout.strip()
    cherry = _out(["cherry", main, fake])
    return cherry.startswith("-")


def merge_tree_batch(merges: list[tuple[str, str, str]]) -> list[str | None]:
    """Run three-way merges ``(base, ours, theirs)`` in one `git merge-tree` process.

    Returns the resulting tree for each clean merge and None for each conflicted
    one. Nothing touches the index or worktree. Arguments may be commits or trees.
    """
    if not merges:
        return []
    stdin = "".join(f"{base} -- {ours} {theirs}\n" for base, ours, theirs in merges)
    proc = _git(
        ["merge-tree", "--stdin", "--write-tree", "--no-messages", "--name-only"],
        stdin=stdin,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return [None] * len(merges)
    # Each record is "<status>\0<tree>\0" then conflicted paths, each "\0"-terminated,
    # then an empty field.
    fields = proc.stdout.split("\0")
    results: list[str | None] = []
    i = 0
    while len(results) < len(merges) and i + 1 < len(fields):
        status, tree = fields[i], fields[i + 1]
        i += 2
        while i < len(fields) and fields[i] != "":
            i += 1
        i += 1
        results.append(tree if status == "1" else None)
    results.extend([None] * (len(merges) - len(results)))
    return results


def _changed_paths(old: str, new: str) -> set[str]:
    out = _out(["-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", old, new])
    return set(out.splitlines())


def landed_in(rev: str, main: str) -> str | None:
    """First commit on main's first-parent history that already contains ``rev``'s changes.

    For each main commit C after the merge-base B that touches the branch's
    files, oldest first, two containment tests run as in-memory merges:

    - B..C contains the branch: merging the branch into C (base B) yields C's
      tree unchanged. Catches a stack that landed as several squashes.
    - C alone contains the branch: replay the branch onto C's parent, then merge
      that into C (base C's parent) and get C's tree back. Catches a squash
      whose lines were later edited again in main, which makes the merge
      against main's tip conflict.

    Returns C, or None if no such commit is found.
    """
    base = _git(["merge-base", main, rev], check=False).stdout.strip()
    if not base:
        return None
    files = _changed_paths(base, rev)
    if not files:
        return base
    log = _out(
        [
            "-c",
            "core.quotePath=false",
            "log",
            "--first-parent",
            "--diff-merges=first-parent",
            "--reverse",
            "--name-only",
            "--no-renames",
            "--format=%x01%H %T %P",
            f"{base}..{main}",
        ]
    )
    candidates: list[tuple[str, str, str, set[str]]] = []
    for chunk in log.split("\x01")[1:]:
        header, _, names = chunk.partition("\n")
        commit, tree, *parents = header.split()
        touched = set(names.split("\n")) - {""}
        if parents and touched & files:
            candidates.append((commit, tree, parents[0], touched))
        if len(candidates) >= _MAX_LANDED_CANDIDATES:
            break

    range_checks: set[int] = set()
    replay_checks: set[int] = set()
    merges: list[tuple[str, str, str]] = []
    covered: set[str] = set()
    for idx, (commit, _tree, parent, touched) in enumerate(candidates):
        covered |= touched
        if files <= covered:
            range_checks.add(idx)
            merges.append((base, commit, rev))
        if files <= touched:
            replay_checks.add(idx)
            merges.append((base, parent, rev))
    results = merge_tree_batch(merges)

    hits: set[int] = set()
    second: list[tuple[str, str, str]] = []
    second_idx: list[int] = []
    pos = 0
    for idx, (commit, tree, parent, _touched) in enumerate(candidates):
        if idx in range_checks:
            if results[pos] == tree:
                hits.add(idx)
            pos += 1
        if idx in replay_checks:
            replayed = results[pos]
            pos += 1
            if replayed is not None and idx not in hits:
                second.append((parent, commit, replayed))
                second_idx.append(idx)
    for idx, result in zip(second_idx, merge_tree_batch(second), strict=True):
        if result == candidates[idx][1]:
            hits.add(idx)
    return candidates[min(hits)][0] if hits else None


class MergeChecker:
    """Decides, with caching, whether a revision's commits are all in main."""

    def __init__(self, mains: list[str]) -> None:
        self.mains = mains
        self._direct: dict[str, str | None] = {}
        self._full: dict[str, str | None] = {}

    def direct(self, rev: str) -> str | None:
        """Cheap proofs: ancestor of main, or one-commit squash/rebase merge."""
        if rev not in self._direct:
            proof = None
            for m in self.mains:
                if is_ancestor(rev, m):
                    proof = f"merged into {m}"
                    break
            else:
                for m in self.mains:
                    if is_squash_merged(rev, m):
                        proof = f"squash-merged into {m}"
                        break
            self._direct[rev] = proof
        return self._direct[rev]

    def full(self, rev: str) -> str | None:
        """Cheap proofs, then whether the changes are present somewhere in main."""
        if rev not in self._full:
            proof = self.direct(rev)
            if proof is None:
                main = self.mains[0]
                commit = landed_in(rev, main)
                if commit is not None:
                    proof = f"changes in {main} as of {commit[:10]}"
            self._full[rev] = proof
        return self._full[rev]


def _clear_scratch_refs() -> None:
    refs = _out(["for-each-ref", "--format=delete %(refname)", _SCRATCH_NS])
    if refs:
        _git(["update-ref", "--stdin"], stdin=refs + "\n")


def fetch_review_heads(remote: str) -> dict[str, str]:
    """Final heads of the remote's pull/merge requests, as ``{sha: ref name}``.

    Fetches every review-ref pattern into a scratch namespace so the objects are
    present locally, reads the tips, then deletes the scratch refs. The fetched
    objects stay in the object store until gc, which keeps repeat runs cheap.
    """
    _clear_scratch_refs()
    refspecs = [f"+{pat}:{_SCRATCH_NS}/{i}/*" for i, pat in enumerate(REVIEW_REF_PATTERNS)]
    try:
        _git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--no-recurse-submodules",
                "--no-auto-maintenance",
                remote,
                *refspecs,
            ]
        )
        out = _out(["for-each-ref", "--format=%(objectname) %(refname)", _SCRATCH_NS])
    finally:
        _clear_scratch_refs()
    heads: dict[str, str] = {}
    for line in out.splitlines():
        sha, ref = line.split(" ", 1)
        idx, num = ref[len(_SCRATCH_NS) + 1 :].split("/", 1)
        heads.setdefault(sha, REVIEW_REF_PATTERNS[int(idx)].replace("*", num))
    return heads


def heads_containing(tips: Iterable[str], heads: Iterable[str], mains: list[str]) -> dict[str, list[str]]:
    """For each tip, the heads it is an ancestor of (looking only outside main)."""
    head_set = set(heads)
    stdin = "\n".join([*head_set, *(f"^{m}" for m in mains)]) + "\n"
    children: dict[str, list[str]] = defaultdict(list)
    for line in _out(["rev-list", "--parents", "--stdin"], stdin=stdin).splitlines():
        commit, *parents = line.split()
        for p in parents:
            children[p].append(commit)
    found: dict[str, list[str]] = {}
    for tip in tips:
        seen = {tip}
        queue = deque([tip])
        hits: list[str] = []
        while queue:
            node = queue.popleft()
            if node in head_set:
                hits.append(node)
            for child in children.get(node, ()):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        found[tip] = hits
    return found


def delete_branch(name: str) -> None:
    # -D (not -d) because squash-merged branches look "unmerged" to git; we have
    # already verified the content is in main ourselves.
    _git(["branch", "-D", name])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-prune-merged",
        description=(
            "Prune deleted remote branches, then delete local branches whose upstream "
            "is gone AND whose commits are all in main (merged, squash-merged, or part "
            "of a merged pull/merge request). Branches that are not provably merged, "
            "or were never pushed, are always kept."
        ),
    )
    parser.add_argument("--remote", default="origin", help="remote to prune (default: origin)")
    parser.add_argument("--main", help="main branch name (default: auto-detect)")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would happen; change nothing",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only print branches that are deleted",
    )
    parser.add_argument(
        "--no-review-refs",
        action="store_true",
        help="don't fetch pull/merge request heads from the remote",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    theme = _theme()
    console = Console(theme=theme)
    err = Console(theme=theme, stderr=True)

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
        pruned_tips = {rev_parse(ref): f"{ref} (before prune)" for ref in pruned if ref_exists(ref)}
        if not args.dry_run:
            do_prune(args.remote)

        if pruned:
            for ref in sorted(pruned):
                say(f"  [pruned]pruned remote ref: {escape(ref)}[/pruned]")
        else:
            say("  [pruned]no stale remote-tracking refs[/pruned]")

        candidates = gone_local_branches(args.remote, pruned)
        cur = current_branch()
        held = worktree_branches()
        protected = protected_branches()
        checker = MergeChecker(mains)

        deleted: list[tuple[str, str]] = []
        kept: list[tuple[str, str]] = []
        unresolved: list[Branch] = []
        for b in candidates:
            if b.name == cur:
                kept.append((b.name, "checked out (cannot delete current branch)"))
            elif b.name in held:
                kept.append((b.name, f"checked out in worktree at {held[b.name]}"))
            elif b.name in protected:
                kept.append((b.name, f"protected by branch.{b.name}.deleteMerged=false"))
            elif proof := checker.direct(b.tip):
                deleted.append((b.name, proof))
            else:
                unresolved.append(b)

        if unresolved:
            heads = dict(pruned_tips)
            if not args.no_review_refs:
                with (
                    console.status(f"Fetching review refs from '{args.remote}'…")
                    if not args.quiet
                    else contextlib.nullcontext()
                ):
                    try:
                        heads = {**fetch_review_heads(args.remote), **heads}
                    except GitError as exc:
                        err.print(f"[keep]warning:[/keep] could not fetch review refs: {escape(str(exc))}")
            containing = heads_containing((b.tip for b in unresolved), heads, mains) if heads else {}
            with console.status("Checking unresolved branches…") if not args.quiet else contextlib.nullcontext():
                for b in unresolved:
                    proof = None
                    for head in containing.get(b.tip, []):
                        head_proof = checker.full(head)
                        if head_proof:
                            proof = f"in {heads[head]}, {head_proof}"
                            break
                    if proof is None:
                        proof = checker.full(b.tip)
                    if proof:
                        deleted.append((b.name, proof))
                    else:
                        kept.append((b.name, "not merged to main"))

        if not args.dry_run:
            for name, _ in deleted:
                delete_branch(name)

        say()
        verb = "Would delete" if args.dry_run else "Deleted"
        if deleted:
            say(f"[delete]{verb} {len(deleted)} merged branch(es):[/delete]")
            for name, proof in sorted(deleted):
                if args.quiet:
                    console.print(f"[delete]✔ {escape(name)}[/delete]")
                else:
                    say(f"  [delete]✔ {escape(name)}[/delete] [reason]— {escape(proof)}[/reason]")
        else:
            say("[pruned]No local branches to delete.[/pruned]")

        if kept and not args.quiet:
            say()
            say(f"[keep]Kept {len(kept)} branch(es):[/keep]")
            for name, reason in sorted(kept):
                say(f"  [keep]•[/keep] {escape(name)} [reason]— {escape(reason)}[/reason]")

        return 0
    except GitError as exc:
        err.print(f"[error]git-prune-merged:[/error] {escape(str(exc))}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
