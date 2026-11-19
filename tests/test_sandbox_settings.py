"""The sandbox and permission settings in .claude/settings.json.

Like tests/test_merge_skill.py these are config tests: they cannot show the
sandbox works, only that the settings that turn it on are there, that nothing
punches a hole through it, and that the commands the project never wants run
unattended stay denied.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(ROOT, '.claude', 'settings.json')

# Commands that lose work or leave the machine; denied whatever else is
# allowed (AGENTS.md: never push unasked, no destructive git).
DENIED = ['Bash(git push *)', 'Bash(git reset --hard *)',
          'Bash(git clean *)', 'Bash(git branch -D *)']


def settings():
    with open(SETTINGS) as f:
        return json.load(f)


def sandbox():
    section = settings().get('sandbox')
    assert isinstance(section, dict), (
        'settings.json has no "sandbox" section yet')
    return section


def test_the_settings_file_parses():
    assert isinstance(settings(), dict)


def test_the_sandbox_is_enabled():
    assert sandbox().get('enabled') is True


def test_it_fails_rather_than_running_without_a_sandbox():
    assert sandbox().get('failIfUnavailable') is True


def test_unsandboxed_commands_are_not_allowed():
    # Explicitly false: an absent key leaves the escape hatch to the default.
    assert sandbox().get('allowUnsandboxedCommands') is False


def test_no_command_is_excluded_from_the_sandbox():
    assert 'excludedCommands' not in sandbox()


def test_local_binding_is_allowed_for_the_tests_and_the_demo():
    # The forge fixture binds servers to port 0 on localhost.
    assert sandbox().get('network', {}).get('allowLocalBinding') is True


def test_the_destructive_git_commands_are_denied():
    deny = settings().get('permissions', {}).get('deny', [])
    missing = [rule for rule in DENIED if rule not in deny]
    assert not missing, 'permissions.deny lacks %s' % missing


def test_no_allow_rule_is_broader_than_one_tool_or_command():
    allow = settings().get('permissions', {}).get('allow', [])
    too_broad = []
    for rule in allow:
        tool, _, argument = rule.partition('(')
        argument = argument.rstrip(')')
        if rule.strip() in ('*', 'Bash') or tool.strip() == '*':
            too_broad.append(rule)
        elif tool == 'Bash' and re.fullmatch(r'[\s*:]*', argument):
            too_broad.append(rule)  # Bash(*), Bash(**), Bash(:*), Bash()
    assert not too_broad, 'allow rules too broad: %s' % too_broad


def test_the_existing_hooks_are_still_registered():
    hooks = settings()['hooks']
    pre = [h['command'] for g in hooks['PreToolUse'] if g['matcher'] == 'Bash'
           for h in g['hooks']]
    stop = [h['command'] for g in hooks['Stop'] for h in g['hooks']]
    prefix = '${CLAUDE_PROJECT_DIR}/.claude/hooks/'
    assert prefix + 'no-commit-on-main.py' in pre
    assert prefix + 'stop-checks.py' in stop


# Guard: these weaken the sandbox, so the project settings never set them.
# It passes before the sandbox block exists and keeps it from growing one.
def test_no_setting_weakens_the_sandbox():
    section = settings().get('sandbox', {})
    weakening = ['enableWeakerNestedSandbox', 'enableWeakerNetworkIsolation',
                 'allowAllUnixSockets', 'allowAppleEvents']
    set_here = [key for key in weakening if key in section]
    if 'disabled' in section.get('filesystem', {}):
        set_here.append('filesystem.disabled')
    assert not set_here, 'sandbox settings that weaken it: %s' % set_here
