#!/bin/bash
# Usage: wait-for.sh host:port... -- command
# Waits until each TCP endpoint accepts connections, then runs command.
while [ "$1" != "--" ]; do
    host=${1%:*}; port=${1#*:}; shift
    until (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; do
        echo "Waiting for $host:$port"; sleep 2
    done
done
shift
exec "$@"
