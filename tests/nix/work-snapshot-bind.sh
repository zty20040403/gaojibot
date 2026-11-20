#!/usr/bin/env bash
# Run from the repository root; Docker supplies a real same-device bind without CAP_SYS_ADMIN.
set -euo pipefail
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/ct/s000001/work/nested" "$fixture/ct/s000002/work" "$fixture/neighbour"
printf TEST_OTHER > "$fixture/neighbour/private"
printf TEST_OK > "$fixture/ct/s000002/work/file"
docker run --rm --network none -v "$PWD/nix/work_snapshot.py:/snapshot.py:ro" \
  -v "$fixture/ct:/ct:ro" -v "$fixture/neighbour:/ct/s000001/work/nested:ro" python:3.12-slim sh -ecu '
  mkdir -m 0700 /out
  test "$(stat -c %d /ct)" = "$(stat -c %d /ct/s000001/work/nested)"
  if python -I /snapshot.py /ct /out 2>/tmp/failure; then exit 1; fi
  grep -F "ValueError: mount boundary in workspace" /tmp/failure
  test -z "$(find /out -name "s000001*" -o -name .pending)"
  test "$(tar xOf /out/s000002_*.tar file)" = TEST_OK
'
