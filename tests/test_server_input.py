"""Malformed requests are the client's mistake: 4xx, never a 500.

A 500 also leaks the exception text, so these also stand in for "errors say
what to do".  The git-over-HTTP tests check that the read-only rule holds
however the request is spelled.
"""

import socket
import urllib.error
import urllib.request

import pytest

from conftest import API_TOKEN


def call(forge, path, method='GET', data=None, token=API_TOKEN):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(forge.url + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def raw_status(forge, request):
    """Send raw bytes (for framing urllib won't produce); the status."""
    port = int(forge.url.rsplit(':', 1)[1])
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(request)
        reply = s.recv(4096)
    return int(reply.split(b' ', 2)[1])


@pytest.fixture
def mr(forge):
    forge.send({'action': 'open_mr', 'project': 'bundles', 'title': 'T',
                'key': 'k', 'files': {'a.yaml': 'a: 1\n'}})
    return '/api/v4/projects/bundles/merge_requests/1'


def assert_client_error(status, body):
    assert 400 <= status < 500, (status, body)


@pytest.mark.parametrize('body', [b'{nope', b'[1]', b'null', b'"text"'])
def test_bad_json_bodies_are_client_errors(forge, mr, body):
    assert_client_error(*call(forge, mr + '/notes', 'POST', body))


@pytest.mark.parametrize('query', [
    'repository/branches?page=abc', 'repository/branches?per_page=abc',
    'merge_requests?per_page=abc'])
def test_non_numeric_paging_is_a_client_error(forge, query):
    assert_client_error(*call(
        forge, '/api/v4/projects/bundles/' + query))


@pytest.mark.parametrize('tail', ['', '/notes', '/approvals'])
def test_non_numeric_merge_request_id_is_a_client_error(forge, tail):
    assert_client_error(*call(
        forge, '/api/v4/projects/bundles/merge_requests/abc' + tail))


@pytest.mark.parametrize('size', [b'zz', b'-5', b''])
@pytest.mark.parametrize('path', ['/api/stream', '/api/v4/projects/bundles'
                                  '/merge_requests/1/notes'])
def test_bad_chunked_framing_is_a_client_error(forge, mr, path, size):
    request = (b'POST ' + path.encode() + b' HTTP/1.1\r\nHost: x\r\n'
               b'Authorization: Bearer ' + API_TOKEN.encode() + b'\r\n'
               b'Content-Type: application/json\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n' + size +
               b'\r\nabc\r\n0\r\n\r\n')
    status = raw_status(forge, request)
    assert 400 <= status < 500, status


@pytest.mark.parametrize('request_path, method', [
    ('/bundles.git/info/refs?service=git-receive-pack', 'GET'),
    ('/bundles.git/info/refs?service=git%2Dreceive%2Dpack', 'GET'),
    ('/bundles.git/git-receive-pack', 'POST'),
    ('/bundles.git/git%2Dreceive%2Dpack', 'POST')])
def test_git_receive_pack_is_refused_however_it_is_spelled(
        forge, request_path, method):
    status, body = call(forge, request_path, method,
                        b'' if method == 'POST' else None, token=None)
    assert status in (403, 404), (status, body)
    assert '# service=git-receive-pack' not in body


def test_git_upload_pack_is_still_served(forge):
    status, body = call(forge, '/bundles.git/info/refs'
                        '?service=git-upload-pack', token=None)
    assert status == 200
    assert body.startswith('001e# service=git-upload-pack')


# -- what an error says --------------------------------------------------------

def test_a_server_error_does_not_say_what_went_wrong(forge, mr):
    """The exception goes to the log; the client gets a fixed message."""
    def boom(*args):
        raise RuntimeError('secret detail at /srv/git-stream/x')
    forge.forge.notes = boom
    status, body = call(forge, mr + '/notes')
    assert status == 500
    assert 'RuntimeError' not in body and 'secret detail' not in body
    assert 'internal error' in body


@pytest.mark.parametrize('body', [b'{nope', b'null', b'"text"', b'42'])
def test_a_bad_stream_body_is_a_client_error_that_says_little(forge, body):
    status, text = call(forge, '/api/stream', 'POST', body)
    assert 400 <= status < 500, (status, text)
    assert 'Expecting' not in text and 'Error' not in text


@pytest.mark.parametrize('body', [b'{nope', b'[1]', b'null'])
def test_a_bad_api_body_does_not_leak_the_parser(forge, mr, body):
    status, text = call(forge, mr + '/notes', 'POST', body)
    assert status == 400
    assert 'Expecting' not in text and 'Error' not in text


def test_a_body_that_is_not_utf8_is_a_client_error(forge, mr):
    req = urllib.request.Request(
        forge.url + mr + '/notes', data=b'body=\xff\xfe', method='POST',
        headers={'Authorization': 'Bearer ' + API_TOKEN,
                 'Content-Type': 'application/x-www-form-urlencoded'})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


@pytest.mark.parametrize('query', [
    'repository/branches?page=0', 'repository/branches?per_page=-1',
    'merge_requests?per_page=0',
    'merge_requests?per_page=99999999999999999999999'])
def test_paging_must_be_a_positive_number(forge, query):
    status, body = call(forge, '/api/v4/projects/bundles/' + query)
    assert status == 400, (status, body)


def test_a_huge_merge_request_id_is_a_client_error(forge):
    status, body = call(forge, '/api/v4/projects/bundles/merge_requests/'
                        + '9' * 5000)
    assert 400 <= status < 500, (status, body)
