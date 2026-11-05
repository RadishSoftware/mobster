"""Projects: the repos and folders Mobster for Mac works in. Part of Mobster for Mac, not the CLI: the public tree
leaves this package out, and tracks.load then records it as absent.

A project is one folder on this Mac, usually a git repository: its app (an Xcode project or workspace, or an Expo or
React Native app), its `.mobster/` (checks, the simulator matrix, and workflows as files) and its branch. The
folder is the source of truth. Mobster keeps only where it is, its name, how it came in and when it was last opened
(the journal's schema ``projects``); everything else is read from the folder when it is asked for (folder.py).

The rules this package keeps:

- Mobster writes only the project's workflow files, keeping their published graphs and files in step, and what Set
  up checks and Run checks make: Set up checks writes new files only, and Run checks builds into `.mobster/build`,
  which `.mobster/.gitignore` keeps out of git. Deleting a workflow removes its file only when it is unchanged.
  Forgetting a project forgets the record; the folder stays. A clone that fails removes only the folder that clone
  made.
- A project is the folder the person chose, never a folder above it: `/`, the home folder and the folders macOS
  keeps for everything (Documents, Desktop, Downloads, Library, Applications, Volumes) are refused, as is Mobster's
  own data folder.
- A clone runs `git clone` with no shell, no terminal prompt and no SSH prompt, from an https or SSH URL only, with
  no password in the URL. It never retries: a clone that failed says why and what to do.
- Nothing here names a real iPhone in a project's files, and Run checks runs on simulators only.

Files: ``register`` (the track), ``store`` (schema ``projects``), ``folder`` (choosing a folder and reading one),
``clone`` (cloning by URL), ``github`` (Import from GitHub), ``setup`` (a starter check), ``runs`` (Run checks),
``service`` (a Runtime's projects, clones and runs), ``routes`` (/api/projects), ``sync`` (keeping workflow files and
published graphs in step) and ``workflow_files`` (the file format).
"""
