"""The /spec project skill: .claude/skills/spec/SKILL.md.

A skill is a prompt, so these tests check what can be checked without a
model: the frontmatter (user-invoked only, runs inline, narrow tools), that
every command it injects is allowed, that every file it tells the model to
read exists, and that the spec format it asks for is the one of
docs/specs/callback-url.md, the template.  They cannot show that Claude Code
accepts the `allowed-tools` patterns or that the interview goes well; that is
a manual dry run of `/spec` on `main` (docs/backlog.md, section 4).
"""

import fnmatch
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, '.claude', 'skills', 'spec', 'SKILL.md')
REVIEW_SKILL = os.path.join(ROOT, '.claude', 'skills', 'review', 'SKILL.md')
AGENT = os.path.join(ROOT, '.claude', 'agents', 'branch-reviewer.md')
TEMPLATE = os.path.join(ROOT, 'docs', 'specs', 'callback-url.md')
AGENTS_MD = os.path.join(ROOT, 'AGENTS.md')


def read(path):
    assert os.path.exists(path), (
        '%s does not exist yet' % os.path.relpath(path, ROOT))
    with open(path) as f:
        return f.read()


def split(path):
    """The frontmatter as a dict of strings, and the body."""
    parts = read(path).split('---\n', 2)
    assert parts[0] == '', 'a skill starts with a --- frontmatter block'
    meta = {}
    for line in parts[1].splitlines():
        key, _, value = line.partition(':')
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2]


@pytest.fixture(scope='module')
def skill():
    return split(SKILL)


def allowed(meta):
    return re.findall(r'\w+(?:\([^)]*\))?', meta['allowed-tools'])


def test_it_is_named_spec_and_says_what_it_does(skill):
    meta, _ = skill
    assert meta['name'] == 'spec'
    assert meta['description']
    assert 'idea' in meta['argument-hint']


def test_only_the_user_can_invoke_it(skill):
    # It creates a branch and writes a file, and it interviews the user.
    meta, _ = skill
    assert meta['disable-model-invocation'] == 'true'


def test_it_runs_inline_because_it_asks_questions_and_writes(skill):
    # /review forks into a read-only subagent; a fork cannot hold the
    # one-question-at-a-time conversation or write the spec.
    meta, _ = skill
    assert 'context' not in meta
    assert 'agent' not in meta
    review_meta, _ = split(REVIEW_SKILL)
    assert review_meta['context'] == 'fork'


def test_its_tools_are_narrow(skill):
    meta, _ = skill
    tools = allowed(meta)
    assert tools
    for tool in tools:
        assert tool not in ('Bash', 'Bash(*)', 'Write', 'Edit'), (
            '%s is not narrow' % tool)
        if tool.startswith(('Write', 'Edit')):
            assert tool.endswith('(docs/specs/*)'), tool
    for tool in tools:
        for forbidden in ('commit', 'push', 'merge', 'reset', 'rm ',
                          'checkout', 'add'):
            assert forbidden not in tool, tool
    assert 'AskUserQuestion' in tools
    assert 'Write(docs/specs/*)' in tools


def test_every_injected_command_is_allowed(skill):
    meta, body = skill
    commands = re.findall(r'!`([^`]+)`', body)
    assert commands, 'the skill injects the branch and the tree state'
    patterns = [t[len('Bash('):-1] for t in allowed(meta)
                if t.startswith('Bash(')]
    for command in commands:
        assert any(fnmatch.fnmatchcase(command, p) for p in patterns), (
            '%r is not in allowed-tools' % command)
    joined = ' '.join(commands)
    assert 'git branch --show-current' in joined
    assert 'git status --short' in joined
    assert 'ls docs/specs' in joined


def test_it_reads_what_the_repo_already_says(skill):
    _, body = skill
    for path in ('docs/roadmap.md', 'docs/backlog.md', 'docs/decisions.md',
                 'tests/pending.txt', 'docs/specs'):
        assert path in body, path


def test_every_file_it_names_exists(skill):
    _, body = skill
    named = set(re.findall(r'(?:docs|tests)/[\w./-]+\.(?:md|txt)', body))
    assert named
    for path in named:
        if '<' in path:
            continue
        assert os.path.exists(os.path.join(ROOT, path)), path


def test_it_asks_for_the_sections_of_the_template_in_order(skill):
    # docs/specs/callback-url.md is the format; if its sections change, the
    # skill must change with it.
    _, body = skill
    with open(TEMPLATE) as f:
        headings = re.findall(r'^## (.+)$', f.read(), re.M)
    assert headings[:3] == ['Purpose', 'Scenarios', 'Fires on']
    position = -1
    for heading in headings:
        found = body.find(heading, position + 1)
        assert found > position, '%r missing or out of order' % heading
        position = found
    assert 'callback-url' in body, 'it points at the template by name'


def test_it_asks_one_question_at_a_time_with_a_recommendation(skill):
    _, body = skill
    text = ' '.join(body.split()).lower()
    assert 'one question at a time' in text
    assert 'never' in text and 'several' in text
    assert 'recommend' in text
    assert 'AskUserQuestion' in body


def test_it_refuses_a_dirty_tree_or_a_branch_other_than_main(skill):
    _, body = skill
    text = ' '.join(body.split()).lower()
    assert 'refuse' in text
    assert 'main' in text
    assert 'uncommitted' in text or 'dirty' in text
    assert 'say why' in text or 'explain why' in text


def test_it_saves_to_a_feat_branch_and_never_commits_or_overwrites(skill):
    _, body = skill
    text = ' '.join(body.split()).lower()
    assert 'git switch -c feat/<name>' in body
    assert 'docs/specs/<name>.md' in body
    assert 'do not commit' in text or 'never commit' in text
    assert 'overwrite' in text


def test_it_stops_after_the_spec_and_offers_a_plan(skill):
    _, body = skill
    text = ' '.join(body.split()).lower()
    assert 'plan' in text
    assert 'no code' in text and 'no tests' in text


def test_its_naming_rule_is_the_one_review_relies_on(skill):
    # /review and the branch-reviewer find the spec of feat/<name> at
    # docs/specs/<name>.md.
    _, body = skill
    with open(AGENT) as f:
        agent = f.read()
    assert 'docs/specs/<name>.md' in agent
    assert 'branch name without its `<type>/` prefix' in agent
    assert 'feat/<name>' in body and 'docs/specs/<name>.md' in body


def test_agents_md_lists_the_skill_and_the_workflow_step():
    text = read(AGENTS_MD)
    layout = text.split('## Repository layout')[1].split('```')[1]
    assert 'skills/spec/' in layout
    workflow = text.split('## Workflow')[1].split('## Security rules')[0]
    assert '/spec' in workflow
