"""A drafting client on the person's own coding agent that answers once in JSON."""

import json
import subprocess
import tempfile

from . import agents
from .cli import child_env

COMMANDS = {
    "claude_code": lambda path, system: [path, "-p", "--output-format", "text", "--tools", "", "--setting-sources", "",
                                         "--disable-slash-commands", "--max-turns", "1", "--append-system-prompt",
                                         system],
    "codex": lambda path, system: [path, "exec", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                                   "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
                                   "-c", 'web_search="disabled"', "-"],
}


def first_object(text):
    """The first complete JSON object in ``text``, or None."""
    decoder, start = json.JSONDecoder(), str(text or "").find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text[start:])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


class AgentDrafter:
    """``complete`` on one ready coding agent. ``run(argv, stdin, env, timeout)`` is the process seam (tests)."""

    def __init__(self, agent_id, run=None):
        self.agent_id, self.model, self.provider = agent_id, agent_id, "plan"
        self.run = run or self._run
        self.hedger = None

    @staticmethod
    def _run(argv, stdin, env, timeout):
        with tempfile.TemporaryDirectory(prefix="mobster-draft-") as folder:
            done = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, env=env,
                                  cwd=folder, start_new_session=True)
        if done.returncode != 0:
            raise RuntimeError(f"the agent exited with {done.returncode}")
        return done.stdout

    def complete(self, messages, schema, timeout=60, **_):
        agent = agents.get(self.agent_id)
        path = agent.locate()
        build = COMMANDS.get(self.agent_id)
        if not path or build is None:
            raise RuntimeError(f"{agent.name} can't draft here")
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system"
                             and isinstance(m["content"], str))
        user = "\n\n".join(part.get("text", "") for m in messages if m.get("role") == "user"
                           for part in (m["content"] if isinstance(m["content"], list) else [{"text": m["content"]}]))
        ask = (f"{user}\n\nAnswer with one JSON object that matches this JSON schema, and nothing else:\n"
               f"{json.dumps(schema)}")
        text = self.run(build(path, system), ask, child_env(path, env=agent.env, drop=getattr(agent, "drop_env", ())),
                        timeout)
        out = first_object(text)
        if out is None:
            raise ValueError("the agent didn't answer in JSON")
        return out, {}
