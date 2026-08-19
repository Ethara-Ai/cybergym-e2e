#!/usr/bin/env bash
# pin_docker_images.sh — Pull unpinned Docker images and print pinned FROM lines.
#
# This script does NOT modify Dockerfiles automatically. It pulls each unpinned
# image, resolves the sha256 digest, and prints the pinned FROM line you can
# paste into the Dockerfile.
#
# Usage:
#   bash scripts/pin_docker_images.sh                # pin all unpinned images
#   bash scripts/pin_docker_images.sh --apply        # pin and update Dockerfiles in-place
#   bash scripts/pin_docker_images.sh --dry-run      # just list what would be pinned
#
# Requires: docker CLI with access to pull from Docker Hub / gcr.io

set -euo pipefail

TASKS_DIR="${TASKS_DIR:-$(dirname "$0")/../tasks}"
APPLY=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=true ;;
        --dry-run) DRY_RUN=true ;;
        --help|-h)
            echo "Usage: $0 [--apply] [--dry-run]"
            echo "  --apply    Update Dockerfiles in-place with pinned digests"
            echo "  --dry-run  Only show what would change, don't pull"
            exit 0
            ;;
    esac
done

echo "=== Docker Image Pinning Tool ==="
echo ""

pinned=0
unpinned=0
errors=0

for dockerfile in "$TASKS_DIR"/*/environment/Dockerfile; do
    task=$(basename "$(dirname "$(dirname "$dockerfile")")")
    from_line=$(grep '^FROM ' "$dockerfile" | head -1)
    image=$(echo "$from_line" | sed 's/^FROM //' | awk '{print $1}')

    if echo "$image" | grep -q '@sha256:'; then
        echo "✅ PINNED  $task"
        echo "   $from_line"
        pinned=$((pinned + 1))
        continue
    fi

    unpinned=$((unpinned + 1))
    echo "🔓 UNPINNED  $task"
    echo "   Current: $from_line"

    if $DRY_RUN; then
        echo "   (dry-run: would pull and resolve digest)"
        echo ""
        continue
    fi

    # Pull the image
    if ! docker pull "$image" > /dev/null 2>&1; then
        echo "   ❌ ERROR: Failed to pull $image"
        errors=$((errors + 1))
        echo ""
        continue
    fi

    # Get the pinned digest
    digest=$(docker inspect --format='{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)

    if [ -z "$digest" ]; then
        echo "   ❌ ERROR: Could not resolve digest for $image"
        errors=$((errors + 1))
        echo ""
        continue
    fi

    echo "   Pinned:  FROM $digest"

    if $APPLY; then
        # Replace the FROM line in-place
        sed -i.bak "s|^FROM ${image}|FROM ${digest}|" "$dockerfile"
        rm -f "${dockerfile}.bak"
        echo "   ✅ Updated $dockerfile"
    else
        echo "   (use --apply to update Dockerfile)"
    fi

    echo ""
done

echo "=== Summary ==="
echo "  Already pinned: $pinned"
echo "  Unpinned:       $unpinned"
echo "  Errors:         $errors"

if [ $unpinned -gt 0 ] && ! $APPLY && ! $DRY_RUN; then
    echo ""
    echo "Run with --apply to update Dockerfiles in-place."
fi
