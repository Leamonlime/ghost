#!/usr/bin/env bash
# Sync the CI mirror (dashboard/pages/) with the LIVE vault-served pages (Session 60).
#
# WHY THIS EXISTS: the dashboard serves ghost-*.html + ghost-common.css from the Obsidian vault at
# request time (that's not changing). dashboard/pages/ is a checked-in COPY so Session 57's
# dashboard-smoke.yml CI has something to test the data-role hook contract against. That copy does
# NOT update itself — it's a snapshot.
#
# RE-RUN THIS whenever a session edits any of these five pages or ghost-common.css, and include the
# result in the SAME commit, so the mirror never silently drifts from what's actually served.
# (A future improvement could wire this into a pre-commit hook or serve the pages from the repo
# directly to remove the mirror entirely — a 2.0 architecture decision, not done here.)
set -euo pipefail

VAULT="${GHOST_VAULT_DIR:-$HOME/CentralVault/Projects/Tech/Ghost}"
DEST="$(cd "$(dirname "$0")" && pwd)/pages"

PAGES=(ghost-dashboard-live.html ghost-bills.html ghost-horoscope.html ghost-tarot.html ghost-quest-log.html ghost-habits.html)

mkdir -p "$DEST/assets"
for p in "${PAGES[@]}"; do
  cp "$VAULT/$p" "$DEST/$p"
done
cp "$VAULT/assets/ghost-common.css" "$DEST/assets/ghost-common.css"

echo "synced ${#PAGES[@]} pages + ghost-common.css"
echo "  from: $VAULT"
echo "  to:   $DEST"
echo "Remember to 'git add dashboard/pages' and commit the mirror alongside the vault edits."
