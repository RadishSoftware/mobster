"""Track `learned`: saved workflows that learn their steps from a good run and replay them with no model (the
accounts half of the replay core; FEATURE-BACKLOG v2, item 1). Part of Mobster for Mac, not the CLI: the public tree
leaves this package out, and tracks.load then records it as absent.

- workflows.py: the run listener that learns, the pre-run hook that replays, and what the Mac app shows;
- programs.py: the saved-program store, learning and code matching, for any Smart run: a request with the same words
  as one that ended done, and other values, replays that run with code checking each step;
- parts.py: spans of saved programs that a request of another shape may run (``MOBSTER_PROGRAMS=on``);
- taught.py: what a stopped lesson becomes (a taught program), and how a practice run's corrections change it;
- teach.py: the live-view recorder behind Runtime.teach_*;
- app_path.py: ``ProgramListener`` (keeps a done Smart run whole) and the program pre-run hook;
- routes.py: ``RouteMemory``, how iOS's own moves work on each phone;
- register.py: registers the listeners and hooks, and the ``/api/replay`` routes.

The engine itself (record, replay, preconditions, approvals) is replay_core, shared with `mobster test`."""
