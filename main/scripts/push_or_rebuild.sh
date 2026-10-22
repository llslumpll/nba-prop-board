#!/usr/bin/env bash
# Commit docs/ + data/ and push. If another run pushed to main meanwhile, never
# merge generated files (two runs' Best 5 tags merged line-by-line once made a
# 6-pick list): reset to the new main, put back this run's freshly fetched
# inputs (KEEP files), rebuild on top of it, and try again.
# Usage: push_or_rebuild.sh "<commit label>" "<rebuild command>" [keep files...]
set -u
MSG="$1"; REBUILD="$2"; shift 2; KEEP=("$@")
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
for i in 1 2 3 4 5; do
  git add docs data
  if git diff --cached --quiet; then echo "nothing changed"; exit 0; fi
  git commit -q -m "$MSG: $(date -u +'%Y-%m-%d %H:%M UTC')"
  if git push -q; then echo "pushed"; exit 0; fi
  echo "main moved while this run worked; rebuilding on top of it (attempt $i)"
  tmp=$(mktemp -d)
  for f in "${KEEP[@]}"; do if [ -f "$f" ]; then mkdir -p "$tmp/$(dirname "$f")"; cp "$f" "$tmp/$f"; fi; done
  git fetch -q origin main && git reset -q --hard origin/main
  for f in "${KEEP[@]}"; do if [ -f "$tmp/$f" ]; then cp "$tmp/$f" "$f"; fi; done
  eval "$REBUILD" || { echo "rebuild failed"; exit 1; }
  sleep $((RANDOM % 10 + 3))
done
echo "could not push after 5 attempts"; exit 1
