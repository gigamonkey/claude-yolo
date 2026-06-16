#!/usr/bin/env bash
# keychain-prompt-probe.sh — does reading a yolo-written secret prompt?
#
# RUN ON THE macOS HOST, in a normal Terminal with the login keychain unlocked
# (NOT inside a yolo container — `security` is macOS-only). Supports the
# harden-secret-security plan's "Access prompts" decision: write a secret exactly
# the way yolo would (`add-generic-password -U`, NO -A/-T), then read it back
# several times the way a launch would (`find-generic-password -w`), and see
# whether macOS throws a GUI "security wants to use a key" dialog.
#
# WHAT TO WATCH: the script can't see a GUI dialog itself. Watch your screen while
# it runs. PASS = all reads return the value instantly with no dialog. If a dialog
# appears, that's the signal yolo would need -T/-A (or "Always Allow").
set -euo pipefail

SERVICE="claude-yolo-secret-probe-$$"
ACCOUNT="${USER:-claude-yolo}"
VALUE="probe-value-$$-do-not-use"
READS=5

cleanup() { security delete-generic-password -s "$SERVICE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "1) add-generic-password (as yolo does: -U, no -A/-T) ..."
# Exactly yolo's _store_oauth_token flags, minus the value source.
security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" -w "$VALUE"

echo "2) reading it back $READS times (each is a fresh \`security\` process,"
echo "   like reading N secrets at launch). WATCH FOR A GUI DIALOG."
echo "   (If a read blocks waiting on a dialog, that itself is the answer.)"
for i in $(seq 1 "$READS"); do
  got=$(security find-generic-password -s "$SERVICE" -w)
  if [ "$got" != "$VALUE" ]; then
    echo "   read #$i: MISMATCH (got '${got}') — investigate" >&2
    exit 1
  fi
  echo "   read #$i: ok"
done

cat <<'EOF'

RESULT: every read returned the value.
  - If NO dialog appeared  -> PASS: no special ACL needed (default add is fine).
  - If a dialog DID appear -> the plan's fallback applies (add with -T /usr/bin/security
    or -A), or the item needs "Always Allow". Note which and update the plan.

(The probe item has been deleted.)
EOF
