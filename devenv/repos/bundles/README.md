# bundles

Stands in for the container bundles under test.  Each bundle is a merge
request that adds `bundle.yaml`; Zuul tests it, comments the results and
approves it when everything passes.  Comment `rerun failed`, `rerun all`
or `rerun <job>...` to run tests again.
