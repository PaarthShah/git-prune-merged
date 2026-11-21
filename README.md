# git-prune-merged

Prune deleted remote branches, then delete **only** the local branches that are
safe to remove.

Running it inside a repo does three things:

1. `git remote prune origin` — drop stale remote-tracking refs (the `origin/foo`
   that the forge deletes after a PR/MR merge).
2. Find local branches whose upstream is gone.
3. Delete such a branch **only if every commit on it is provably in main**.

It **never** deletes:

- branches that are not provably merged to main,
- branches that were **never pushed** (they have no upstream to prune, so they
  are excluded automatically),
- branches checked out in a worktree, or
- branches you protect with `git config branch.<name>.deleteMerged false` (the
  same setting `git branch --delete-merged` honours in git 2.56+).

### How "merged" is decided

Everything is plain git, so it works with any host. A branch is merged when
any of these hold (cheapest first):

1. **Ancestor of main** — a normal merge or fast-forward.
2. **Squash/rebase merge** — the branch's whole diff matches one commit on main
   (`git cherry` against a throwaway commit of the branch's diff).
3. **Part of a merged pull/merge request** — the branch's tip is an ancestor of
   a review head that is itself merged by these rules. Review heads are the
   final heads of pull/merge requests, which forges publish as ordinary refs:

   | ref pattern | hosts |
   | --- | --- |
   | `refs/pull/*/head` | GitHub, Gitea, Forgejo |
   | `refs/merge-requests/*/head` | GitLab |
   | `refs/pull-requests/*/from` | Bitbucket Data Center |

   The upstream tip captured just before this run prunes it counts as a review
   head too. This handles a local branch that is *behind* what was merged
   (commits pushed from another machine or worktree, "update branch" merges,
   review fixes) and stacked PRs.
4. **Changes already in main** — merging the branch into some commit C on
   main's first-parent history leaves C unchanged, either across the range since
   the merge-base (a stack that landed as several squashes) or for C alone
   (a squash whose lines main later edited again). These are in-memory
   `git merge-tree` merges; nothing touches your index or worktree. This
   check needs git 2.45 or newer; on older git it never matches, so branches
   it would have caught are kept.

Review heads are fetched into a temporary ref namespace that is deleted before
the run ends, so no refs are left behind. The objects stay in the object store
until `git gc`, so the first run in a repo with many PRs downloads more (for
example, 2,500 PRs took about 20s and 200 MB), and later runs are fast. Pass
`--no-review-refs` to skip this.

## Install

```sh
uv tool install git+https://github.com/PaarthShah/git-prune-merged
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
| `--no-review-refs` | don't fetch pull/merge request heads from the remote |

"Merged to main" is checked against `origin/main` first (the source of truth for
merges) and then local `main`, so it works even if your local `main` is stale.
