#!/bin/bash
# One-shot setup: ZooKeeper TLS certs in /var/certs (Zuul requires TLS to
# ZooKeeper).  The git repositories are created by git-stream itself.
set -eu

if [ ! -e /var/certs/keystores/zk.pem ]; then
    /certs-tools/zk-ca.sh /var/certs zk
    # ZooKeeper and Zuul run as different users.
    chmod -R a+rX /var/certs
fi
