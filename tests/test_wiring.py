"""Settings reach the forge: what build_forge (used by main) does with them.

Each of these is a place where a setting could be read correctly by
config.py and then never arrive, which for tokens means a silently open
or silently closed forge.
"""

import logging
import socket
import threading
import urllib.error
import urllib.request

import pytest

from conftest import FAST_POLL
from git_stream import config


@pytest.fixture
def settings(tmp_path):
    """settings(server=..., other=..., environ=...): parsed like main does."""
    count = []

    def load(server='', other='', environ=None):
        path = tmp_path / ('git-stream-%d.toml' % len(count))
        path.write_text(
            '[server]\ndata = "%s"\nbase_url = "http://x"\n'
            'listen = "127.0.0.1:0"\n%s\n%s' % (
                tmp_path / ('data%d' % len(count)), server, other))
        count.append(1)
        return config.load(str(path), environ=environ or {})
    return load


@pytest.fixture
def make(settings):
    from git_stream.__main__ import build_forge
    made = []

    def build(**kw):
        forge = build_forge(settings(**kw))
        made.append(forge)
        return forge
    yield build
    for forge in made:
        forge.close()


def test_api_tokens_map_each_token_to_its_user(make):
    forge = make(other='[api_tokens]\nzuul = "t1"\nbot = "t2"\n')
    assert forge.user_for_token('t1') == 'zuul'
    assert forge.user_for_token('t2') == 'bot'
    assert forge.user_for_token('t3') is None


def test_stream_tokens_arrive(make):
    forge = make(other='[stream.tokens]\nbuilder = "tok-b"\n')
    assert forge.stream_user_for_token('tok-b') == 'builder'
    assert forge.stream_user_for_token('other') is None
    assert forge.stream_user_for_token(None) is None


def test_tokens_from_the_environment_arrive(make):
    forge = make(environ={'GIT_STREAM_API_TOKENS': 'zuul=t1',
                          'GIT_STREAM_STREAM_TOKENS': 'builder=tok-b'})
    assert forge.user_for_token('t1') == 'zuul'
    assert forge.stream_user_for_token('tok-b') == 'builder'


def test_report_users_come_from_the_retry_section(make):
    assert make().report_users == {'zuul'}  # the default
    forge = make(other='[retry]\nreport_users = ["zuul", "zuul-ci"]\n')
    assert forge.report_users == {'zuul', 'zuul-ci'}


def test_insecure_comes_from_the_server_section(make):
    assert make().user_for_token('anything') is None  # secure by default
    forge = make(server='insecure = true')
    assert forge.user_for_token('anything') == 'zuul'
    assert forge.stream_user_for_token(None) == 'git-stream'


def startup_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and r.name.startswith('git_stream')]


def test_startup_says_what_is_refused_and_how_to_allow_it(make, caplog):
    with caplog.at_level(logging.WARNING):
        make()
    warnings = startup_warnings(caplog)
    assert any('API writes' in w and 'refused' in w and '[api_tokens]' in w
               for w in warnings), warnings
    assert any('/api/stream' in w and 'refused' in w and '[stream]' in w
               for w in warnings), warnings


def test_startup_warns_loudly_about_insecure_mode(make, caplog):
    with caplog.at_level(logging.WARNING):
        make(server='insecure = true')
    assert any('insecure mode' in w and 'production' in w
               for w in startup_warnings(caplog))


def test_a_fully_configured_forge_starts_quietly(make, caplog):
    with caplog.at_level(logging.WARNING):
        make(other='[api_tokens]\nzuul = "t1"\n[stream.tokens]\nb = "t2"\n')
    assert startup_warnings(caplog) == []


# -- the other things main builds ----------------------------------------------

def test_webhook_settings_reach_the_sender(settings):
    from git_stream.__main__ import build_sender
    sender = build_sender(settings(other=(
        '[webhooks]\nurls = ["http://zuul/a", "http://zuul/b"]\n'
        'token = "hook-token"\nmax_backoff = 7\n')), metrics=None)
    assert sender.urls == ['http://zuul/a', 'http://zuul/b']
    assert sender.token == 'hook-token'
    assert sender.max_backoff == 7


def test_retry_settings_reach_the_policy(settings, make):
    from git_stream.__main__ import build_policy
    forge = make()
    assert build_policy(settings(), forge, metrics=None) is None  # no API
    policy = build_policy(settings(other=(
        '[retry]\nzuul_api = "http://zuul/api/tenant/t"\nmax_attempts = 5\n'
        'report_users = ["zuul", "ci"]\nfailed_pattern = "^FAILED"\n'
        'author = "retrier"\nsettle = 9\n'
        'escalate = ["http://jira/hook"]\n')), forge, metrics=None)
    assert policy.zuul_api == 'http://zuul/api/tenant/t'
    assert policy.max_attempts == 5
    assert policy.report_users == {'zuul', 'ci'}
    assert policy.failed_re.pattern == '^FAILED'
    assert policy.author == 'retrier'
    assert policy.settle == 9
    assert policy.escalate_urls == ['http://jira/hook']


@pytest.fixture
def served(settings, make):
    """served(limits): a running server built from settings, as main does."""
    from git_stream.__main__ import build_server
    started = []

    def start(limits):
        server_options = 'insecure = true\n' + limits
        server = build_server(settings(server=server_options),
                              make(server='insecure = true'), sender=None,
                              metrics=None)
        threading.Thread(target=server.serve_forever, kwargs=FAST_POLL,
                         daemon=True).start()
        started.append(server)
        return 'http://127.0.0.1:%d' % server.server_address[1]
    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def test_request_limits_reach_the_server(served):
    url = served('max_body = 300\nread_timeout = 1')
    req = urllib.request.Request(
        url + '/api/stream', data=b'x' * 301, method='POST',
        headers={'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 413
    host, port = url.rsplit('//', 1)[1].split(':')
    with socket.create_connection((host, int(port)), timeout=5) as idle:
        idle.sendall(b'POST /api/stream HTTP/1.1\r\nHost: x\r\n'
                     b'Content-Length: 50\r\n\r\nabc')  # then silence
        assert b' 408 ' in idle.recv(4096).split(b'\r\n')[0]
