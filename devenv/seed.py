"""Announce the demo's starting state to git-stream, then exit.

  seed.py [FORGE]

Creates the two placeholder projects Zuul's tenant needs (bundles, ci-tool)
and sends ci-tool's first state as data: the tool has a known bug that makes
bundle-security crash.  Safe to run again: it changes nothing that exists.
Zuul's scheduler waits until ci-tool has data, so the tenant loads after
this ran.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

FORGE = (sys.argv[1] if len(sys.argv) > 1 else
         os.environ.get('FORGE', 'http://git-stream:8090'))
TOOL_BROKEN = {'broken_jobs': ['bundle-security']}


def call(path, msg=None):
    req = urllib.request.Request(
        FORGE + path, method='POST' if msg is not None else 'GET',
        data=None if msg is None else json.dumps(msg).encode('utf8'),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main():
    for _ in range(60):
        try:
            call('/health')
            break
        except OSError:
            time.sleep(2)
    else:
        sys.exit('git-stream is not answering at %s' % FORGE)
    for project in ('bundles', 'ci-tool'):
        call('/api/stream', {'action': 'create_project', 'project': project})
    try:
        call('/api/v4/projects/ci-tool/data')
        print('ci-tool already has data', flush=True)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        call('/api/stream', {'action': 'push', 'project': 'ci-tool',
                             'author': 'ci-tool-dev', 'data': TOOL_BROKEN,
                             'message': 'ci-tool: known bug in '
                                        'parse_manifest()'})
        print('ci-tool: announced its first state', flush=True)


if __name__ == '__main__':
    main()
