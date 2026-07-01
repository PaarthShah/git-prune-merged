# git-prune-merged

Prune deleted remote branches, then delete **only** the local branches that are
safe to remove.

Running it inside a repo does three things:

1. `git remote prune origin` — drop stale remote-tracking refs (the `origin/foo`
   that GitHub deletes after a PR merge).
2. Find local branches whose upstream was just pruned ("gone").
3. Delete such a branch **only if it is merged to main** — a normal merge
   (ancestor of main) *or* a squash/rebase merge (its whole diff is already in
   main).

It **never** deletes:

- branches that were **never merged** to main, or
- branches that were **never pushed** (they have no upstream to prune, so they
  are excluded automatically).

Squash-merged branches — which `git branch -d` refuses to remove because they
look "unmerged" — are detected by replaying the branch's diff onto the merge-base
and asking `git cherry` whether that patch is already in main.

## Install

```sh
uv tool install git+https://github.com/<you>/git-prune-merged
# or, from a local clone:
uv tool install /path/to/git-prune-merged
```

## Use

```sh
git-prune-merged            # prune + delete safe branches
git-prune-merged -n         # dry run: show what would happen, change nothing
git prune-merged            # same thing (git finds git-* on PATH)
```

Options:

| flag | meaning |
| --- | --- |
| `--remote NAME` | remote to prune (default `origin`) |
| `--main NAME` | main branch name (default: auto-detect from remote HEAD, else `main`/`master`) |
| `-n`, `--dry-run` | change nothing |
| `-q`, `--quiet` | only print deleted branches |

"Merged to main" is checked against `origin/main` first (the source of truth for
merges) and then local `main`, so it works even if your local `main` is stale.
