# Git Cheat Sheet — budget-tool

A beginner-friendly reference. Git saves **snapshots** of your project so you can
track changes over time and undo mistakes.

> **Tip:** Open a terminal *inside* this folder and you can drop the
> `-C /Users/danlear/Documents/Claude/Projects/budget-tool` part from every
> command below — just type `git status`, `git add .`, etc.
> To open a terminal here: in Finder, right-click the `budget-tool` folder →
> Services → "New Terminal at Folder".

---

## The everyday loop (save your work)

Do this whenever you've made changes you want to keep:

```bash
git status                       # 1. See what changed
git add .                        # 2. Stage everything (respects .gitignore)
git commit -m "what you changed" # 3. Save a snapshot with a message
```

Think of it as: **look → stage → save.**

Write commit messages that describe *what* changed, e.g.
`"Add CSV normalizer"`, `"Fix date parsing bug"`.

---

## Looking around

```bash
git status            # What's changed / staged right now
git log --oneline     # List of past commits (newest first)
git log               # Full history with dates and authors
git diff              # Show exact line-by-line changes not yet staged
git diff --staged     # Show changes that ARE staged, before you commit
```

Press `q` to exit a log or diff screen that "takes over" the terminal.

---

## Undoing things (before you've committed)

```bash
git restore FILENAME          # Discard changes to a file (CAREFUL: can't undo)
git restore --staged FILENAME # Unstage a file, but keep your changes
```

## Undoing things (after a commit)

```bash
git revert HEAD               # Make a NEW commit that undoes the last one (safe)
```

`revert` is the safe way to undo — it doesn't rewrite history.
Avoid `git reset --hard` unless you know what it does; it can permanently
delete work.

---

## What git is ignoring (the .gitignore file)

This project ignores (never tracks) the following, so real data and secrets
never get committed by accident:

```
*.csv                          # any CSV file
data/                          # anything in a data/ folder
.streamlit/secrets.toml        # Streamlit secrets
.claude/settings.local.json    # local Claude Code settings
```

To ignore something new, add a line to the `.gitignore` file.

---

## Branches (optional, for later)

A branch lets you try changes without touching your main work.

```bash
git branch                     # List branches (main is your default)
git switch -c my-experiment    # Create and switch to a new branch
git switch main                # Switch back to main
```

---

## Backing up to GitHub (not set up yet)

Right now your commits live **only on this Mac**. To back them up online,
ask Claude: *"help me push this to GitHub"* — it involves creating a remote
repo and running `git push`.

---

## Glossary

| Term          | Meaning |
|---------------|---------|
| **repository / repo** | A folder git is tracking (has a hidden `.git` inside) |
| **stage**     | Mark changes to be included in the next commit (`git add`) |
| **commit**    | A saved snapshot of your staged changes |
| **HEAD**      | The most recent commit on your current branch |
| **working tree** | Your actual files on disk right now |
| **clean**     | No unsaved changes — everything is committed |
| **remote**    | A copy of the repo hosted elsewhere (e.g. GitHub) |
