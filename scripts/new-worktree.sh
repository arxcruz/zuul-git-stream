#!/bin/sh
# Make a sibling worktree on a new branch, with its own .venv, so a second
# Claude Code session can work in parallel without touching this checkout.
#
#   scripts/new-worktree.sh <type>/<topic>
#
# The worktree goes next to the main checkout as <repo>-<type>-<topic>, on a
# new branch started from main (wherever you run this from).  Its .venv is
# separate because .venv is git-ignored and its editable install points at one
# checkout.  The setup is the one in AGENTS.md ("Setting up and testing");
# NO_ZUUL=1 leaves out the Zuul install (a slow download from opendev.org)
# when you only need to run the tests that don't import Zuul.
#
# Refuses, creating nothing, if the branch or the folder already exists.  If
# an install fails, the worktree is kept and the cleanup commands are printed;
# nothing is ever deleted here.
set -eu

fail() {
    echo "new-worktree: $*" >&2
    exit 1
}

if [ $# -ne 1 ]; then
    echo "usage: $0 <type>/<topic>" >&2
    exit 2
fi
branch=$1

# A leading '-' must never become an option; the rest is git's own rules.
case $branch in
    '' | -*) fail "'$branch' is not a usable branch name (empty or starts with '-')" ;;
esac
git check-ref-format "refs/heads/$branch" ||
    fail "'$branch' is not a valid branch name"
git rev-parse --git-dir >/dev/null 2>&1 || fail "run this inside the repository"
command -v uv >/dev/null 2>&1 ||
    fail "uv is not installed (see AGENTS.md, Setting up and testing)"

# The first entry of the list is the main checkout, even from a linked worktree.
main_dir=$(git worktree list --porcelain | sed -n '1s/^worktree //p')
target=$(dirname "$main_dir")/$(basename "$main_dir")-$(printf %s "$branch" | tr / -)

if git show-ref --verify --quiet "refs/heads/$branch"; then
    fail "the branch '$branch' already exists; pick another name"
fi
if [ -e "$target" ] || [ -L "$target" ]; then
    fail "$target already exists; remove it or pick another branch name"
fi

if [ "$(git -C "$main_dir" branch --show-current)" = main ]; then
    echo "warning: the original checkout ($main_dir) is on main, and git lets" \
        "only one worktree have main checked out, so /merge cannot switch to" \
        "it from a worktree.  Run 'git switch --detach' in $main_dir." >&2
fi

# Start from main.  A clone that never checked main out has only origin/main,
# and `worktree add -b X PATH main` then makes a local main and ignores X, so
# the start point is resolved to a commit here.
base=$(git -C "$main_dir" rev-parse --verify --quiet refs/heads/main^{commit} ||
    git -C "$main_dir" rev-parse --verify --quiet \
        refs/remotes/origin/main^{commit}) ||
    fail "there is no main or origin/main branch to start from"
git -C "$main_dir" worktree add -b "$branch" --end-of-options "$target" "$base"
[ "$(git -C "$target" branch --show-current)" = "$branch" ] ||
    fail "git put $target on a different branch than '$branch'; remove it with: git worktree remove $target"
cd "$target"

# Run one setup step; if it fails, say how to finish or clean up and stop.
step() {
    "$@" || {
        status=$?
        echo "new-worktree: '$*' failed in $target." >&2
        echo "The worktree and the branch were kept.  Fix the problem and run" \
            "the rest of the AGENTS.md setup there, or remove them with:" >&2
        echo "  git worktree remove $target && git branch -d $branch" >&2
        exit "$status"
    }
}

step uv venv -p 3.12 .venv
step uv pip install -p .venv/bin/python -e '.[test,lint]'
if [ -z "${NO_ZUUL:-}" ]; then
    step uv pip install -p .venv/bin/python 'git+https://opendev.org/zuul/zuul'
fi

echo "Created $target on branch $branch."
echo "Start a session there:  cd $target && claude"
