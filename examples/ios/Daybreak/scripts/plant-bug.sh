#!/bin/sh
# Plant Daybreak's paywall bug in the source, or take it out again.
#
#   scripts/plant-bug.sh missing-plan          the paywall shows 2 plans instead of 3 (a one-line change)
#   scripts/plant-bug.sh missing-plan --undo   put the line back
#
# Rebuild after either one. The other planted bugs need no source change: launch the app
# with -DaybreakBug annual-price, reminder-not-saved or stuck-loading.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
file="$here/Daybreak/PaywallView.swift"
good='ForEach(Plan.all) { plan in'
bad='ForEach(Plan.all.prefix(2)) { plan in'

usage() {
  echo "usage: scripts/plant-bug.sh missing-plan [--undo]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
case "$1" in
  missing-plan) ;;
  annual-price|reminder-not-saved|stuck-loading)
    echo "$1 needs no source change. Launch Daybreak with: -DaybreakBug $1" >&2
    exit 2 ;;
  *) usage ;;
esac
undo=0
if [ $# -ge 2 ]; then
  [ "$2" = "--undo" ] || usage
  undo=1
fi

if [ "$undo" -eq 1 ]; then from=$bad; to=$good; else from=$good; to=$bad; fi
count=$(grep -cF "$from" "$file" || true)
if [ "$count" -ne 1 ]; then
  if grep -qF "$to" "$file"; then
    [ "$undo" -eq 1 ] && echo "missing-plan isn't planted." || echo "missing-plan is already planted."
    exit 0
  fi
  echo "Can't find the plans line in $file. Was PaywallView.swift changed by hand?" >&2
  exit 1
fi

tmp="$file.tmp.$$"
awk -v from="$from" -v to="$to" '{
  i = index($0, from)
  if (i) { $0 = substr($0, 1, i - 1) to substr($0, i + length(from)) }
  print
}' "$file" > "$tmp"
mv "$tmp" "$file"

if [ "$undo" -eq 1 ]; then
  echo "Removed missing-plan: the paywall shows all 3 plans again. Rebuild Daybreak."
else
  echo "Planted missing-plan: the paywall now shows 2 plans. Rebuild Daybreak."
fi
