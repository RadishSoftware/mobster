#!/bin/bash
# The one thing an iPhone Shortcut's SSH key can run on this Mac: start a Mobster task with the text it sent.
#
# Its line in ~/.ssh/authorized_keys forces this script, whatever the phone asks to run:
#
#   restrict,command="/Users/you/Library/Scripts/Mobster/ssh-task.sh" ssh-ed25519 AAAA... mobster-shortcut
#
# `restrict` turns off the terminal (pty), port, agent and X11 forwarding, and ~/.ssh/rc. sshd puts what the
# phone asked to run in SSH_ORIGINAL_COMMAND; this reads it as the task's words, never as a command. It refuses,
# prints why and exits 1 for:
#
#   - no text, as in a plain `ssh` login to a shell
#   - control characters, line breaks and tabs included
#   - text that starts with - or /, like an option or a program path (sftp asks for /usr/libexec/sftp-server)
#   - shell syntax that a spoken or typed task doesn't need: ` $( ${ ; | && < >
#   - scp, sftp, rsync and git, which start their own programs over SSH
#   - more than 4,000 characters (mobster-task.sh checks)
#
# Text that passes goes to mobster-task.sh as one argument, so it is never run even if a check above missed
# something. The script returns as soon as the task starts (MOBSTER_WAIT=0): the iPhone that sent it is usually
# the phone the task drives, and a Shortcut still on screen would get in the agent's way. The answer and any
# approval are in Mobster on the Mac. The phone only learns that the task started, or why it didn't.
set -u
# Byte-wise checks, whatever locale the phone's SSH client asked for. UTF-8 text (accents, emoji, curly quotes)
# passes: its bytes are above 0x7f.
export LC_ALL=C

refuse() {
  printf '%s\n' "Mobster didn't start this: $1"
  exit 1
}

text=${SSH_ORIGINAL_COMMAND-}

case $text in
  *[[:cntrl:]]*)
    refuse "it has a line break or a control character. Send the task as one line." ;;
esac
# C1 control characters (U+0080 to U+009F) in UTF-8: some terminals read them as escapes.
case $text in
  *$'\xc2'[$'\x80'-$'\x9f']*)
    refuse "it has a control character. Send the task as one line." ;;
esac
# Leading spaces don't hide a - or a /.
trimmed=${text#"${text%%[![:space:]]*}"}
if [ -z "$trimmed" ]; then
  refuse "there was no text. This key only starts Mobster tasks: send what Mobster should do."
fi
case $trimmed in
  -*|/*)
    refuse "a task can't start with - or /." ;;
esac
# shellcheck disable=SC2016  # the quotes hold these characters literally, on purpose
case $text in
  *'`'*|*'$('*|*'${'*|*';'*|*'|'*|*'&&'*|*'<'*|*'>'*)
    refuse "it has shell syntax (\` \$( \${ ; | && < >). Say the task in words." ;;
esac
first=${trimmed%%[[:space:]]*}
case $first in
  scp|sftp|rsync|internal-sftp|git|git-*)
    refuse "this key only starts Mobster tasks." ;;
esac

here=$(cd "$(dirname "$0")" && pwd) || refuse "it couldn't find mobster-task.sh."
unset LC_ALL
MOBSTER_WAIT=0 exec "$here/mobster-task.sh" "$text"
