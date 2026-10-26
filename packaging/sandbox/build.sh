#!/bin/sh
set -eu

case "$GAOJI_RECIPE_ID" in
    *[!a-f0-9]*|'') exit 2 ;;
esac
test "${#GAOJI_RECIPE_ID}" = 64
available=$(df --output=avail -k /nix | tail -n 1 | tr -d ' ')
test "$available" -ge 12582912 || { printf 'Sandbox build needs 12 GiB free in the container engine.\n' >&2; exit 3; }

timeout --kill-after=30 3300 nix-build /recipe/default.nix --argstr tag "$GAOJI_RECIPE_ID" \
    --option sandbox false --option filter-syscalls false \
    --option build-users-group nixbld --max-jobs 1 --cores 2 \
    --out-link /tmp/gaoji-sandbox-image
nix-store --verify --check-contents
cp -L /tmp/gaoji-sandbox-image /out/image.tar.gz.partial
chmod 0600 /out/image.tar.gz.partial
sync /out/image.tar.gz.partial
mv /out/image.tar.gz.partial /out/image.tar.gz
sync /out
