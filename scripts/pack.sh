#!/usr/bin/env bash
# Pack source trees into a reproducible src.tgz for a task payload.
#
#  * tracked files only (`git ls-files -c`): untracked leftovers such as a
#    poc.bin, crash.log or build tree never ship to the agent;
#  * a deny-list for artefact names, as a second line of defence;
#  * uniform mtime / owner / ordering, so the archive carries no metadata
#    that reveals which files were edited last (an injected defect is
#    otherwise a one-command `tar tvzf` tell).
set -euo pipefail

OUTPUT="${OUTPUT:-src.tgz}"
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

DENY_RE='(^|/)(poc\.bin|crash\.log|fix\.patch|patch\.diff|report\.json|oracle\.json|\.pytest_cache(/|$)|__pycache__(/|$))'

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <repo_dir1> [repo_dir2 ...]" >&2
  exit 1
fi

# Archive members are always <basename>/<path>, whatever form the argument
# took (relative, absolute, trailing slash): the Dockerfile extracts into /src
# and expects /src/<repo>/... .  All arguments must share one parent directory.
PARENT=""
for filename in "$@"; do
  [ -e "$filename" ] || { echo "ERROR: $filename does not exist" >&2; exit 1; }
  abs=$(cd "$(dirname "$filename")" && pwd -P)/$(basename "$filename")
  parent=$(dirname "$abs")
  if [ -z "$PARENT" ]; then PARENT="$parent"; fi
  if [ "$parent" != "$PARENT" ]; then
    echo "ERROR: all inputs must live in the same directory ($PARENT vs $parent)" >&2; exit 1
  fi
  base=$(basename "$abs")
  if [ -d "$abs/.git" ]; then
    echo "Adding tracked files from $base ..."
    # -z / NUL-safe: paths with |, &, spaces or newlines are handled verbatim.
    (
      cd "$abs"
      git ls-files -c -z
    ) | while IFS= read -r -d '' f; do
      printf '%s/%s\n' "$base" "$f"
    done >> "$TMPFILE"
  elif [ -d "$abs" ]; then
    echo "Warning: No .git found in $base, adding all files." >&2
    (cd "$PARENT" && find "$base" -type f -print) >> "$TMPFILE"
  else
    echo "Adding file $base ..."
    echo "$base" >> "$TMPFILE"
  fi
done
OUTPUT=$(cd "$(dirname "$OUTPUT")" && pwd -P)/$(basename "$OUTPUT")
cd "$PARENT"

denied=$(grep -E "$DENY_RE" "$TMPFILE" || true)
if [ -n "$denied" ]; then
  echo "Refusing to pack answer/artefact files:" >&2
  echo "$denied" >&2
  exit 1
fi

LC_ALL=C sort -u -o "$TMPFILE" "$TMPFILE"

if tar --version 2>/dev/null | grep -q 'GNU tar'; then
  tar --sort=name --mtime='2000-01-01 00:00:00Z' --owner=0 --group=0 --numeric-owner \
      --exclude-vcs -czf "$OUTPUT" --files-from "$TMPFILE"
elif command -v gtar >/dev/null 2>&1; then
  gtar --sort=name --mtime='2000-01-01 00:00:00Z' --owner=0 --group=0 --numeric-owner \
      --exclude-vcs -czf "$OUTPUT" --files-from "$TMPFILE"
else
  # bsdtar (macOS): no --sort/--mtime, but the list is sorted and ownership
  # can be forced; normalise mtimes on a staged copy instead.
  STAGE=$(mktemp -d)
  trap 'rm -rf "$STAGE" "$TMPFILE"' EXIT
  # Stage through a plain tar stream (keeps modes, drops xattrs/resource forks,
  # and is ~20x faster than a per-file cp loop on large trees).
  COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --no-acls --no-fflags -cf - -T "$TMPFILE" \
    | tar -xf - -C "$STAGE"
  TZ=UTC find "$STAGE" -exec touch -t 200001010000 {} +
  # COPYFILE_DISABLE + --no-mac-metadata: otherwise bsdtar stores AppleDouble
  # `._file` entries (xattrs / resource forks) that leak host metadata and
  # litter the tree the agent sees.
  (cd "$STAGE" && COPYFILE_DISABLE=1 tar --uid 0 --gid 0 --numeric-owner --exclude-vcs \
      --no-mac-metadata --no-xattrs --no-acls --no-fflags -czf - -T "$TMPFILE") > "$OUTPUT"
fi
echo "Wrote $OUTPUT ($(wc -c < "$OUTPUT") bytes, $(wc -l < "$TMPFILE") files)"
