#!/usr/bin/env bash
# Pack logic: no mission builds, nodes, live scenarios or git operations.
# T5_COMPILE_COMMANDS enables optional native C syntax checks using an existing build.
# Independent unittest modules run concurrently; generated output stays in /tmp.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d /tmp/task5-selftest-XXXXXX)" || exit 2
trap 'find "$work" -type f -delete; rmdir "$work"' EXIT
pids=()
names=()
for test in "$here"/selftest/test_*.py; do
    name="$(basename "$test" .py)"
    (cd "$here" && PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s selftest -p "$name.py" > "$work/$name.log" 2>&1) &
    pids+=("$!")
    names+=("$name")
done
status=0
for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then
        echo "PASS ${names[$i]}"
        tail -n 4 "$work/${names[$i]}.log"
    else
        echo "FAIL ${names[$i]}"
        cat "$work/${names[$i]}.log"
        status=1
    fi
done
if [ "$status" -eq 0 ]; then
    echo "SELFTEST OK (pack logic; production component runners and live checks unexercised)"
else
    echo "SELFTEST FAILED"
fi
exit "$status"
