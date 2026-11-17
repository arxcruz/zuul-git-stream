#!/bin/bash
# Guided presentation of git-stream.
#
#   ./demo.sh          start everything (via deploy.sh) and walk through the demo
#
# Each step waits for Enter.  At a "send" prompt, press Enter to let the
# script publish the message, or click the same button in the demo UI and
# type "s" + Enter to skip sending.
set -eu
source "$(dirname "$0")/common.sh"

title() { printf '\n%s━━ %s ━━%s\n\n' "$B$BLUE" "$1" "$R"; }
pause() { printf '\n  %s[Enter]%s ' "$D" "$R"; read -r _ </dev/tty; }
open_url() { command -v open >/dev/null && open "$1" || xdg-open "$1" 2>/dev/null || true; }
show() { printf '%s' "$D"; sed 's/^/      /'; printf '%s' "$R"; }

# send PATH BODY LABEL: publish through the demo UI; prints the message key.
send() {
    printf '\n  %s%s?%s Enter = send it, s = I clicked it in the UI ' \
        "$B" "$3" "$R" >&2
    read -r ans </dev/tty
    if [ "$ans" = "s" ]; then
        return
    fi
    curl -sf -X POST "$UI$1" -d "$2" | python3 -c '
import json, sys
m = json.load(sys.stdin)
if m.get("error"):
    sys.exit("  " + m["error"])
print("  \033[36m→ POST /api/stream → %s\033[0m" % m.get("result", ""), file=sys.stderr)
print("    " + json.dumps(json.loads(m["value"]), indent=1).replace("\n", "\n    "), file=sys.stderr)
print(m["key"])'
}

newest_bundle() {
    curl -sf "$UI/api/bundles" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["bundles"][0]["id"])'
}

# Print the latest result of each job for a bundle.
show_bundle() {
    curl -sf "$UI/api/bundles" | python3 -c '
import json, sys
d = json.load(sys.stdin)
b = next((b for b in d["bundles"] if b["id"] == sys.argv[1]), None)
if not b:
    sys.exit()
icons = {"SUCCESS": "\033[32m✔", "FAILURE": "\033[31m✘"}
cells = []
for job in d["jobs"]:
    state = (b["jobs"].get(job) or {}).get("state")
    cells.append("%s %s\033[0m" % (icons.get(state, "\033[2m·"), job[len("bundle-"):]))
for i in range(0, len(cells), 5):
    print("    " + "   ".join(cells[i:i + 5]))
print("    merge request !%s%s" % (b["iid"], "  \033[32m(approved by Zuul)\033[0m" if b["approved"] else ""))
' "$1"
}

# Show the webhooks git-stream sent since the given count.
show_hooks() {
    curl -sf "$UI/api/deliveries" | python3 -c '
import json, sys
for h in json.load(sys.stdin)[:int(sys.argv[1])][::-1]:
    print("    \033[36m→ %s\033[0m HTTP %s   %s" % (h["kind"], h["status"], h["summary"].replace("\n", " ")[:70]))
' "$1"
}

# Wait until a bundle is approved or escalated, showing progress.
wait_settled() {
    printf '\n'
    for _ in $(seq 1 300); do
        local line
        line=$(curl -sf "$UI/api/bundles" | python3 -c '
import json, sys
b = next((b for b in json.load(sys.stdin)["bundles"] if b["id"] == sys.argv[1]), None)
if b:
    s = [v["state"] for v in b["jobs"].values()]
    print("%s run %d: \033[32m%d passed\033[0m  \033[31m%d failed\033[0m  \033[34m%d running\033[0m%s" % (
        b["id"], max(b["runs"], 1), s.count("SUCCESS"), s.count("FAILURE"),
        s.count("RUNNING") + s.count("QUEUED"),
        "  \033[32mapproved\033[0m" if b["approved"] else
        "  \033[31mescalated\033[0m" if b["escalated"] else ""))
' "$1" 2>/dev/null || true)
        printf '\r\e[2K    %s' "$line"
        case $line in *approved*|*escalated*) break ;; esac
        sleep 2
    done
    printf '\n'
}

