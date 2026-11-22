"""Serve the demo's config repositories over git smart HTTP, read only.

  gitserver.py SRC ROOT [PORT]

Each directory in SRC (here: zuul-config, the Zuul pipelines and jobs) is
committed into a bare repository ROOT/<name>, once per change of its files,
and served at http://<host>:PORT/<name>.  It stands in for the real
repositories that hold Zuul's config in production: git-stream never holds
job definitions.  Zuul reads it through its plain `git` driver.

Standard library plus git.  Pushing is refused.
"""

import http.server
import os
import subprocess
import sys

AUTHOR = {'GIT_AUTHOR_NAME': 'demo', 'GIT_AUTHOR_EMAIL': 'demo@example.com',
          'GIT_COMMITTER_NAME': 'demo',
          'GIT_COMMITTER_EMAIL': 'demo@example.com'}


def git(repo, *args, env=None, input=None):
    done = subprocess.run(['git', '--git-dir', repo] + list(args),
                          env=dict(os.environ, **(env or {})), input=input,
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def sync(src, root):
    """Commit the files of src into the bare repository root/<name>."""
    for name in sorted(os.listdir(src)):
        tree = os.path.join(src, name)
        if not os.path.isdir(tree):
            continue
        repo = os.path.join(root, name)
        if not os.path.exists(os.path.join(repo, 'HEAD')):
            subprocess.run(['git', 'init', '-q', '--bare', '-b', 'master',
                            repo], check=True)
        with open(os.path.join(repo, 'info', 'exclude'), 'w') as f:
            f.write('__pycache__/\n')
        index = os.path.join(repo, 'demo-index')
        env = {'GIT_INDEX_FILE': index, 'GIT_WORK_TREE': tree}
        if os.path.exists(index):
            os.unlink(index)
        git(repo, 'add', '-A', '--', '.', env=env)
        new = git(repo, 'write-tree', env=env)
        os.unlink(index)
        head = subprocess.run(
            ['git', '--git-dir', repo, 'rev-parse', '-q', '--verify',
             'refs/heads/master'], capture_output=True, text=True)
        old = head.stdout.strip()
        if old and git(repo, 'rev-parse', old + '^{tree}') == new:
            print('%s is up to date' % name, flush=True)
            continue
        args = ['commit-tree', new, '-m', 'Demo config'] + (
            ['-p', old] if old else [])
        commit = git(repo, *args, env=AUTHOR)
        git(repo, 'update-ref', 'refs/heads/master', commit)
        print('%s: committed %s' % (name, commit[:8]), flush=True)


def handler(root):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, fmt, *args):
            pass

        def _serve(self):
            path, _, query = self.path.partition('?')
            if 'receive-pack' in path or 'receive-pack' in query:
                return self._send(403, b'read only\n', 'text/plain', {})
            body = b''
            if self.command == 'POST':
                body = self.rfile.read(int(
                    self.headers.get('Content-Length', 0)))
            env = dict(os.environ, GIT_PROJECT_ROOT=root,
                       GIT_HTTP_EXPORT_ALL='1', PATH_INFO=path,
                       QUERY_STRING=query, REQUEST_METHOD=self.command,
                       CONTENT_TYPE=self.headers.get('Content-Type', ''),
                       CONTENT_LENGTH=str(len(body)),
                       REMOTE_ADDR=self.client_address[0])
            if self.headers.get('Content-Encoding'):
                env['HTTP_CONTENT_ENCODING'] = self.headers[
                    'Content-Encoding']
            if self.headers.get('Git-Protocol'):
                env['GIT_PROTOCOL'] = self.headers['Git-Protocol']
            proc = subprocess.run(['git', 'http-backend'], input=body,
                                  env=env, capture_output=True)
            head, sep, out = proc.stdout.partition(b'\r\n\r\n')
            if not sep:
                head, _, out = proc.stdout.partition(b'\n\n')
            code, headers = 200, {}
            for line in head.decode('latin1').splitlines():
                key, _, value = line.partition(':')
                if key.lower() == 'status':
                    code = int(value.split()[0])
                elif key:
                    headers[key] = value.strip()
            ctype = headers.pop('Content-Type', 'text/plain')
            headers.pop('Content-Length', None)
            self._send(code, out, ctype, headers)

        def _send(self, code, body, ctype, headers):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _serve
    return Handler


def main():
    src, root = sys.argv[1], sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 8091
    os.makedirs(root, exist_ok=True)
    sync(src, root)
    print('Serving %s on port %d' % (root, port), flush=True)
    http.server.ThreadingHTTPServer(('0.0.0.0', port),
                                    handler(root)).serve_forever()


if __name__ == '__main__':
    main()
