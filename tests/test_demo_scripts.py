"""The demo's own scripts, run without podman or Zuul.

gitserver.py serves Zuul's config repository (the demo's stand-in for the
real config repos), seed.py announces the starting state, and the demo UI
turns clicks into messages.  Message-only: none of them puts a file in
git-stream.
"""

import http.server
import importlib.util
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

DEVENV = os.path.join(os.path.dirname(__file__), '..', 'devenv')


def load(name, path):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(DEVENV, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args, cwd=None):
    return subprocess.run(['git'] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def gitserver():
    return load('demo_gitserver', 'gitserver.py')


@pytest.fixture
def served(gitserver, tmp_path):
    """A git server over a source tree with one project, on a free port."""
    src, root = tmp_path / 'src', tmp_path / 'git'
    (src / 'zuul-config' / 'zuul.d').mkdir(parents=True)
    (src / 'zuul-config' / 'zuul.d' / 'a.yaml').write_text('- a\n')
    gitserver.sync(str(src), str(root))
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), gitserver.handler(str(root)))
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    yield src, root, 'http://127.0.0.1:%d' % server.server_address[1]
    server.shutdown()
    server.server_close()


def test_the_config_repository_is_served_over_http(served, tmp_path):
    src, root, url = served
    work = tmp_path / 'work'
    git('clone', '-q', url + '/zuul-config', str(work))
    assert (work / 'zuul.d' / 'a.yaml').read_text() == '- a\n'


def test_the_config_repository_is_read_only(served, tmp_path):
    src, root, url = served
    work = tmp_path / 'work'
    git('clone', '-q', url + '/zuul-config', str(work))
    (work / 'x').write_text('x')
    git('add', 'x', cwd=work)
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m',
        'x', cwd=work)
    with pytest.raises(subprocess.CalledProcessError):
        git('push', 'origin', 'HEAD:master', cwd=work)
    assert git('--git-dir', str(root / 'zuul-config'), 'rev-list',
               '--count', 'master') == '1'


def test_a_change_to_the_source_is_a_new_commit_and_no_change_is_none(
        served, gitserver, tmp_path):
    src, root, url = served
    repo = str(root / 'zuul-config')
    first = git('--git-dir', repo, 'rev-parse', 'master')
    gitserver.sync(str(src), str(root))  # a restart with nothing changed
    assert git('--git-dir', repo, 'rev-parse', 'master') == first
    (src / 'zuul-config' / 'zuul.d' / 'a.yaml').write_text('- b\n')
    (src / 'zuul-config' / '__pycache__').mkdir()
    (src / 'zuul-config' / '__pycache__' / 'x.pyc').write_bytes(b'\x00')
    gitserver.sync(str(src), str(root))
    second = git('--git-dir', repo, 'rev-parse', 'master')
    assert second != first
    assert git('--git-dir', repo, 'rev-parse', second + '^') == first
    assert git('--git-dir', repo, 'ls-tree', '-r', '--name-only',
               'master') == 'zuul.d/a.yaml'


def test_seed_announces_the_starting_state_and_is_safe_to_repeat(forge):
    seed = load('demo_seed', 'seed.py')
    seed.FORGE = forge.url
    seed.main()
    seed.main()
    for project in ('bundles', 'ci-tool'):
        with urllib.request.urlopen(
                '%s/api/v4/projects/%s' % (forge.url, project)):
            pass
    with urllib.request.urlopen(forge.url +
                                '/api/v4/projects/ci-tool/data') as r:
        assert json.load(r)['data'] == {'broken_jobs': ['bundle-security']}
    pushes = [m for m in forge.forge.messages({'action': 'push',
                                               'project': 'ci-tool'})]
    assert len(pushes) == 1  # the second run found the data and sent nothing


@pytest.fixture
def ui(forge, monkeypatch):
    monkeypatch.setenv('FORGE', forge.url)
    app = load('demo_ui_app', 'demo-ui/app.py')
    seed = load('demo_seed2', 'seed.py')
    seed.FORGE = forge.url
    seed.main()
    return app


def data_of(forge, path):
    with urllib.request.urlopen(forge.url + '/api/v4/projects/' + path) as r:
        return json.load(r)['data']


def test_the_ui_announces_a_bundle_as_data_not_files(forge, ui):
    key, msg = ui.bundle_message({'action': 'new', 'flaky': 0.5})
    assert 'files' not in msg
    assert 'error' not in ui.send(key, msg)
    assert data_of(forge, 'bundles/merge_requests/1/data') == {
        'id': key, 'flaky': 0.5,
        'images': ['registry.local/api:' + key,
                   'registry.local/worker:' + key]}


def test_the_ui_walks_the_ci_tool_fix_through_data(forge, ui):
    assert ui.ci_tool()['broken_jobs'] == ['bundle-security']
    for action in ('fix-open', 'fix-merge'):
        entry = ui.send(*ui.tool_message({'action': action}))
        assert 'error' not in entry, entry
    state = ui.ci_tool()
    assert state['broken_jobs'] == [] and state['fix']['state'] == 'merged'
    assert 'error' not in ui.send(*ui.tool_message({'action': 'break'}))
    assert ui.ci_tool()['broken_jobs'] == ['bundle-security']


def test_the_ui_reads_a_ci_tool_that_announced_nothing(forge, monkeypatch):
    monkeypatch.setenv('FORGE', forge.url)
    app = load('demo_ui_app2', 'demo-ui/app.py')
    forge.send({'action': 'create_project', 'project': 'ci-tool'})
    assert app.ci_tool()['broken_jobs'] == []


def test_dispatch_reads_the_bundle_from_the_forge(forge):
    dispatch = load('demo_dispatch',
                    'repos/zuul-config/playbooks/bundle/dispatch.py')
    mr = forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                     'key': 'b-1', 'data': {'id': 'b-1', 'flaky': 0.0}})
    api = forge.url + '/api/v4'
    assert dispatch.bundle_data(api, 'bundles', mr['iid'], mr['sha']) == {
        'id': 'b-1', 'flaky': 0.0}
    assert dispatch.bundle_data(api, 'bundles', mr['iid'], '0' * 40) == {}
    assert dispatch.bundle_data(api, 'missing', 1, 'x') == {}
    with pytest.raises(urllib.error.URLError):  # a forge that is down
        dispatch.bundle_data('http://127.0.0.1:9/api/v4', 'bundles', 1, 'x')


@pytest.mark.parametrize('length', ['abc', '-1', str(10 * 1024 * 1024 + 1)])
def test_the_config_server_refuses_a_bad_content_length(served, length):
    import socket
    _, _, url = served
    host, port = url[len('http://'):].split(':')
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(('POST /zuul-config/git-upload-pack HTTP/1.0\r\n'
                   'Content-Type: application/x-git-upload-pack-request\r\n'
                   'Content-Length: %s\r\n\r\n' % length).encode())
        reply = s.recv(200)
    assert reply.startswith(b'HTTP/1.0 400'), reply