# Print a bundle's merge request comments (first line of each).
show_thread() {
    local iid
    iid=$(curl -sf "$UI/api/bundles" | python3 -c '
import json, sys
print(next(b["iid"] for b in json.load(sys.stdin)["bundles"] if b["id"] == sys.argv[1]))' "$1")
    curl -sf "$FORGE/api/v4/projects/bundles/merge_requests/$iid/notes?sort=asc" | python3 -c '
import json, sys
for n in json.load(sys.stdin):
    who = n["author"]["username"]
    color = "\033[36m" if who == "git-stream" else "\033[2m"
    print("    %s%-10s\033[0m %s" % (color, who, n["body"].split("\n")[0]))'
}

show_escalations() {
    curl -sf "$FORGE/api/stream/escalations" | python3 -c '
import json, sys
for t in json.load(sys.stdin)[:int(sys.argv[1])]:
    print("    \033[31mescalated\033[0m %s: %s" % (t["key"], ", ".join(
        "%s failed %d times" % (j["job"], j["attempts"]) for j in t["failing"])))' "$1"
}

# Show live job progress until the pipelines are empty.
watch_jobs() {
    local seen=0 empty=0
    printf '\n'
    for _ in $(seq 1 240); do
        local line
        line=$(curl -sf "$UI/api/status" | python3 -c '
import json, sys
items = json.load(sys.stdin)
count = {}
for item in items:
    for j in item["jobs"]:
        state = j["result"] or ("running" if j["started"] else "queued")
        count[state] = count.get(state, 0) + 1
names = [("running", "\033[34m▶"), ("queued", "\033[2m…"),
         ("SUCCESS", "\033[32m✔"), ("FAILURE", "\033[31m✘"),
         ("SKIPPED", "\033[2m⤼")]
if items:
    print("%s: " % ", ".join(sorted({i["pipeline"] for i in items})) +
          "   ".join("%s %d %s\033[0m" % (icon, count[k], k.lower())
                     for k, icon in names if count.get(k)))
' 2>/dev/null || true)
        if [ -n "$line" ]; then
            seen=1; empty=0
            printf '\r\e[2K    %s' "$line"
        elif [ $seen = 1 ]; then
            empty=$((empty + 1))
            [ $empty -ge 2 ] && break
        fi
        sleep 2
    done
    printf '\n'
    [ $seen = 1 ] || say "${YEL}Nothing was enqueued.${R}"
}

walkthrough() {
    clear
    title "Zuul × git-stream"
    say "Zuul reacts to code review events from Gerrit, GitHub, GitLab..."
    say "Our build system doesn't have code review: it just ${B}announces things${R}."
    say ""
    say "git-stream is a small fake GitLab.  Other systems send it events over"
    say "HTTP; it turns them into git commits and merge requests, and sends"
    say "Zuul the webhooks a real GitLab would send:"
    say ""
    say "  ${CYAN}build system → git-stream (commit + merge request)${R}"
    say "  ${CYAN}             → GitLab webhook → Zuul's stock GitLab driver → jobs${R}"
    say "  ${CYAN}             ← comments and approvals on the merge request${R}"
    say "  ${CYAN}git-stream retries failures, then escalates what still fails${R}"
    say ""
    say "No Zuul plugin, no patch: Zuul runs from its upstream images."
    open_url "$UI"
    open_url "$FORGE"
    pause

    title "1. How Zuul is configured"
    say "zuul.conf has an ordinary GitLab connection, pointing at git-stream:"
    sed -n '/^\[connection gitlab\]/,/^$/p' etc_zuul/zuul.conf | show
    say "and the pipeline triggers are ordinary GitLab triggers:"
    sed -n '/name: bundle-test$/,/recheck/p' repos/zuul-config/zuul.d/pipelines.yaml | show
    pause

    title "2. A new bundle is announced"
    say "The build system sends one event.  git-stream opens a merge"
    say "request that adds bundle.yaml, and Zuul runs the 10 bundle tests."
    say "They fail at random like a flaky network, and ${B}security${R} always"
    say "fails: ci-tool, the tool the tests use, has a bug."
    say ""
    say "When Zuul reports a failure, git-stream re-runs the failing jobs by"
    say "itself, up to 3 runs per job, then escalates what still fails."
    local bundle
    bundle=$(send /api/bundle '{"action": "new", "flaky": 0.3}' "Announce a bundle")
    bundle=${bundle:-$(newest_bundle)}
    sleep 3
    say ""; say "Webhooks git-stream sent to Zuul:"; show_hooks 2
    wait_settled "$bundle"
    show_bundle "$bundle"
    pause

    title "3. What happened on the merge request"
    say "Zuul reports with comments; git-stream answers with \"rerun failed\""
    say "(bundle-dispatch reads it back and runs only those jobs):"
    show_thread "$bundle"
    say ""
    say "The flaky jobs passed on a retry.  security failed 3 times, so"
    say "git-stream escalated it (also on $FORGE/escalations):"
    show_escalations 1
    say ""
    say "People can still ask for runs: \"recheck\", \"rerun failed\", \"rerun api auth\"."
    pause

    title "4. Test with a fix that isn't merged yet"
    say "A ci-tool developer opens a merge request with the fix."
    send /api/ci-tool '{"action": "fix-open"}' "Open the ci-tool fix" >/dev/null
    sleep 3
    say ""
    say "Our bundle's merge request gets a ${B}Depends-On:${R} line pointing at it."
    say "Zuul tests the bundle with the fix speculatively merged into ci-tool,"
    say "exactly like it does for Gerrit or GitHub changes."
    send /api/bundle "{\"action\": \"with-fix\", \"bundle_id\": \"$bundle\", \"flaky\": 0}" \
        "Add Depends-On to the bundle" >/dev/null
    wait_settled "$bundle"
    show_bundle "$bundle"
    say ""
    say "All green, so Zuul approved the merge request."
    say "(Its version changed, so the retry count started over.)"
    pause

    title "5. Merge the fix"
    say "Merging is a push to ci-tool's master; git-stream tells Zuul about it."
    send /api/ci-tool '{"action": "fix-merge"}' "Merge the ci-tool fix" >/dev/null
    sleep 3
    show_hooks 2
    say ""
    say "New bundles are tested with the fixed ci-tool, without Depends-On."
    pause

    title "6. Under the hood"
    say "git-stream is a real git server; Zuul's merger clones it over HTTP:"
    say "    git clone http://localhost:8090/bundles.git"
    say "    git fetch origin refs/merge-requests/1/head"
    say ""
    say "Anything that can make an HTTP request can send events:"
    say "    curl -X POST localhost:8090/api/stream -d '{\"action\": \"comment\","
    say "         \"project\": \"bundles\", \"mr\": \"$bundle\", \"note\": \"recheck\"}'"
    pause

    title "Thanks!"
    if [ "$DEMO_HOST" != localhost ]; then
        say "Try it yourself: http://$DEMO_HOST:8080"
    else
        say "Try it yourself: $UI"
    fi
    say ""
}

./deploy.sh
walkthrough
