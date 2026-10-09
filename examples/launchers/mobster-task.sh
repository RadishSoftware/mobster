#!/bin/bash
# Starts a Mobster task from a launcher: a Shortcut or Siri on the Mac, Raycast, Alfred, or a Shortcut on the
# iPhone over SSH (ssh-task.sh). It hands the text to the running Mobster for Mac, or `mobster serve`, with
# `mobster chat --new --json`, so the task, its approvals and its card all show in Mobster. It never answers an
# approval, and it never runs the task in this process: with no Mobster running, it says so and stops.
#
#   mobster-task.sh "What's my next calendar event?"
#
# It prints one line for the launcher to show or speak, then exits 0 whatever happened, because launchers
# treat any other exit as their own failure. The line is one of:
#
#   the answer, or "Done."                                 the task finished
#   Mobster couldn't finish: <why>                         the task ended without finishing
#   Mobster is waiting for your approval on your Mac.      it needs you before it sends, buys, posts or deletes
#   Mobster has a question for you on your Mac: <question>
#   Started. Watch it in Mobster on your Mac.              still working after MOBSTER_WAIT seconds; it goes on
#   Mobster isn't running on your Mac. Open it and try again.
#
# The text reaches mobster as one argument after `--`. Nothing here runs it as a command.
#
# Environment:
#   MOBSTER_BIN   the mobster command (default: PATH, then ~/.local/bin, /opt/homebrew/bin, /usr/local/bin
#                 and ~/.mobster/current, where the installers put it; launchers often have a short PATH)
#   MOBSTER_WAIT  seconds to wait for the answer, 0 to 3600 (default 25; 0 returns once the task starts)
#   MOBSTER_URL   Mobster's address on this Mac (default http://127.0.0.1:8765)
set -u

LIMIT=4000  # characters, the same limit as `mobster chat` and MCP's phone_task

# Print one line and stop. Line breaks and tabs in an answer become spaces.
finish() {
  printf '%s\n' "$(printf '%s' "$1" | LC_ALL=C tr '\r\n\t' '   ')"
  exit 0
}

find_mobster() {
  if [ -n "${MOBSTER_BIN:-}" ]; then
    [ -x "$MOBSTER_BIN" ] && printf '%s\n' "$MOBSTER_BIN"
    return
  fi
  if command -v mobster 2>/dev/null; then
    return
  fi
  for candidate in "$HOME/.local/bin/mobster" /opt/homebrew/bin/mobster /usr/local/bin/mobster \
                   "${MOBSTER_HOME:-$HOME/.mobster}/current/mobster"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
}

# One text field of the JSON that `mobster chat --json` printed; empty when it's missing, null or not text.
# plutil is on every Mac, so this needs no jq.
field() {
  printf '%s' "$json" | plutil -extract "$1" raw -expect string -o - - 2>/dev/null
}

# The text: leading and trailing whitespace dropped.
text=${1-}
text=${text#"${text%%[![:space:]]*}"}
text=${text%"${text##*[![:space:]]}"}
if [ -z "$text" ]; then
  finish "Say what Mobster should do, like: What's my next calendar event?"
fi
# Count characters, not bytes, whatever locale the launcher runs in.
length=$(LC_ALL=en_US.UTF-8; printf '%s' "${#text}")
if [ "$length" -gt "$LIMIT" ]; then
  finish "That's too long for one task. Keep it under 4,000 characters."
fi

wait=${MOBSTER_WAIT:-25}
case $wait in
  ''|*[!0-9]*) wait=25 ;;
esac
if [ "$wait" -gt 3600 ]; then
  wait=3600
fi

mobster=$(find_mobster)
if [ -z "$mobster" ]; then
  finish "The mobster command isn't installed. Install it with: curl -fsSL https://mobster.dev/install.sh | sh"
fi

# --url keeps the task in the running Mobster: without it, `mobster chat` would run the task here when no
# Mobster answers, where nobody can approve anything.
out=$("$mobster" chat --new --json --wait "$wait" --url "${MOBSTER_URL:-http://127.0.0.1:8765}" -- "$text" \
      </dev/null 2>/dev/null)
code=$?
json=${out##*$'\n'}  # the JSON object is the last line
status=$(field status)

case $code in
  0)
    answer=$(field data)
    [ -n "$answer" ] || answer=$(field answer)
    finish "${answer:-Done.}"
    ;;
  1)
    reason=$(field reason)
    if [ -n "$reason" ]; then
      finish "Mobster couldn't finish: $reason"
    fi
    finish "Mobster couldn't finish. Open Mobster on your Mac to see why."
    ;;
  2)
    if [ "$status" = waiting_for_answer ]; then
      finish "Mobster has a question for you on your Mac: $(field question)"
    elif [ "$status" = waiting_for_approval ]; then
      finish "Mobster is waiting for your approval on your Mac."
    fi
    ;;
  3)
    if [ "$(field code)" = not_running ]; then
      finish "Mobster isn't running on your Mac. Open it and try again."
    fi
    error=$(field error)
    if [ -n "$error" ]; then
      finish "Mobster couldn't start the task: $error"
    fi
    ;;
  4)
    finish "Started. Watch it in Mobster on your Mac."
    ;;
esac
finish "mobster stopped with exit code $code. Run the same task with \`mobster chat\` in Terminal to see why."
