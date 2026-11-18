"""Settings: defaults, then the config file, then the environment, then
the command line (each overrides the one before).

The config file is TOML, by default /etc/git-stream/git-stream.toml:

  [server]
  listen = "0.0.0.0:8090"
  base_url = "http://git-stream:8090"   # how Zuul reaches git-stream
  data = "/srv/git-stream"              # repos/ + state.db (forge) + metrics.db
  seed = "/repos"                       # create projects from these dirs

  [api_tokens]                          # user = token; tokens for writes
  zuul = "secret-api-token"

  [stream]                              # POST /api/stream; open unless a
                                        # token is set
  build-system = "secret-stream-token"

  [webhooks]
  urls = ["http://zuul-web:9000/api/connection/gitlab/payload"]
  token = "secret-webhook-token"        # sent as X-Gitlab-Token
  max_backoff = 30                      # seconds between redeliveries

  [retry]                               # off without zuul_api
  zuul_api = "http://zuul-web:9000/api/tenant/main"
  max_attempts = 3                      # runs of a failing job, first included
  report_users = ["zuul"]               # whose comments are Zuul's reports
  failed_pattern = '^\\s*Build failed'   # a report that says "failed"
  author = "git-stream"                 # who posts the reruns
  escalate = []                         # URLs to POST escalations to
  settle = 2                            # seconds before reading results

  [metrics]                             # what the jobs did, in a database
  path = ""                             # SQLite file ("" = <data>/metrics.db)
  build_url = ""                        # prefix for build links; "" = none
"""

import os
import tomllib

DEFAULT_PATH = '/etc/git-stream/git-stream.toml'

# section -> option -> (default, type, environment variable)
SCHEMA = {
    'server': {
        'listen': ('0.0.0.0:8090', str, 'GIT_STREAM_LISTEN'),
        'base_url': (None, str, 'GIT_STREAM_BASE_URL'),
        'data': ('/srv/git-stream', str, 'GIT_STREAM_DATA'),
        'seed': (None, str, 'GIT_STREAM_SEED'),
        'debug': (False, bool, 'GIT_STREAM_DEBUG'),
    },
    'api_tokens': {},
    # user = token; POST /api/stream requires one when any are configured.
    # With none, the stream is open (fine for a demo; set tokens in
    # production so only the announcing system can drive the forge).
    'stream': {
        'tokens': ({}, dict, 'GIT_STREAM_STREAM_TOKENS'),
    },
    'webhooks': {
        'urls': ([], list, 'GIT_STREAM_WEBHOOKS'),
        'token': ('', str, 'GIT_STREAM_WEBHOOK_TOKEN'),
        'max_backoff': (30, int, 'GIT_STREAM_WEBHOOK_MAX_BACKOFF'),
    },
    'retry': {
        'zuul_api': (None, str, 'GIT_STREAM_ZUUL_API'),
        'max_attempts': (3, int, 'GIT_STREAM_MAX_ATTEMPTS'),
        'report_users': (['zuul'], list, 'GIT_STREAM_REPORT_USERS'),
        'failed_pattern': (r'^\s*Build failed', str,
                           'GIT_STREAM_FAILED_PATTERN'),
        'author': ('git-stream', str, 'GIT_STREAM_RETRY_AUTHOR'),
        'escalate': ([], list, 'GIT_STREAM_ESCALATE'),
        'settle': (2, int, 'GIT_STREAM_RETRY_SETTLE'),
    },
    'metrics': {
        # Where the metrics database lives; empty = <data>/metrics.db.
        'path': ('', str, 'GIT_STREAM_METRICS_PATH'),
        # Prefix for per-build links (e.g. "http://zuul-web:9000/build");
        # empty leaves the build_url column empty.
        'build_url': ('', str, 'GIT_STREAM_METRICS_BUILD_URL'),
    },
}


class ConfigError(Exception):
    pass


def _convert(value, kind, where):
    if kind is dict:
        if isinstance(value, dict) and all(
                isinstance(v, str) for v in value.values()):
            return value
    elif kind is list:
        if isinstance(value, str):
            return value.split()
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value
    elif kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in (
                'true', '1', 'yes', 'false', '0', 'no', ''):
            return value.lower() in ('true', '1', 'yes')
    elif kind is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lstrip('-').isdigit():
            return int(value)
    elif isinstance(value, str):
        return value
    raise ConfigError('%s: expected %s, got %r' % (where, kind.__name__,
                                                   value))


def load(path=None, environ=None, overrides=None):
    """Return the settings as {section: {option: value}}.

    path is the config file; None means GIT_STREAM_CONFIG or the default
    path, if they exist.  overrides holds command line values, as
    {section: {option: value}}, with None meaning "not given".
    """
    environ = os.environ if environ is None else environ
    settings = {section: {name: (list(d) if isinstance(d, list) else d)
                          for name, (d, _, _) in options.items()}
                for section, options in SCHEMA.items()}

    if path is None:
        path = environ.get('GIT_STREAM_CONFIG')
        if path is None and os.path.exists(DEFAULT_PATH):
            path = DEFAULT_PATH
    if path:
        try:
            with open(path, 'rb') as f:
                data = tomllib.load(f)
        except OSError as e:
            raise ConfigError('cannot read %s: %s' % (path, e.strerror))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError('%s: %s' % (path, e))
        for section, values in data.items():
            if section not in SCHEMA or not isinstance(values, dict):
                raise ConfigError('%s: unknown section [%s]' % (path,
                                                                section))
            for name, value in values.items():
                where = '%s: [%s] %s' % (path, section, name)
                if section == 'api_tokens':
                    settings[section][name] = _convert(value, str, where)
                    continue
                if name not in SCHEMA[section]:
                    raise ConfigError('%s: unknown option' % where)
                kind = SCHEMA[section][name][1]
                settings[section][name] = _convert(value, kind, where)

    for section, options in SCHEMA.items():
        for name, (_, kind, var) in options.items():
            if environ.get(var) is not None:
                settings[section][name] = _convert(environ[var], kind, var)
    # Map tokens from the environment: "user=token user=token".
    for section, var in (('api_tokens', 'GIT_STREAM_API_TOKENS'),
                         ('git', 'GIT_STREAM_GIT_TOKENS'),
                         ('stream', 'GIT_STREAM_STREAM_TOKENS')):
        for item in (environ.get(var) or '').split():
            user, _, token = item.partition('=')
            if user and token:
                settings[section][user] = token

    for section, values in (overrides or {}).items():
        for name, value in values.items():
            if value is not None:
                settings[section][name] = value
    return settings
