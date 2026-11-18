"""stream.apply decides who a message acts as, without any HTTP.

The spec for the rules themselves is test_stream_identity.py (over HTTP);
these pin the choke point's own behavior: the sender is an argument, a
refused message leaves no trace, and a malformed author is a client error.
"""

import json
import urllib.error

import pytest

from git_stream import stream
from git_stream.forge import Forge, ForgeError

NOTE = {'action': 'comment', 'project': 'bundles', 'mr': 'k', 'note': 'hi'}


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**kw):
        forge = Forge(str(tmp_path / ('data%d' % len(made))), 'http://x', **kw)
        forge.create_project('bundles', {'README.md': 'x\n'})
        forge.open_mr('bundles', 'T', {'a.yaml': 'a: 1\n'}, 'git-stream',
                      key='k')
        made.append(forge)
        return forge
    yield build
    for forge in made:
        forge.close()


def authors(forge):
    mr = forge.mr(forge.project('bundles'), 'k')
    return [n['author'] for n in mr['notes']]


def test_the_sender_is_the_default_author(make):
    forge = make(stream_tokens={'builder': 'tok'})
    stream.apply(forge, dict(NOTE), 'builder')
    assert authors(forge) == ['builder']


def test_a_sender_may_repeat_its_own_name(make):
    forge = make(stream_tokens={'builder': 'tok'})
    stream.apply(forge, dict(NOTE, author='builder'), 'builder')
    assert authors(forge) == ['builder']


def test_another_name_is_refused_with_tokens(make):
    forge = make(stream_tokens={'builder': 'tok', 'bot': 'tok2'})
    with pytest.raises(ForgeError) as e:
        stream.apply(forge, dict(NOTE, author='bot'), 'builder')
    assert e.value.status == 403
    assert str(e.value) == "token for 'builder' may not act as 'bot'"
    assert authors(forge) == []


def test_tokens_refuse_even_a_name_nobody_reserved(make):
    forge = make(stream_tokens={'builder': 'tok'}, report_users=('zuul',))
    with pytest.raises(ForgeError) as e:
        stream.apply(forge, dict(NOTE, author='someone'), 'builder')
    assert e.value.status == 403


def test_open_stream_refuses_reserved_names_and_allows_the_rest(make):
    forge = make(report_users=('zuul',))
    with pytest.raises(ForgeError) as e:
        stream.apply(forge, dict(NOTE, author='zuul'), 'git-stream')
    assert e.value.status == 403 and 'reserved' in str(e.value)
    stream.apply(forge, dict(NOTE, author='alice'), 'git-stream')
    assert authors(forge) == ['alice']


def test_nothing_is_reserved_by_default(make):
    forge = make()
    stream.apply(forge, dict(NOTE, author='zuul'), 'git-stream')
    assert authors(forge) == ['zuul']


def test_a_refused_message_does_not_use_up_its_id(make):
    forge = make(report_users=('zuul',))
    msg = dict(NOTE, id='m-1')
    with pytest.raises(ForgeError):
        stream.apply(forge, dict(msg, author='zuul'), 'git-stream')
    # The sender fixes the author and resends: applied, not a duplicate.
    assert 'duplicate' not in stream.apply(forge, msg, 'git-stream')
    assert authors(forge) == ['git-stream']
    assert stream.apply(forge, msg, 'git-stream') == {
        'duplicate': True, 'id': 'm-1'}


@pytest.mark.parametrize('author', [['zuul'], 7, {'name': 'zuul'}])
def test_an_author_that_is_not_a_string_is_a_client_error(make, author):
    forge = make(report_users=('zuul',))
    with pytest.raises(ForgeError) as e:
        stream.apply(forge, dict(NOTE, author=author), 'git-stream')
    assert e.value.status == 400
    assert authors(forge) == []


def test_over_http_a_bad_author_is_400_not_a_crash(make_forge):
    forge = make_forge(report_users=('zuul',))
    with pytest.raises(urllib.error.HTTPError) as e:
        forge.send(dict(NOTE, author=['zuul']))
    assert e.value.code == 400
    assert 'author' in json.load(e.value)['message']
