# ci-tool

The tool the bundle tests run.  `tool.yaml` lists the jobs a known bug
makes crash; a fix is a merge request that empties the list.  A bundle
can be tested with an unmerged fix by adding `Depends-On: <fix URL>` to
its description.
