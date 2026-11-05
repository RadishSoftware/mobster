#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Ask Mobster
# @raycast.mode compact

# Optional parameters:
# @raycast.packageName Mobster
# @raycast.description Start a task on your iPhone with Mobster
# @raycast.argument1 { "type": "text", "placeholder": "What should Mobster do?" }

# A Raycast Script Command: add this folder in Raycast (Settings, Extensions, Script Commands, Add Directories).
# In compact mode Raycast shows the last line a script prints, and a failure for any exit but 0, so this hands the
# text to mobster-task.sh, one folder up, which always prints one line and exits 0.
exec "$(dirname "$0")/../mobster-task.sh" "${1-}"
