#!/usr/bin/env python3
"""Restore orphaned git worktrees whose admin record this repo has lost.

Run from a repo's MAIN checkout (not a linked worktree):

    scripts/restore-orphaned-worktrees.py --dry-run   # show what would be done
    scripts/restore-orphaned-worktrees.py             # do it

A linked worktree is two halves that point at each other: the worktree dir's
one-line `.git` file (`gitdir: <repo>/.git/worktrees/<id>`) and the repo's
`.git/worktrees/<id>/` admin dir (HEAD, index, `gitdir` back-pointer). When the
admin dir is gone — typically a `git worktree prune` run somewhere that couldn't
see the worktree's directory, such as inside a container that mounts the repo's
`.git` but not its sibling worktrees — the worktree is "orphaned": every git
command in it fails with `not a git repository: .../.git/worktrees/<id>`, and
`git worktree repair` can't help because there is nothing left to re-link. The
working files are intact, though, and usually so is the branch.

This script finds such worktrees and re-registers them: it recreates the admin
dir (`gitdir`, `HEAD` on the worktree's branch, `commondir`), points the
worktree's `.git` at it, and runs `git reset` inside the worktree to rebuild the
index from the branch tip. Working files are never touched, so uncommitted edits
come back as ordinary modifications. Commits that only ever existed in a lost
`.git` can't be recovered, but their content is still in the working tree and
shows up as uncommitted changes.

Where it looks: yolo's worktree directory for this repo
(`~/.claude-yolo/worktrees/<slug>/*`), plus any directories given as arguments
(each either a worktree dir or a parent to scan one level deep). `--all` scans
every repo's dir under `~/.claude-yolo/worktrees/`; only worktrees whose `.git`
points at THIS repo are touched unless `--adopt` is given.

The branch is taken from the worktree's directory name (yolo's convention:
topic == branch). A missing branch is created from `--base` (default `HEAD`).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def resolved(p: pathlib.Path) -> pathlib.Path:
    try:
        return p.resolve()
    except OSError:
        return p


def main_repo(cwd: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(common_git, main_root) for the main checkout containing cwd; exits otherwise."""
    out = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if out.returncode != 0:
        sys.exit("not inside a git repository.")
    common_git = resolved(pathlib.Path(out.stdout.strip()))
    git_dir = resolved(
        pathlib.Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    )
    if git_dir != common_git:
        sys.exit(f"run this from the main checkout ({common_git.parent}), not a linked worktree.")
    return common_git, common_git.parent


def yolo_slug(main_root: pathlib.Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(main_root))


