"""Run the forge.

  git-stream [--config /etc/git-stream/git-stream.toml] [options]

Every setting can be in the config file (see config.py for all of them),
in the environment (GIT_STREAM_*), or on the command line; the command
line wins over the environment, which wins over the file.

With a Zuul API URL, failed jobs are re-run until they failed
max_attempts times, then the merge request is escalated (see retry.py).

The forge keeps its state in <data>/state.db and, when [metrics] is
configured, logs job activity in <data>/metrics.db (both SQLite, both
survive a restart).  See config.py for the [server] and [metrics] options.
"""

import argparse
import logging
import sys

from git_stream import config
from git_stream.forge import Forge
from git_stream.metrics import Metrics
from git_stream.retry import RetryPolicy
from git_stream.server import serve
from git_stream.webhooks import WebhookSender


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
    parser.add_argument('--insecure', action='store_true', default=None,
                        help='demos only: with no tokens, accept any API '
                        'token and an open stream')
    parser.add_argument('--max-body', type=int, metavar='BYTES',
                        help='largest request body accepted')
    parser.add_argument('--read-timeout', type=int, metavar='SECONDS',
                        help='drop a client silent this long')
    parser.add_argument('--body-timeout', type=int, metavar='SECONDS',
                        help='longest a whole request body may take')
    parser.add_argument('--max-connections', type=int, metavar='N',
                        help='most connections served at once; more get a '
                        '503')
    parser.add_argument('--debug', action='store_true', default=None)
    args = parser.parse_args(argv)
    overrides = {
        'server': {'listen': args.listen, 'base_url': args.base_url,
                   'data': args.data,
                   'debug': args.debug, 'insecure': args.insecure,
                   'max_body': args.max_body,
                   'read_timeout': args.read_timeout,
                   'body_timeout': args.body_timeout,
                   'max_connections': args.max_connections},
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


def listen_address(settings):
    """(host, port, base_url) from [server] listen and base_url."""
    server_cfg = settings['server']
    host, _, port = server_cfg['listen'].rpartition(':')
    base_url = server_cfg['base_url'] or 'http://%s:%s' % (
        'localhost' if host in ('', '0.0.0.0') else host, port)
    return host or '0.0.0.0', int(port), base_url


def build_forge(settings, emit=None):
    """The forge, with its tokens; says at startup what is refused."""
    log = logging.getLogger('git_stream')
    server_cfg = settings['server']
    insecure = server_cfg['insecure']
    users = {token: user for user, token in settings['api_tokens'].items()}
    stream_tokens = dict(settings['stream']['tokens'])
    if stream_tokens:
        log.info('POST /api/stream requires a token (%s)',
                 ', '.join(stream_tokens))
    if insecure:
        log.warning('Running in insecure mode: with no tokens, any API token '
                    'is accepted and POST /api/stream is open.  Never use '
                    'this in production.')
    else:
        if not users:
            log.warning('API writes are refused: no [api_tokens] are '
                        'configured; add them, or set [server] insecure = '
                        'true for a demo')
        if not stream_tokens:
            log.warning('POST /api/stream is refused: no [stream] tokens '
                        'are configured; add them, or set [server] insecure '
                        '= true for a demo')
    return Forge(server_cfg['data'], listen_address(settings)[2], emit=emit,
                 users=users, stream_tokens=stream_tokens,
                 report_users=settings['retry']['report_users'],
                 insecure=insecure)


def build_sender(settings, metrics):
    hooks = settings['webhooks']
    return WebhookSender(hooks['urls'], hooks['token'],
                         max_backoff=hooks['max_backoff'], metrics=metrics)


def build_policy(settings, forge, metrics):
    """The retry policy, or None without a Zuul API URL."""
    retry = settings['retry']
    if not retry['zuul_api']:
        return None
    return RetryPolicy(
        forge, retry['zuul_api'], retry['max_attempts'],
        retry['escalate'], retry['report_users'],
        failed_re=retry['failed_pattern'], author=retry['author'],
        settle=retry['settle'], metrics=metrics)


def build_server(settings, forge, sender, metrics):
    host, port, _ = listen_address(settings)
    server_cfg = settings['server']
    return serve(forge, sender, host, port, metrics=metrics,
                 max_body=server_cfg['max_body'],
                 read_timeout=server_cfg['read_timeout'],
                 body_timeout=server_cfg['body_timeout'],
                 max_connections=server_cfg['max_connections'])


def main(argv=None):
    settings = parse_args(argv)
    server_cfg, hooks, retry, metrics_cfg = (
        settings['server'], settings['webhooks'], settings['retry'],
        settings['metrics'])
    logging.basicConfig(
        level=logging.DEBUG if server_cfg['debug'] else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    log = logging.getLogger('git_stream')

    metrics = None
    if metrics_cfg['path']:
        metrics = Metrics(metrics_cfg['path'])
    sender = build_sender(settings, metrics)
    observers = [sender.emit]
    forge = build_forge(
        settings, emit=lambda payload: [f(payload) for f in observers])
    policy = build_policy(settings, forge, metrics)
    if policy:
        observers.append(policy.observe)
        policy.start()
    sender.start()

    server = build_server(settings, forge, sender, metrics)
    log.info('Serving on %s (base URL %s); webhooks to %s',
             server_cfg['listen'], listen_address(settings)[2],
             ', '.join(hooks['urls']) or 'nobody')
    if policy:
        log.info('Retrying failed jobs up to %d runs (Zuul API %s)',
                 retry['max_attempts'], retry['zuul_api'])
    server.serve_forever()


if __name__ == '__main__':
    sys.exit(main())
