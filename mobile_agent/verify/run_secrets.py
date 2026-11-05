"""A check's ${NAME}s, expanded from the project's secrets (project_env.py) before any simulator is touched. A value
is never logged, and a replay never keeps a path that carries one."""

import dataclasses

from .. import project_env
from .verdict import CouldntRun


class ProjectSecrets:
    def _load_secrets(self):
        """Expand the check's ${NAME}s from the project's secrets, or refuse before any simulator is touched."""
        check = self.check
        names = project_env.refs(check.steps, check.launch_args, check.launch_env, check.open_url)
        if not names:
            return
        if self.replay is not None and (self.replay.mode == "strict" or self.replay.pin is not None):
            self.replay.refuse_pin("A fixed replay cannot use project secrets in its action path.")
        if getattr(self, "repeat_pin", None) is not None:
            raise CouldntRun("replay", "A fixed repeat cannot use project secrets in its action path.")
        root = self.runs_dir.parent.parent if self.runs_dir.parent.name == ".mobster" else None
        if root is None:
            raise CouldntRun("usage",
                             f"This check uses {', '.join(names)}, which come from a project's .mobster/env.example.",
                             "Run it from a project folder that has .mobster/, with --out left at its default.")
        try:
            self.secrets = project_env.ProjectEnv(root).resolve(names)
        except project_env.EnvError as error:
            raise CouldntRun("usage", str(error),
                             "Set the value with `mobster env set NAME`, then run the check again.") \
                from None
        # Screen signatures and search selectors can contain a value too; never persist a secret-bearing path.
        self.replay = None
        self._live = dataclasses.replace(
            check, launch_args=project_env.expand(check.launch_args, self.secrets),
            launch_env=project_env.expand(check.launch_env, self.secrets),
            open_url=project_env.expand(check.open_url, self.secrets),
            steps=project_env.expand(check.steps, self.secrets))
        self.log(f"secrets: {', '.join(names)} expanded from this project (values are never logged)")