def read_pointer(wt: pathlib.Path) -> pathlib.Path | None:
    """The `gitdir:` target of a worktree dir's `.git` file, or None if not a pointer."""
    dotgit = wt / ".git"
    if not dotgit.is_file():
        return None
    text = dotgit.read_text(errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    target = pathlib.Path(text[len("gitdir:") :].strip())
    if not target.is_absolute():
        target = wt / target
    return target


def candidates(args: argparse.Namespace, home: pathlib.Path, slug: str) -> list[pathlib.Path]:
    """Worktree dirs to examine: yolo's dir for this repo, --all, and explicit paths."""
    roots: list[pathlib.Path] = []
    yolo_root = home / ".claude-yolo" / "worktrees"
    if args.all:
        if yolo_root.is_dir():
            roots += sorted(p for p in yolo_root.iterdir() if p.is_dir())
    elif (yolo_root / slug).is_dir():
        roots.append(yolo_root / slug)
    found: list[pathlib.Path] = []
    for root in roots:
        found += sorted(p for p in root.iterdir() if p.is_dir())
    for raw in args.paths:
        p = pathlib.Path(os.path.expanduser(raw))
        if not p.is_dir():
            sys.exit(f"no such directory: {raw}")
        if (p / ".git").exists():
            found.append(p)
        else:  # a parent dir: scan one level
            found += sorted(c for c in p.iterdir() if c.is_dir() and (c / ".git").exists())
    seen: set[pathlib.Path] = set()
    out = []
    for p in found:
        r = resolved(p)
        if r not in seen:
            seen.add(r)
            out.append(p)
    return out


def checked_out_branches(main_root: pathlib.Path) -> dict[str, str]:
    """branch -> worktree path, for every branch git currently has checked out."""
    out: dict[str, str] = {}
    wt = None
    for line in git(main_root, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            wt = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/") and wt:
            out[line[len("branch refs/heads/") :]] = wt
    return out


def branch_exists(main_root: pathlib.Path, name: str) -> bool:
    return (
        git(main_root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False)
    ).returncode == 0


def free_id(common_git: pathlib.Path, wanted: str, claimed: set[str]) -> str:
    """`wanted` if no admin dir uses it (and this run hasn't claimed it — a dry run
    writes nothing), else the first `wanted<N>` that is free, git's own scheme."""
    admin = common_git / "worktrees"

    def taken(i: str) -> bool:
        return i in claimed or (admin / i).exists()

    wt_id = wanted
    n = 1
    while taken(wt_id):
        wt_id = f"{wanted}{n}"
        n += 1
    claimed.add(wt_id)
    return wt_id


class Plan:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.claimed_ids: set[str] = set()  # admin ids this run has (or would have) used

    def say(self, text: str) -> None:
        print(("  would: " if self.dry_run else "  ") + text)

    def write(self, path: pathlib.Path, text: str) -> None:
        self.say(f"write {path} <- {text.strip()!r}")
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run(self, repo: pathlib.Path, *args: str) -> None:
        self.say("git -C " + " ".join([str(repo), *args]))
        if not self.dry_run:
            git(repo, *args)


def restore(
    wt: pathlib.Path,
    pointer: pathlib.Path,
    common_git: pathlib.Path,
    main_root: pathlib.Path,
    args: argparse.Namespace,
    plan: Plan,
) -> bool:
    """Re-register `wt` (whose pointer target is missing). True if handled."""
    wt_id = pointer.name
    record = common_git / "worktrees" / wt_id

    # The repo moved but the record survived: that's exactly what `repair` fixes.
    if record.is_dir():
        back = record / "gitdir"
        if back.is_file() and resolved(pathlib.Path(back.read_text().strip())) == resolved(
            wt / ".git"
        ):
            print(f"{wt}: record intact, pointer stale (repo moved) -> git worktree repair")
            plan.run(main_root, "worktree", "repair", str(wt))
            return True
    branch = wt.name
    checked_out = checked_out_branches(main_root)
    if branch in checked_out:
        print(
            f"{wt}: SKIP — branch '{branch}' is already checked out at {checked_out[branch]}",
            file=sys.stderr,
        )
        return False

    # the id may be in use by some other worktree (or claimed earlier this run)
    wt_id = free_id(common_git, wt_id, plan.claimed_ids)
    record = common_git / "worktrees" / wt_id
    print(f"{wt}: admin record missing -> re-register as .git/worktrees/{wt_id} on '{branch}'")
    if not branch_exists(main_root, branch):
        tip = git(
            main_root, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}", check=False
        )
        if tip.returncode != 0:
            print(
                f"  SKIP — branch '{branch}' is gone and --base {args.base!r} doesn't resolve",
                file=sys.stderr,
            )
            return False
        print(
            f"  branch '{branch}' is gone; creating it from {args.base} ({tip.stdout.strip()[:9]})"
        )
        plan.run(main_root, "branch", branch, args.base)

    plan.write(record / "gitdir", f"{wt / '.git'}\n")
    plan.write(record / "HEAD", f"ref: refs/heads/{branch}\n")
    plan.write(record / "commondir", "../..\n")
    plan.write(wt / ".git", f"gitdir: {record}\n")
    # Rebuild the index from the branch tip; the working files are untouched, so
    # anything that differs from the tip shows as an ordinary modification.
    plan.run(wt, "reset", "-q")
    if not plan.dry_run:
        status = git(wt, "status", "--short").stdout.splitlines()
        n = len(status)
        print(
            f"  restored; {n} path{'s' if n != 1 else ''} differ from the '{branch}' tip"
            + (" (uncommitted work, or commits lost with the old record)" if n else "")
        )
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="extra worktree dirs (or parents of them) to examine")
    ap.add_argument(
        "-n", "--dry-run", action="store_true", help="print the steps without doing them"
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="scan every repo's dir under ~/.claude-yolo/worktrees/, not just this repo's",
    )
    ap.add_argument(
        "--adopt",
        action="store_true",
        help="also restore worktrees whose .git points at a repo path that no longer "
        "exists (this repo used to live there)",
    )
    ap.add_argument(
        "--base",
        default="HEAD",
        help="where to create a worktree's branch if it is gone (default: HEAD)",
    )
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # keep stdout/stderr interleaved in order

    common_git, main_root = main_repo(pathlib.Path.cwd())
    home = pathlib.Path.home()
    plan = Plan(args.dry_run)
    print(f"repo: {main_root}" + ("  (dry run)" if args.dry_run else ""))

    restored = skipped = 0
    for wt in candidates(args, home, yolo_slug(main_root)):
        pointer = read_pointer(wt)
        if pointer is None:
            continue  # not a linked worktree (a main checkout, or no .git at all)
        if pointer.exists():
            back = pointer / "gitdir"
            if back.is_file() and resolved(pathlib.Path(back.read_text().strip())) == resolved(
                wt / ".git"
            ):
                continue  # healthy: the two halves point at each other
            # The record exists but belongs to another worktree dir (this one is a
            # copy of it, or was restored under the other's id): sharing a HEAD and
            # index with it is not usable, so register this dir under its own id.
            print(f"{wt}: its record .git/worktrees/{pointer.name} belongs to another worktree")
        their_git = pointer.parent.parent  # <repo>/.git
        if resolved(their_git) != common_git:
            if their_git.exists():
                continue  # orphan of some other, still-existing repo — not ours
            if not args.adopt:
                print(
                    f"{wt}: points at {their_git}, which no longer exists — pass --adopt "
                    "if this repo used to live there",
                    file=sys.stderr,
                )
                skipped += 1
                continue
        if restore(wt, pointer, common_git, main_root, args, plan):
            restored += 1
        else:
            skipped += 1

    verb = "would restore" if args.dry_run else "restored"
    print(
        f"{verb} {restored} worktree{'s' if restored != 1 else ''}"
        + (f", skipped {skipped}" if skipped else "")
        + "."
    )
    if restored and not args.dry_run:
        print(git(main_root, "worktree", "list").stdout, end="")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
