# Runbook: Git

## Overview

This runbook covers the Git operations used to diagnose regressions, manage hotfixes, and maintain clean history in a fast-moving trading codebase. All scenarios map to `lab_git.py` (G-01 through G-05).

---

## G-01 — Bisect a Regression

Use `git bisect` to binary-search for the commit that introduced a bug.

### When to use
- A bug exists now but didn't in a known-good earlier version
- Manual log scanning is too slow (hundreds of commits)

### Procedure

```bash
# Start bisect
git bisect start

# Mark current state as broken
git bisect bad

# Mark last known-good commit (tag, SHA, or relative ref)
git bisect good v2.3.0
git bisect good abc1234

# Git checks out the midpoint commit — run your test
python3 -c "from notional import calc; assert calc('BUY', 100, 185.5) > 0"

# Tell git the result
git bisect good   # test passed — bug is in a later commit
git bisect bad    # test failed — bug is in an earlier commit

# Git narrows down and eventually identifies the culprit:
# "abc1234 is the first bad commit"

# Exit bisect and return to original branch
git bisect reset
```

### Automated bisect (when you have a test script)

```bash
# Git will run the script automatically at each step
# Script must exit 0 for "good", non-zero for "bad"
git bisect run python3 tests/test_notional.py

# Result: Git finds the bad commit without manual steps
```

### After finding the bad commit

```bash
git show <bad-commit-sha>          # see what changed
git log <bad-commit-sha> -1        # commit message
git diff <bad-commit-sha>~1 <bad-commit-sha>   # exact diff
```

---

## G-02 — Clean Up Commit History (Amend / Rebase)

### Amend the last commit

```bash
# Fix a typo or add a forgotten file to the most recent commit
git add forgotten_file.py
git commit --amend --no-edit   # keep same message

# Change just the commit message
git commit --amend -m "Fix notional calculation for SELL orders"

# WARNING: Never amend commits that have been pushed to a shared branch
```

### Interactive rebase — squash/reorder/edit commits

```bash
# Edit the last N commits (here: 4)
git rebase -i HEAD~4

# In the editor, change "pick" to:
#   pick   → keep as-is
#   reword → keep but edit message
#   squash → merge into previous commit
#   fixup  → merge into previous, discard this commit's message
#   drop   → remove this commit entirely

# Example: squash 3 WIP commits into one clean commit
# pick abc1234 Add notional calculation
# squash def5678 WIP fix
# squash ghi9012 WIP fix 2
# reword jkl3456 Add position reconciliation check
```

### Rebase onto another branch

```bash
# Move your branch's commits on top of updated main
git fetch origin
git rebase origin/main

# If conflicts arise:
git status                      # see conflicted files
# ... resolve conflicts in editor ...
git add <resolved-file>
git rebase --continue           # move to next commit

# Abort if it goes wrong
git rebase --abort
```

---

## G-03 — Recover a Deleted Branch

### If deleted locally but still on remote

```bash
git fetch origin
git checkout -b <branch-name> origin/<branch-name>
```

### If deleted locally and not on remote — use reflog

```bash
# reflog records every HEAD movement, including deleted branches
git reflog

# Find the last commit SHA from the deleted branch
# Output looks like:
# abc1234 HEAD@{3}: checkout: moving from deleted-branch to main

# Recreate the branch from that SHA
git checkout -b recovered-branch abc1234
git branch recovered-branch abc1234   # or just create without switching
```

### Key insight
`git reflog` retains entries for 90 days by default. As long as you act within that window, "deleted" work is recoverable.

---

## G-04 — Cherry-Pick a Hotfix

### When to use
- A fix was committed to `main` but needs to be applied to a release branch without merging all of `main`

```bash
# Find the commit SHA to cherry-pick
git log main --oneline | grep "fix"
# e.g.: abc1234 Fix divide-by-zero in notional calculation

# Checkout the target branch
git checkout release/v2.3

# Cherry-pick the fix
git cherry-pick abc1234

# If there are conflicts:
# ... resolve ...
git add <file>
git cherry-pick --continue

# Abort if needed
git cherry-pick --abort

# Cherry-pick a range of commits (inclusive)
git cherry-pick abc1234..def5678

# Cherry-pick without auto-committing (to edit the message)
git cherry-pick abc1234 --no-commit
git commit -m "Backport: fix divide-by-zero in notional calc [release/v2.3]"
```

---

## G-05 — Blame and Annotate a Regression

### Find who changed a specific line

```bash
# Show who last changed each line of a file
git blame src/notional.py

# Output format: SHA (Author Date LineNum) Code
# abc12345 (Alice Chen 2024-01-10 09:30:00 +0000  42) def calc_notional(qty, price):

# Blame a specific line range
git blame -L 40,55 src/notional.py

# Blame ignoring whitespace changes
git blame -w src/notional.py

# Blame ignoring commits that only moved code
git blame -M src/notional.py

# Show blame for a file at a specific commit
git blame abc1234 -- src/notional.py
```

### Search commit history for a change

```bash
# Find commits that touched a specific function or string
git log -S "calc_notional" --oneline           # added or removed the string
git log -G "quantity \* price" --oneline       # regex match in diff

# Show diffs for a specific file across all commits
git log -p -- src/notional.py | less

# When was a specific string introduced?
git log -S "SELL_SIGN = -1" --all --oneline

# Find all commits by an author on a file
git log --author="Alice" -- src/notional.py
```

### Revert a bad deploy

```bash
# Create a new commit that undoes a specific commit
git revert abc1234

# Revert a merge commit (specify parent with -m)
git revert -m 1 <merge-commit-sha>

# Revert a range (oldest first, newest last)
git revert abc1234..def5678

# WHY revert instead of reset:
# revert adds a new commit — safe to push to shared branches
# reset rewrites history — dangerous on shared branches
```

---

## Essential Git Cheat Sheet

### Status and Log

```bash
git status                              # working tree state
git log --oneline --graph --all         # visual branch graph
git log --oneline -20                   # last 20 commits
git log --since="2 days ago" --oneline
git show <sha>                          # inspect a commit
git diff HEAD~3 HEAD -- src/notional.py # changes to a file over 3 commits
```

### Branching

```bash
# Get default branch name (handles both main and master)
git symbolic-ref --short HEAD

# Create and switch
git checkout -b fix/notional-calculation

# Track a remote branch
git checkout -b feature/x origin/feature/x

# Delete a branch
git branch -d my-branch          # safe delete (must be merged)
git branch -D my-branch          # force delete
git push origin --delete my-branch
```

### Stash

```bash
git stash                         # save dirty working tree
git stash pop                     # restore most recent stash
git stash list                    # see all stashes
git stash apply stash@{2}         # apply a specific stash
```

### Undoing

```bash
git restore <file>                # discard unstaged changes to a file
git restore --staged <file>       # unstage a file (keep changes)
git reset HEAD~1                  # undo last commit, keep changes staged
git reset --hard HEAD~1           # undo last commit AND changes — DESTRUCTIVE
```

### Remote

```bash
git fetch origin                  # download without merging
git pull --rebase origin main     # rebase on top of remote main (cleaner history)
git push -u origin <branch>       # push and set upstream
```

---

## Escalation Criteria

| Situation | Action |
|-----------|--------|
| Force push to `main` needed | Get explicit approval from team lead — potentially destructive |
| Reflog SHA not found (> 90 days) | Work may be unrecoverable — check CI artifact cache |
| Large binary files accidentally committed | Use `git filter-repo` to rewrite history — team coordination required |
| Merge conflict too complex to resolve | Sync with both authors to understand intent before resolving |
