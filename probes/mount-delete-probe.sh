#!/usr/bin/env bash
# mount-delete-probe.sh — does an in-container delete reach the host bind mount?
#
# RUN ON THE macOS HOST, where the docker engine (Docker Desktop / OrbStack) is
# reachable (NOT inside a yolo container). Supports the harden-secret-security
# plan's two claims about cleanup:
#
#   (A) deleting a file INSIDE a bind-mounted DIRECTORY propagates to the host
#       -> required for the ephemeral env-secret self-delete.
#   (B) a SINGLE-FILE bind mount is a mountpoint and can't be unlinked from
#       inside (EBUSY) -> why file-target secrets are always keep-for-session.
#
# Uses a tiny image (alpine); it'll be pulled if not already cached.
set -euo pipefail

IMG="alpine"
HOSTDIR="$(mktemp -d "${TMPDIR:-/tmp}/yolo-mount-probe.XXXXXX")"
cleanup() { rm -rf "$HOSTDIR"; }
trap cleanup EXIT

echo "host scratch: $HOSTDIR"
echo

# (A) file inside a bind-mounted directory --------------------------------------
echo "=== (A) delete a file inside a bind-mounted DIRECTORY ==="
echo "secretA" > "$HOSTDIR/inside"
docker run --rm -v "$HOSTDIR:/run/secrets" "$IMG" \
  sh -c 'echo "  container sees:"; ls -1 /run/secrets; rm -f /run/secrets/inside; echo "  after rm:"; ls -1 /run/secrets || true'
if [ -e "$HOSTDIR/inside" ]; then
  echo "  HOST: /inside STILL PRESENT -> delete did NOT propagate (claim A FAILS)"
  A=FAIL
else
  echo "  HOST: /inside is GONE -> delete propagated (claim A holds)"
  A=PASS
fi
echo

# (B) single-file bind mount ----------------------------------------------------
echo "=== (B) try to unlink a SINGLE-FILE bind mount ==="
echo "secretB" > "$HOSTDIR/single"
docker run --rm -v "$HOSTDIR/single:/mnt/single" "$IMG" \
  sh -c 'rm -f /mnt/single; echo "  rm rc=$? (nonzero/EBUSY expected)"; echo "  file still in container:"; ls -l /mnt/single 2>&1 || true'
if [ -e "$HOSTDIR/single" ]; then
  echo "  HOST: /single still present -> single-file mount survived (claim B holds)"
  B=PASS
else
  echo "  HOST: /single GONE -> single-file unlink DID propagate (claim B FAILS — revisit)"
  B=FAIL
fi
echo

echo "SUMMARY: (A) dir-content delete propagates = $A ; (B) single-file mount un-deletable = $B"
echo "Update plans/harden-secret-security.md (Part 2 / File-target secrets) with the result."
