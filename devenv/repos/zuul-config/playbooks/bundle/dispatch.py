"""Decide which bundle tests to run for a bundle merge request.

  dispatch.py ZUUL_API FORGE_API PROJECT MR PIPELINE MANIFEST CHILD_JOBS_JSON

MANIFEST is the merge request's bundle.yaml (id, flaky).  In the
bundle-rerun pipeline, the newest "rerun" comment on the merge request
says what to run:

  rerun failed     only the jobs whose latest result for this merge
                   request is not SUCCESS
  rerun all        all of them
  rerun JOB...     these jobs (the "bundle-" prefix is optional)

and may end with flaky=N to change the chance (0-1) that a test hits a
"network problem".  Anywhere else, all tests run.

Prints a JSON object with child_jobs, bundle_id, flaky and a report.
"""

import json
import re
import sys
import urllib.parse
import urllib.request

import yaml

# Results that don't say whether a job works for this bundle.
NOT_A_VERDICT = {'SKIPPED', 'RETRY', None}
RERUN_RE = re.compile(r'^\s*rerun\b(.*)$', re.I | re.S)


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def latest_results(zuul_api, project, mr):
    """Latest result per job for this merge request (newest first)."""
    query = urllib.parse.urlencode({'project': project, 'change': mr,
                                    'complete': 'true', 'limit': 500})
    results = {}
    for build in get('%s/builds?%s' % (zuul_api, query)):
        if build['result'] not in NOT_A_VERDICT:
            results.setdefault(build['job_name'], build['result'])
    return results


def rerun_comment(forge_api, project, mr):
    """The newest "rerun ..." comment on the merge request."""
    notes = get('%s/projects/%s/merge_requests/%s/notes?sort=desc' % (
        forge_api, urllib.parse.quote(project, safe=''), mr))
    for note in notes:
        m = RERUN_RE.match(note['body'])
        if m:
            return note, m.group(1).split()
    return None, []


def main():
    (zuul_api, forge_api, project, mr, pipeline, manifest,
     child_jobs) = sys.argv[1:]
    child_jobs = json.loads(child_jobs)
    with open(manifest) as f:
        bundle = yaml.safe_load(f) or {}
    bundle_id = str(bundle.get('id', 'mr-%s' % mr))
    flaky = float(bundle.get('flaky', 0.3))
    report = ['bundle %s (%s!%s, pipeline %s)' % (bundle_id, project, mr,
                                                  pipeline)]

    words = []
    if pipeline == 'bundle-rerun':
        note, words = rerun_comment(forge_api, project, mr)
        if note:
            report.append('asked by %s: "%s"' % (note['author']['username'],
                                                 note['body'].strip()))
    for word in list(words):
        if word.lower().startswith('flaky='):
            flaky = float(word.split('=', 1)[1])
            words.remove(word)

    if words and words[0].lower() == 'failed':
        results = latest_results(zuul_api, project, mr)
        selected = [j for j in child_jobs if results.get(j) != 'SUCCESS']
        for job in child_jobs:
            report.append('%-18s last result %-8s -> %s' % (
                job, results.get(job, 'never ran'),
                'run again' if job in selected else 'skip'))
        if not selected:
            report.append('nothing failed, nothing to re-run')
    elif words and words[0].lower() != 'all':
        wanted = {w if w.startswith('bundle-') else 'bundle-' + w
                  for w in words}
        selected = [j for j in child_jobs if j in wanted]
        report.append('explicit list: %s' % (', '.join(selected) or 'none'))
        unknown = sorted(wanted - set(child_jobs))
        if unknown:
            report.append('ignoring unknown jobs: %s' % ', '.join(unknown))
    else:
        selected = child_jobs
        report.append('running all %d tests' % len(selected))
    report.append('network flakiness %d%%' % round(flaky * 100))

    print(json.dumps({
        'child_jobs': selected,
        'bundle_id': bundle_id,
        'flaky': flaky,
        'report': report,
    }))


if __name__ == '__main__':
    main()
