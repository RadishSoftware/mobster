"""Gemini CLI as a Mobster task's agent, on the person's Google account."""

import json
import os
from pathlib import Path

from .cli import AgentEvent, CliAgent, Command, child_env, classify, run_quietly, usage_counts

BUILT_INS = ("run_shell_command", "write_file", "replace", "edit", "read_file", "read_many_files", "list_directory",
             "glob", "search_file_content", "grep", "web_fetch", "google_web_search", "save_memory", "write_todos",
             "activate_skill")
PLAN_AUTH = ("oauth-personal", "login-with-google", "cloud-shell", "compute-default-credentials")


class Gemini(CliAgent):
    id = "gemini"
    name = "Gemini CLI"
    binary = "gemini"
    plan = "Google"
    vendor = "Google"
    install_url = "https://github.com/google-gemini/gemini-cli"
    install_command = "npm install -g @google/gemini-cli"
    sign_in_command = "gemini"
    path_env = "MOBSTER_GEMINI_PATH"
    model_env = "MOBSTER_GEMINI_MODEL"
    drop_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT")
    resumes = False  # a follow-up gets the conversation in its first message instead
    signed_out_codes = (41,)

    def settings_path(self):
        home = Path(self.env.get("GEMINI_CLI_HOME") or Path(self.env.get("HOME") or "~").expanduser())
        return home / ".gemini" / "settings.json"

    def read_status(self, path):
        env = child_env(path, env=self.env, drop=self.drop_env)
        code, out, _ = run_quietly([path, "--version"], env)
        state = {"version": out.strip().split()[-1] if code == 0 and out.strip() else None}
        try:
            settings = json.loads(self.settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        auth = (((settings.get("security") or {}).get("auth") or {}).get("selectedType")
                or settings.get("selectedAuthType"))
        if auth in PLAN_AUTH:
            return {**state, "signedIn": True, "billing": "plan", "account": "Google account"}
        if auth in ("gemini-api-key", "vertex-ai", "USE_GEMINI", "USE_VERTEX_AI"):
            return {**state, "signedIn": True, "billing": "api_key", "account": "Gemini API key"}
        return {**state, "signedIn": False, "hint": "Sign in to Gemini CLI"}

    def workspace_settings(self, mcp_url, token):
        """.gemini/settings.json for the run's working folder: only Mobster's MCP server, trusted, no built-ins."""
        return json.dumps({"mcpServers": {"mobster": {"httpUrl": mcp_url,
                                                      "headers": {"Authorization": f"Bearer {token}"},
                                                      "trust": True, "timeout": 900000}},
                           "tools": {"exclude": list(BUILT_INS)}, "telemetry": {"enabled": False},
                           "general": {"disableAutoUpdate": True}})

    def argv(self, path, prompt, *, mcp_url, token_env, instructions, workdir, resume=None, model=None, **_):
        argv = [path, "-o", "stream-json", "--allowed-mcp-server-names", "mobster", "--skip-trust"]
        if model or self.model():
            argv += ["-m", model or self.model()]
        return Command(argv + ["-p", "-"])

    def stdin(self, prompt, instructions=None):
        # Gemini takes no system prompt on the command line: Mobster's instructions lead the task.
        return f"{instructions}\n\n{prompt}" if instructions else prompt

    def extra_env(self, token_env, token):
        return {"GEMINI_CLI_TRUST_WORKSPACE": "true", "NO_BROWSER": "true"}

    def prepare(self, workdir, mcp_url, token):
        """Write the run's project settings (0600); returns the path to remove after the run."""
        folder = Path(workdir) / ".gemini"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / "settings.json"
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.workspace_settings(mcp_url, token))
        return path

    def parser(self):
        """A parser for one run: assistant text arrives in pieces (delta) and is said whole at the next tool call."""
        state = {"buffer": [], "last": None, "model": None}

        def flush(out):
            text = "".join(state["buffer"]).strip()
            state["buffer"].clear()
            if text:
                out.append(AgentEvent("message", text=text))
                state["last"] = text

        def parse(obj):
            kind = obj.get("type") if isinstance(obj, dict) else None
            out = []
            if kind == "init":
                state["model"] = obj.get("model") if isinstance(obj.get("model"), str) else None
                return [AgentEvent("session", session_id=obj.get("session_id") or None, model=obj.get("model"))]
            if kind == "message" and obj.get("role") == "assistant":
                state["buffer"].append(str(obj.get("content") or ""))
                if not obj.get("delta"):
                    flush(out)
                return out
            if kind == "tool_use":
                flush(out)
                name = str(obj.get("tool_name") or "")
                server, tool = (("mobster", name[len("mcp_mobster_"):]) if name.startswith("mcp_mobster_")
                                else (None, name))
                out.append(AgentEvent("tool_start", tool=tool, server=server,
                                      arguments=obj.get("parameters") if isinstance(obj.get("parameters"), dict)
                                      else {}))
                return out
            if kind == "tool_result":
                failed = obj.get("status") == "error"
                error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
                return [AgentEvent("tool_end", ok=not failed, text=str(error.get("message") or "") if failed else "")]
            if kind == "error":
                message = str(obj.get("message") or "")
                return [AgentEvent("error", text=message[:2000], code=classify(message),
                                   fatal=obj.get("severity") == "error")]
            if kind == "result":
                flush(out)
                stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else {}
                # input_tokens includes what was cached; the model is the one init named.
                out.append(AgentEvent("usage", usage=usage_counts(stats.get("input_tokens"), stats.get("output_tokens"),
                                                                  stats.get("cached")), model=state["model"]))
                ok = obj.get("status") in ("success", None)
                if not ok:
                    error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
                    message = str(error.get("message") or obj.get("status") or "Gemini CLI stopped on an error.")
                    out.append(AgentEvent("error", text=message[:2000], code=classify(message), fatal=True))
                out.append(AgentEvent("done", text=(state["last"] or "") if ok else "", ok=ok))
            return out
        return parse

    def parse(self, obj):
        return self.parser()(obj)
