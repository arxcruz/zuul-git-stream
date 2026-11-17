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
