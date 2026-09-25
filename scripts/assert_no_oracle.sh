#!/usr/bin/env bash
# Assert that a built agent image carries no readable copy of a task's target
# library outside /src.  See ORACLE_LEAK_FIX.md §7.
#
# A task whose agent image ships a released copy of its own target hands the
# agent the injected CWE via a single `diff`, invalidating the discovery phase.
# Run this against every agent image before trusting a score.
#
# Usage: assert_no_oracle.sh <image> <name> [<name> ...]
#   Python target: pass the IMPORT name   e.g. ... harbor-<uuid>:<tag>-agent jwt
#   C/Go target:   pass SOURCE file names e.g. ... <image> sqlite3.c sqlite3.h
#
# Matching is by name, over both directories and files.  For a non-Python
# target, a bare project name is the wrong probe -- an interpreter ships a
# stdlib package of the same name (python3.x/sqlite3) that reveals nothing
# about a C-library CVE -- so pass the source filenames you actually care about.
#
# Exit 0 = clean, 1 = leak found, 2 = usage error.
set -euo pipefail

IMAGE="${1:?image required}"; shift
[ "$#" -gt 0 ] || { echo "usage: $0 <image> <import-name> [...]" >&2; exit 2; }

# Build the find expression for the import names plus their dist-info dirs.
EXPR=""
for n in "$@"; do
    EXPR="$EXPR -name $n -o -name ${n}-*.dist-info -o"
done
EXPR="$EXPR -false"

# An editable install's dist-info is expected: it points at /src, carries no
# source, and its RECORD holds no source hashes, so it is not an oracle.  What
# must not exist is a real package directory holding readable .py/.c sources.
# /src and /out are task-owned (the target source and its intended reproducer
# artifacts); an oracle is something the HARNESS layered on top, so prune both.
# An interpreter's own stdlib package of the same name is never the task's
# target copy -- a real Python oracle lands in site-packages/dist-packages, and
# a C/Go target is not a Python package at all.  Drop those, but keep anything
# under site-packages/dist-packages so a genuine leak is never hidden.
FOUND="$(docker run --rm --entrypoint /bin/sh "$IMAGE" -c "
    find / -xdev -path /src -prune -o -path /out -prune -o -path /proc -prune -o \
        \( -type d -o -type f \) \( $EXPR \) -print 2>/dev/null || true
" 2>/dev/null | grep -vE '/lib/python[0-9.]+/[^/]+$' || true)"

if [ -n "$FOUND" ]; then
    echo "LEAK: $IMAGE exposes a copy of: $*" >&2
    echo "$FOUND" >&2
    echo "Rebuild this task's agent image with the scrub applied (ORACLE_LEAK_FIX.md §4)." >&2
    exit 1
fi

echo "clean: $IMAGE exposes no copy of: $*"
