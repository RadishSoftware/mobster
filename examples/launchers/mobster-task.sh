#!/bin/bash
# Starts a Mobster task from a launcher: a Shortcut or Siri on the Mac, Raycast, Alfred, or a Shortcut on the
# iPhone over SSH (ssh-task.sh). It hands the text to the running Mobster for Mac, or `mobster serve`, with
# `mobster chat --new --json`, so the task, its approvals and its card all show in Mobster. It never answers an
# approval, and it never runs the task in this process: with no Mobster running, it says so and stops.
#
#   mobster-task.sh "What's my next calendar event?"
#   mobster-task.sh --workflow "Reels digest" topic=basketball minutes=10
#   mobster-task.sh --json "How many unread emails do I have?"
#
# With --workflow it runs one of your saved workflows instead, by its name, with a value for each blank you give as
# NAME=VALUE (a blank you leave out takes its default). It starts a new conversation, or continues the workflow's own
# one when it always does.
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
# With --json first, it prints one JSON object for a Shortcut's "Get Dictionary from Input" instead, and waits for
# the task to end: an approval or a question waits for you in Mobster on your Mac, and the task goes on once you
# answer. The object has mobster chat --json's fields, with these three set:
#
#   status    completed, or how it ended; waiting_for_approval, waiting_for_answer or running when MOBSTER_WAIT ran
#             out first; couldnt_run when it never started
#   answer    the whole answer as text
#   data      the answer's structured data (an object or a list), only when the task returned some
#   message   the line the plain mode prints
#
# The text reaches mobster as one argument after `--`. Nothing here runs it as a command.
#
# Environment:
#   MOBSTER_BIN   the mobster command (default: PATH, then the agent inside Mobster.app in /Applications or
#                 ~/Applications, then ~/.local/bin, /opt/homebrew/bin, /usr/local/bin and ~/.mobster/current;
#                 launchers often have a short PATH)
#   MOBSTER_WAIT  seconds to wait for the answer, 0 to 3600 (default 25, or 600 with --json; 0 returns once the
#                 task starts)
#   MOBSTER_URL   Mobster's address on this Mac (default: the address the Mac app wrote beside its token, else
#                 http://127.0.0.1:8765; the app's agent moves off 8765 when another app holds that port)
set -u

LIMIT=4000  # characters, the same limit as `mobster chat` and MCP's phone_task

# --json: what to print is one JSON object, for a Shortcut.
as_json=
if [ "${1-}" = --json ]; then
  as_json=1
  shift
fi
json=

# Print one line and stop. Line breaks and tabs in an answer become spaces. With --json, the object instead: the
# JSON mobster printed (or {"status": "couldnt_run"} when it printed none), with answer, data and message set.
finish() {
  local line
  line=$(printf '%s' "$1" | LC_ALL=C tr '\r\n\t' '   ')
  if [ -n "$as_json" ]; then
    dictionary "$line" "${2:-couldnt_run}"
    exit 0
  fi
  printf '%s\n' "$line"
  exit 0
}

# The object --json prints: mobster's own fields; answer is the whole text answer, data stays only when it's
# structured, and message is the line. plutil is on every Mac, so this needs no jq.
dictionary() {
  local out text
  out=$json
  if ! printf '%s' "$out" | plutil -extract status raw -o - - >/dev/null 2>&1; then
    out=$(printf '{"status":"%s"}' "$2")  # one of the script's own words
  fi
  text=$(field data)
  if [ -n "$text" ]; then
    out=$(printf '%s' "$out" | plutil -replace answer -string "$text" -o - - | plutil -remove data -o - -)
  fi
  printf '%s\n' "$(printf '%s' "$out" | plutil -replace message -string "$1" -o - -)"
}

find_mobster() {
  if [ -n "${MOBSTER_BIN:-}" ]; then
    [ -x "$MOBSTER_BIN" ] && printf '%s\n' "$MOBSTER_BIN"
    return
  fi
  if command -v mobster 2>/dev/null; then
    return
  fi
  for candidate in /Applications/Mobster.app/Contents/MacOS/mobster-agent \
                   "$HOME/Applications/Mobster.app/Contents/MacOS/mobster-agent" \
                   "$HOME/.local/bin/mobster" /opt/homebrew/bin/mobster /usr/local/bin/mobster \
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

# A saved workflow: its name, then NAME=VALUE for its blanks.
workflow=
sets=()
if [ "${1-}" = --workflow ]; then
  workflow=${2-}
  if [ -z "$workflow" ]; then
    finish "Name the workflow, like: --workflow \"Reels digest\" topic=basketball"
  fi
  shift 2
  for pair in "$@"; do
    case $pair in
      ?*=*) sets+=(--set "$pair") ;;
      *) finish "Give each blank as NAME=VALUE, like topic=basketball" ;;
    esac
  done
  set -- "$workflow"
fi

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

default_wait=25
keep=()
if [ -n "$as_json" ]; then
  # A Shortcut waits for the answer, approvals included: the run's own approval waits 10 minutes.
  default_wait=600
  keep=(--keep-waiting)
fi
wait=${MOBSTER_WAIT:-$default_wait}
case $wait in
  ''|*[!0-9]*) wait=$default_wait ;;
esac
if [ "$wait" -gt 3600 ]; then
  wait=3600
fi

mobster=$(find_mobster)
if [ -z "$mobster" ]; then
  finish "The mobster command isn't installed. Download Mobster for Mac: https://mobster.dev/download"
fi

# --url keeps the task in the running Mobster: without it, `mobster chat` would run the task here when no
# Mobster answers, where nobody can approve anything.
url=${MOBSTER_URL:-}
if [ -z "$url" ]; then
  url=$(head -n 1 "$HOME/Library/Application Support/app.mobster.desktop/state/api-url" 2>/dev/null)
  case "$url" in
    http://127.0.0.1:[0-9]*) ;;
    *) url=http://127.0.0.1:8765 ;;
  esac
fi
if [ -n "$workflow" ]; then
  out=$("$mobster" chat --json ${keep[@]+"${keep[@]}"} --wait "$wait" --url "$url" \
        --workflow "$workflow" ${sets[@]+"${sets[@]}"} </dev/null 2>/dev/null)
else
  out=$("$mobster" chat --new --json ${keep[@]+"${keep[@]}"} --wait "$wait" --url "$url" -- "$text" \
        </dev/null 2>/dev/null)
fi
code=$?
json=${out##*$'\n'}  # the JSON object is the last line
status=$(field status)

case $code in
  0)
    answer=$(field data)
    [ -n "$answer" ] || answer=$(field answer)
    finish "${answer:-Done.}" completed
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
