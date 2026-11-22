"""Senders send messages, never files.

"files" in a message is a 400 that says to send "data", before anything
is created, and the `[server] seed` setting is gone.
"""

import json
import urllib.error

import pytest

from git_stream import config

MESSAGES = {
    'create_project': {'action': 'create_project', 'project': 'p2'},
    'push': {'action': 'push', 'project': 'bundles', 'data': {'a': 1}},
    'open_mr': {'action': 'open_mr', 'project': 'p2', 'title': 'T'},
    'update_mr': {'action': 'update_mr', 'project': 'bundles', 'mr': 'k'},
}


def refused(forge, msg):
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send(msg)
    return e.value.code, json.load(e.value)['message']


@pytest.mark.parametrize('action', sorted(MESSAGES))
@pytest.mark.parametrize('files', [{'a.yaml': 'a: 1\n'}, {}, 'x', []])
def test_files_are_a_400_that_says_to_send_data(forge, action, files):
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                'key': 'k', 'data': {'v': 1}})
    before = (sorted(forge.forge.state['projects']),
              forge.forge.state['projects']['bundles']['next_iid'],
              len(forge.hooks))
    code, message = refused(forge, dict(MESSAGES[action], files=files))
    assert code == 400
    assert '"files"' in message and '"data"' in message
    assert (sorted(forge.forge.state['projects']),
            forge.forge.state['projects']['bundles']['next_iid'],
            len(forge.hooks)) == before  # nothing was created or sent


def test_a_null_files_is_the_same_as_none(forge):
    mr = forge.send({'action': 'open_mr', 'project': 'bundles',
                     'title': 'T', 'files': None, 'data': {'a': 1}})
    assert mr['state'] == 'opened'


def test_the_refusal_is_in_the_message_log(forge):
    refused(forge, {'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                    'files': {'a': 'b'}})
    rows = forge.forge.messages({'outcome': 'rejected'})
    assert rows and rows[0]['status'] == 400
    assert 'data' in rows[0]['detail']


def test_the_seed_setting_is_gone(tmp_path):
    path = tmp_path / 'g.toml'
    path.write_text('[server]\nseed = "/repos"\n')
    with pytest.raises(Exception, match='seed'):
        config.load(str(path), environ={})
