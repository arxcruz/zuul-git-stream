import pytest

from git_stream import config
from git_stream.__main__ import parse_args


def write(tmp_path, text):
    path = tmp_path / 'git-stream.toml'
    path.write_text(text)
    return str(path)


def test_defaults(tmp_path):
    s = config.load(environ={})
    assert s['server']['listen'] == '0.0.0.0:8090'
    assert s['retry']['zuul_api'] is None
    assert s['retry']['max_attempts'] == 3
    assert s['retry']['report_users'] == ['zuul']


def test_file_then_environment_then_command_line(tmp_path):
    path = write(tmp_path, '''
[server]
base_url = "http://git-stream:8090"
seed = "/repos"

[api_tokens]
zuul = "file-token"

[webhooks]
urls = ["http://zuul/payload"]
token = "file-hook-token"

[retry]
zuul_api = "http://zuul/api/tenant/t"
max_attempts = 5
escalate = ["http://jira-adapter/hook"]
''')
    s = config.load(path, environ={})
    assert s['server']['seed'] == '/repos'
    assert s['api_tokens'] == {'zuul': 'file-token'}
    assert s['webhooks']['urls'] == ['http://zuul/payload']
    assert s['retry']['max_attempts'] == 5
    assert s['retry']['escalate'] == ['http://jira-adapter/hook']

    env = {'GIT_STREAM_MAX_ATTEMPTS': '2',
           'GIT_STREAM_WEBHOOKS': 'http://a http://b',
           'GIT_STREAM_API_TOKENS': 'bot=env-token'}
    s = config.load(path, environ=env)
    assert s['retry']['max_attempts'] == 2
    assert s['webhooks']['urls'] == ['http://a', 'http://b']
    assert s['api_tokens'] == {'zuul': 'file-token', 'bot': 'env-token'}

    s = config.load(path, environ=env,
                    overrides={'retry': {'max_attempts': 7, 'zuul_api': None}})
    assert s['retry']['max_attempts'] == 7
    assert s['retry']['zuul_api'] == 'http://zuul/api/tenant/t'


def test_config_path_from_environment(tmp_path):
    path = write(tmp_path, '[retry]\nmax_attempts = 4\n')
    s = config.load(environ={'GIT_STREAM_CONFIG': path})
    assert s['retry']['max_attempts'] == 4


@pytest.mark.parametrize('text, error', [
    ('[nope]\nx = 1\n', 'unknown section'),
    ('[retry]\nmax_retries = 1\n', 'unknown option'),
    ('[retry]\nmax_attempts = "many"\n', 'expected int'),
    ('[webhooks]\nurls = "http://a"\nurls = 1\n', 'Cannot overwrite'),
])
def test_bad_files(tmp_path, text, error):
    with pytest.raises(config.ConfigError, match=error):
        config.load(write(tmp_path, text), environ={})


def test_command_line(tmp_path, monkeypatch):
    monkeypatch.delenv('GIT_STREAM_CONFIG', raising=False)
    path = write(tmp_path, '[retry]\nmax_attempts = 4\n'
                 '[api_tokens]\nzuul = "t1"\n')
    s = parse_args(['--config', path, '--max-attempts', '6',
                    '--api-token', 'bot=t2', '--webhook', 'http://w'])
    assert s['retry']['max_attempts'] == 6
    assert s['api_tokens'] == {'zuul': 't1', 'bot': 't2'}
    assert s['webhooks']['urls'] == ['http://w']
    with pytest.raises(SystemExit):
        parse_args(['--config', str(tmp_path / 'missing.toml')])


def test_insecure_on_the_command_line(monkeypatch):
    monkeypatch.delenv('GIT_STREAM_CONFIG', raising=False)
    assert parse_args([])['server']['insecure'] is False
    assert parse_args(['--insecure'])['server']['insecure'] is True


@pytest.mark.parametrize('name', ['max_body', 'read_timeout',
                                  'body_timeout', 'max_connections'])
