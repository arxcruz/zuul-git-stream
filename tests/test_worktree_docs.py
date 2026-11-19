"""The worktree documentation: AGENTS.md's "Worktrees" section, the layout
list, the decision record and the backlog item.

These are docs tests in the style of tests/test_merge_skill.py: they cannot
say the prose is good, only that it exists, says the things a person running
parallel sessions needs, and does not point at files that are not there.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_MD = os.path.join(ROOT, 'AGENTS.md')
DECISIONS = os.path.join(ROOT, 'docs', 'decisions.md')
BACKLOG = os.path.join(ROOT, 'docs', 'backlog.md')


def read(path):
    assert os.path.exists(path), (
        '%s does not exist yet' % os.path.relpath(path, ROOT))
    with open(path) as f:
        return f.read()


def words(body):
    return ' '.join(body.split()).lower()


def worktrees_section():
    text = read(AGENTS_MD)
    assert '\n## Worktrees\n' in text, 'AGENTS.md has no "Worktrees" heading'
    return text.split('\n## Worktrees\n')[1].split('\n## ')[0]


def test_agents_md_has_a_worktrees_heading():
    assert worktrees_section().strip()


def test_every_path_the_section_mentions_exists():
    section = worktrees_section()
    paths = {path.rstrip('.') for path in re.findall(
        r'(?:scripts|docs|tests|devenv|\.claude)/[\w./-]*\w', section)}
    assert 'scripts/new-worktree.sh' in paths
    for path in paths:
        assert os.path.exists(os.path.join(ROOT, path)), path


def test_the_layout_list_has_the_script_and_the_tests():
    layout = read(AGENTS_MD).split('## Repository layout')[1].split('```')[1]
    assert 'scripts/new-worktree.sh' in layout
    assert 'test_new_worktree.py' in layout
    assert 'test_worktree_docs.py' in layout


def test_worktrees_live_next_to_the_repo_and_are_made_by_the_script():
    text = words(worktrees_section())
    assert 'next to' in text
    assert 'scripts/new-worktree.sh' in text


def test_each_worktree_has_its_own_venv_that_is_never_shared():
    text = words(worktrees_section())
    assert 'own `.venv`' in text and 'never shared' in text


def test_one_gate_and_one_merge_at_a_time():
    text = words(worktrees_section())
    assert 'one gate at a time' in text
    assert 'one branch at a time' in text
    # Why: main can be checked out in one worktree only.
    assert 'main' in text and 'only one worktree' in text
    assert 'git switch main' in text


def test_the_original_checkout_stays_detached_and_the_hook_says_so():
    text = words(worktrees_section())
    assert 'git switch --detach' in text
    assert 'detached head' in text
    assert 'no-commit-on-main.py' in text and 'refuses' in text


def test_the_stop_hook_blocks_when_the_venv_is_missing():
    text = words(worktrees_section())
    assert 'stop-checks.py' in text
    assert 'blocks' in text and 'venv is missing' in text


def test_the_demo_runs_in_one_worktree_only():
    text = words(worktrees_section())
    assert 'demo' in text and 'one worktree only' in text
    for port in ('8080', '8090', '9000', '8000'):
        assert port in text, port


def test_any_non_empty_no_zuul_skips_zuul_even_zero():
    text = words(worktrees_section())
    assert 'no_zuul' in text
    assert 'non-empty' in text and 'no_zuul=0' in text


def test_decisions_records_the_layout_and_the_detached_head_choice():
    assert re.search(r'^## 14\. ', read(DECISIONS), re.M)
    entry = words(read(DECISIONS).split('## 14. ')[1])
    assert 'worktree' in entry and 'sibling' in entry
    assert 'detached head' in entry


def test_backlog_has_an_item_for_the_merge_leftover_on_main():
    text = read(BACKLOG)
    items = re.split(r'\n(?=- \[S\d\])', text)
    found = [item for item in items
             if '/merge' in item and 'worktree' in item
             and re.match(r'- \[S\d\]', item)]
    assert found, 'no [S?] backlog item about /merge in a worktree'
    item = words(found[0])
    assert 'left on `main`' in item or 'leaves the session on `main`' in item
    assert '.claude/skills/merge/skill.md' in item
