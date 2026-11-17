"""Run the forge.

  git-stream [--config /etc/git-stream/git-stream.toml] [options]

Every setting can be in the config file (see config.py for all of them),
in the environment (GIT_STREAM_*), or on the command line; the command
line wins over the environment, which wins over the file.

With a Zuul API URL, failed jobs are re-run until they failed
max_attempts times, then the merge request is escalated (see retry.py).
"""

import argparse
import logging
import os
import sys

from git_stream import config
from git_stream.forge import Forge
from git_stream.retry import RetryPolicy
from git_stream.server import serve
from git_stream.webhooks import WebhookSender


def seed(forge, root):
    """Create a project from each directory in root that doesn't exist."""
    for name in sorted(os.listdir(root)):
        src = os.path.join(root, name)
        if not os.path.isdir(src) or name in forge.state['projects']:
            continue
        files = {}
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames
                           if d not in ('.git', '__pycache__')]
            for f in filenames:
                full = os.path.join(dirpath, f)
                with open(full, 'rb') as fh:
                    files[os.path.relpath(full, src)] = fh.read()
        forge.create_project(name, files)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='git-stream', description='A GitLab-compatible forge whose '
        'events come from other systems.  Options override the config file.')
    parser.add_argument('--config', help='TOML config file (default: '
                        '$GIT_STREAM_CONFIG or %s)' % config.DEFAULT_PATH)
    parser.add_argument('--listen', help='address:port to serve on')
    parser.add_argument('--base-url', help='URL Zuul uses to reach this '
                        'server')
    parser.add_argument('--data', help='directory for repositories and state')
    parser.add_argument('--seed', help='create projects from the '
                        'directories in this one')
    parser.add_argument('--webhook', action='append', metavar='URL',
                        help='send webhooks here (repeatable)')
    parser.add_argument('--webhook-token', help='sent as X-Gitlab-Token')
    parser.add_argument('--api-token', action='append',
                        metavar='USER=TOKEN', help='API token (repeatable)')
    parser.add_argument('--stream-token', action='append',
                        metavar='USER=TOKEN',
                        help='token required on POST /api/stream (repeatable)')
    parser.add_argument('--zuul-api', help="Zuul's tenant API URL; turns on "
                        'retries, e.g. http://zuul:9000/api/tenant/main')
    parser.add_argument('--max-attempts', type=int,
                        help='runs of a failing job before escalating')
    parser.add_argument('--escalate', action='append', metavar='URL',
                        help='POST escalations here (repeatable)')
    parser.add_argument('--report-user', action='append', metavar='USER',
                        help="whose comments are Zuul's reports (repeatable)")
    parser.add_argument('--debug', action='store_true', default=None)
    args = parser.parse_args(argv)
    overrides = {
        'server': {'listen': args.listen, 'base_url': args.base_url,
                   'data': args.data, 'seed': args.seed,
                   'debug': args.debug},
        'webhooks': {'urls': args.webhook, 'token': args.webhook_token},
        'retry': {'zuul_api': args.zuul_api,
                  'max_attempts': args.max_attempts,
                  'escalate': args.escalate,
                  'report_users': args.report_user},
        'api_tokens': dict(item.partition('=')[::2]
                           for item in args.api_token or []),
        'stream': {'tokens': dict(item.partition('=')[::2]
                                  for item in args.stream_token or [])},
    }
    try:
        return config.load(args.config, overrides=overrides)
    except config.ConfigError as e:
        parser.exit(2, 'git-stream: %s\n' % e)


def main(argv=None):
    settings = parse_args(argv)
    server_cfg, hooks, retry = (settings['server'], settings['webhooks'],
                                settings['retry'])
    log = logging.getLogger('git_stream')
    logging.basicConfig(
        level=logging.DEBUG if server_cfg['debug'] else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    host, _, port = server_cfg['listen'].rpartition(':')
    base_url = server_cfg['base_url'] or 'http://%s:%s' % (
        'localhost' if host in ('', '0.0.0.0') else host, port)
    users = {token: user for user, token in settings['api_tokens'].items()}
    stream_tokens = dict(settings['stream']['tokens'])
    if stream_tokens:
        log.info('POST /api/stream requires a token (%s)',
                 ', '.join(stream_tokens))
    else:
        log.warning('POST /api/stream is open; set [stream] tokens to '
                    'require one')

    sender = WebhookSender(hooks['urls'], hooks['token'],
                           max_backoff=hooks['max_backoff'])
    observers = [sender.emit]
    forge = Forge(server_cfg['data'], base_url, users=users,
                  stream_tokens=stream_tokens,
                  emit=lambda payload: [f(payload) for f in observers])
    if retry['zuul_api']:
        policy = RetryPolicy(
            forge, retry['zuul_api'], retry['max_attempts'],
            retry['escalate'], retry['report_users'],
            failed_re=retry['failed_pattern'], author=retry['author'],
            settle=retry['settle'])
        observers.append(policy.observe)
        policy.start()
    if server_cfg['seed']:
        seed(forge, server_cfg['seed'])
    sender.start()

    server = serve(forge, sender, host or '0.0.0.0', int(port))
    log.info('Serving on %s (base URL %s); webhooks to %s',
             server_cfg['listen'], base_url,
             ', '.join(hooks['urls']) or 'nobody')
    if retry['zuul_api']:
        log.info('Retrying failed jobs up to %d runs (Zuul API %s)',
                 retry['max_attempts'], retry['zuul_api'])
    server.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