def test_a_limit_below_one_is_refused(tmp_path, name):
    path = write(tmp_path, '[server]\n%s = 0\n' % name)
    with pytest.raises(config.ConfigError, match='%s must be at least 1' % name):
        config.load(path, environ={})


def test_request_limits(tmp_path):
    s = config.load(environ={})
    assert s['server']['max_body'] == 10 * 1024 * 1024
    assert s['server']['read_timeout'] == 30
    path = write(tmp_path, '[server]\nmax_body = 2048\nread_timeout = 5\n')
    s = config.load(path, environ={})
    assert (s['server']['max_body'], s['server']['read_timeout']) == (2048, 5)
    s = config.load(path, environ={'GIT_STREAM_MAX_BODY': '4096',
                                   'GIT_STREAM_READ_TIMEOUT': '7'})
    assert (s['server']['max_body'], s['server']['read_timeout']) == (4096, 7)


def test_max_connections_defaults_to_64():
    assert config.load(environ={})['server']['max_connections'] == 64


def test_max_connections_file_then_environment_then_command_line(
        tmp_path, monkeypatch):
    monkeypatch.delenv('GIT_STREAM_CONFIG', raising=False)
    monkeypatch.delenv('GIT_STREAM_MAX_CONNECTIONS', raising=False)
    path = write(tmp_path, '[server]\nmax_connections = 10\n')
    assert config.load(path, environ={})['server']['max_connections'] == 10
    s = config.load(path, environ={'GIT_STREAM_MAX_CONNECTIONS': '20'})
    assert s['server']['max_connections'] == 20
    monkeypatch.setenv('GIT_STREAM_MAX_CONNECTIONS', '20')
    assert parse_args(['--config', path])['server']['max_connections'] == 20
    s = parse_args(['--config', path, '--max-connections', '30'])
    assert s['server']['max_connections'] == 30


@pytest.mark.parametrize('value', ['-3', '0'])
def test_max_connections_below_one_is_refused_from_the_environment(value):
    with pytest.raises(config.ConfigError,
                       match='max_connections must be at least 1'):
        config.load(environ={'GIT_STREAM_MAX_CONNECTIONS': value})


def test_max_connections_must_be_a_number(tmp_path):
    path = write(tmp_path, '[server]\nmax_connections = "many"\n')
    with pytest.raises(config.ConfigError, match='expected int'):
        config.load(path, environ={})
    with pytest.raises(config.ConfigError, match='expected int'):
        config.load(environ={'GIT_STREAM_MAX_CONNECTIONS': 'many'})


def test_insecure_is_off_unless_asked_for(tmp_path):
    assert config.load(environ={})['server']['insecure'] is False
    path = write(tmp_path, '[server]\ninsecure = true\n')
    assert config.load(path, environ={})['server']['insecure'] is True
    assert config.load(environ={'GIT_STREAM_INSECURE': 'true'})[
        'server']['insecure'] is True


def test_stream_tokens_come_from_the_file_and_the_environment(tmp_path):
    path = write(tmp_path, '[stream.tokens]\nalice = "file-token"\n')
    s = config.load(path, environ={})
    assert s['stream']['tokens'] == {'alice': 'file-token'}
    s = config.load(path, environ={
        'GIT_STREAM_STREAM_TOKENS': 'bob=env-b alice=env-a'})
    assert s['stream']['tokens'] == {'alice': 'env-a', 'bob': 'env-b'}


def test_stream_tokens_from_the_environment_alone():
    s = config.load(environ={'GIT_STREAM_STREAM_TOKENS': 'alice=tok-a'})
    assert s['stream']['tokens'] == {'alice': 'tok-a'}
    assert s['api_tokens'] == {}  # the two kinds of token stay apart


def test_settings_do_not_leak_from_one_load_to_the_next():
    """The defaults are shared objects; filling in tokens must not edit them."""
    config.load(environ={'GIT_STREAM_STREAM_TOKENS': 'alice=tok-a',
                         'GIT_STREAM_API_TOKENS': 'zuul=tok-z'})
    again = config.load(environ={})
    assert again['stream']['tokens'] == {}
    assert again['api_tokens'] == {}
