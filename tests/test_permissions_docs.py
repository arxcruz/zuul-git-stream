"""The permissions and sandbox documentation: AGENTS.md's "Permissions and
sandbox" section, decision 15 and the backlog items about the push guard and
the /merge allow patterns.

Docs tests in the style of tests/test_worktree_docs.py: they cannot say the
prose is good, only that it exists, says what a person changing
`.claude/settings.json` needs to know, and does not point at missing files.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_MD = os.path.join(ROOT, 'AGENTS.md')
DECISIONS = os.path.join(ROOT, 'docs', 'decisions.md')
BACKLOG = os.path.join(ROOT, 'docs', 'backlog.md')
SETTINGS = os.path.join(ROOT, '.claude', 'settings.json')


def read(path):
    assert os.path.exists(path), (
        '%s does not exist yet' % os.path.relpath(path, ROOT))
    with open(path) as f:
        return f.read()


def words(body):
    return ' '.join(body.split()).lower()


def section():
    text = read(AGENTS_MD)
    heading = '\n## Permissions and sandbox\n'
    assert heading in text, 'AGENTS.md has no "Permissions and sandbox" heading'
    return text.split(heading)[1].split('\n## ')[0]


def decision_15():
    text = read(DECISIONS)
    assert re.search(r'^## 15\. ', text, re.M), 'no decision 15'
    return words(text.split('## 15. ')[1])


def backlog_items():
    return re.split(r'\n(?=- \[S\d)', read(BACKLOG))


def test_agents_md_has_a_permissions_and_sandbox_heading():
    assert section().strip()


def test_every_path_the_section_mentions_exists():
    paths = {path.rstrip('.') for path in re.findall(
        r'(?:scripts|docs|tests|devenv|\.claude)/[\w./-]*\w', section())}
    assert '.claude/settings.json' in paths
    for path in paths:
        assert os.path.exists(os.path.join(ROOT, path)), path


def test_the_layout_list_has_the_docs_test():
    layout = read(AGENTS_MD).split('## Repository layout')[1].split('```')[1]
    assert 'test_permissions_docs.py' in layout


def test_the_section_names_what_the_settings_enable():
    text = words(section())
    for setting in ('sandbox', 'failifunavailable', 'allowlocalbinding'):
        assert setting in text, setting
    assert 'unsandboxed' in text and 'no unsandboxed retry' in text


def test_the_section_lists_the_four_deny_rules():
    text = section()
    settings = json.loads(read(SETTINGS))
    deny = settings['permissions']['deny']
    assert len(deny) == 4
    for rule in deny:
        assert rule in text, rule


def test_settings_load_only_at_session_start():
    text = words(section())
    assert 'only at session start' in text
    assert 'restart' in text


def test_scripts_and_the_demo_run_from_a_terminal_not_the_agent():
    text = words(section())
    assert 'scripts/new-worktree.sh' in text and 'demo' in text
    assert 'from a terminal' in text
    assert 'excludedcommands' in text and 'edit an excluded script' in text


def test_the_section_records_the_probe_findings():
    text = words(section())
    assert 'compound commands are denied' in text
    assert 'git -c . push' in text and "bash -c '" in text
    assert 'speed bump' in text and 'the sandbox is the boundary' in text


def test_decision_15_gives_the_choices_and_the_untested_remote():
    entry = decision_15()
    for term in ('sandbox', 'failifunavailable', 'excludedcommands',
                 'allowlocalbinding', 'deny rules'):
        assert term in entry, term
    assert 'speed bump' in entry
    assert 'untested' in entry and 'real remote' in entry


def test_backlog_has_an_s2_item_for_the_push_guard():
    found = [item for item in backlog_items()
             if re.match(r'- \[S2\]', item) and 'push guard' in item.lower()]
    assert found, 'no [S2] backlog item about a push guard'
    item = words(found[0])
    assert 'no-commit-on-main.py' in item
    assert '-c' in item and 'bash -c' in item
    assert 'real remote' in item and 'sandbox' in item


def test_the_merge_item_separates_deny_rules_from_allow_patterns():
    found = [item for item in backlog_items()
             if '`/merge` skill' in item and 'allowed-tools' in item]
    assert found, 'no backlog item about the /merge skill patterns'
    item = words(found[0])
    assert 'verified for deny rules' in item
    assert 'unchecked for' in item and 'allow patterns' in item


def test_the_protected_path_merge_item_is_done_and_the_probe_item_exists():
    # The S2 item "/merge cannot merge a branch that changes a protected
    # path" was implemented (docs/specs/merge-protected-path-check.md), so it
    # must be gone; what is left is the S3 item to probe the inferred names.
    assert not [item for item in backlog_items()
                if re.match(r'- \[S2\]', item) and 'protected path' in item]
    found = [item for item in backlog_items()
             if re.match(r'- \[S3\]', item) and '.mcp.json' in item
             and 'probe' in item.lower()]
    assert found, 'no [S3] backlog item to probe the inferred protected names'
    item = words(found[0])
    assert 'real `/merge`' in item and 'inferred' in item


def test_the_section_says_the_skill_checks_protected_paths_now():
    text = words(section())
    assert 'git diff --name-only --no-renames main...head' in text
    assert 'merge from a terminal outside claude code' in text
    assert 'not check for it yet' not in text
    assert 'observed' in text and 'inferred' in text
