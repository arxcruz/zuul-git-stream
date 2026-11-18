# CI

There is no CI configuration in this repository yet (nothing has run on a
CI platform, and the platform may change).  The gate is the local command

```sh
scripts/check.sh
```

which is what a CI run would do.  When CI is added, port the two jobs below.

## Jobs

Run both on every push and every merge/pull request.  Avoid duplicate
pipelines for a branch that has an open merge request.

**lint**

```sh
pip install '.[lint]'          # ruff, pinned in pyproject.toml
ruff check .
```

**test**

```sh
pip install -e '.[test]'
git ls-remote https://opendev.org/zuul/zuul HEAD    # log the Zuul commit tested
pip install 'git+https://opendev.org/zuul/zuul'
pytest                                              # includes the contract tests
```

## Notes

- Use a full Python 3.12 image, not a slim one: git-stream needs
  `git` >= 2.38 (Debian bookworm has 2.39), and Zuul's dependencies need a
  recent Python.  (An old system Python failed to resolve Zuul's `boto3`.)
- CI installs Zuul's **latest** commit on purpose, so contract drift shows
  up as a red `test` job.  A red run can therefore mean Zuul changed; the
  logged commit says which.  The demo keeps its own pin
  (`devenv/common.sh`).
- The contract tests fail, not skip, when Zuul isn't importable
  (`tests/test_zuul_contract.py`); never run `pytest -m "not contract"` in
  CI.
- No `PBR_VERSION` workaround is needed to install Zuul from git.
- Verified locally in clean Python 3.12 venvs: both jobs pass (35 tests,
  against Zuul `faa679f`).  The platform-specific parts (pipeline rules,
  caching, runner access to opendev.org) have not been exercised.
