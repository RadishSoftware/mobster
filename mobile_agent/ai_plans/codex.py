"""Codex (OpenAI's coding agent CLI) as a Mobster task's agent, on the person's ChatGPT plan."""

import json
import re
import time

from .cli import AgentEvent, CliAgent, Command, child_env, classify, count, limit_signal, run_quietly, usage_counts

# Codex features that add tools beyond the phone's; an unknown name is ignored.
OFF_FEATURES = ("shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external", "computer_use",
                "in_app_browser", "image_generation", "multi_agent", "plugins", "remote_plugin", "tool_suggest",
                "skill_mcp_dependency_install", "goals", "hooks", "view_image")
# Passed back despite --ignore-user-config: without them a keychain sign-in reads as signed out.
AUTH_KEYS = ("cli_auth_credentials_store", "forced_login_method", "forced_chatgpt_workspace_id", "model_provider",
             "chatgpt_base_url")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
DEFAULT_EFFORT = "low"
RESETS = re.compile(r"try again (?:in|at) ([^.]+)", re.I)


WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}


def _usage(usage):
    """Codex's usage (input includes the cached part) in Mobster's convention."""
    return usage_counts(usage.get("input_tokens"), usage.get("output_tokens"),
                        usage.get("cached_input_tokens"), usage.get("cache_write_input_tokens"),
                        reasoning=usage.get("reasoning_output_tokens") if "reasoning_output_tokens" in usage else None)


def _limits(rate_limits):
    """A token_count's rate_limits as ``limit_signal``'s shape, or None when it has no readable window."""
    windows, resets, rejected = {}, None, False
    for slot in ("primary", "secondary"):
        window = rate_limits.get(slot) if isinstance(rate_limits, dict) else None
        used, minutes = (window or {}).get("used_percent"), (window or {}).get("window_minutes")
        if not isinstance(window, dict) or isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        at = window.get("resets_at")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            wait = window.get("resets_in_seconds")
            at = time.time() + wait if isinstance(wait, (int, float)) and not isinstance(wait, bool) else None
        name = WINDOW_NAMES.get(minutes) if isinstance(minutes, int) else None
        name = name or (f"{count(minutes)}_minutes" if minutes else slot)
        windows[name] = {"utilization": used / 100, "resetsAt": at}
        rejected = rejected or used >= 100
        resets = resets if resets is not None else at
    return limit_signal("rejected" if rejected else "allowed", windows, kind=next(iter(windows), None),
                        resets_at=resets) if windows else None


def toml_string(value):
    """A TOML basic string (JSON's escapes are TOML's)."""
    return json.dumps(str(value), ensure_ascii=True)


class Codex(CliAgent):
    id = "codex"
    name = "Codex"
    binary = "codex"
    plan = "ChatGPT"
    vendor = "OpenAI"
    install_url = "https://developers.openai.com/codex/cli"
    install_command = "npm install -g @openai/codex"
    sign_in_command = "codex login"
    path_env = "MOBSTER_CODEX_PATH"
    model_env = "MOBSTER_CODEX_MODEL"
    # An API key in Mobster's environment must not move Codex off the person's ChatGPT sign-in.
    drop_env = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
                "OPENAI_PROJECT_ID")

    def read_status(self, path):
        env = child_env(path, env=self.env, drop=self.drop_env)
        code, out, err = run_quietly([path, "login", "status"], env)
        said = f"{out}\n{err}".strip()
        version = None
        vcode, vout, verr = run_quietly([path, "--version"], env)
        if vcode == 0:
            found = re.search(r"(\d+\.\d+\.\d+[\w.-]*)", vout or verr)
            version = found.group(1) if found else None
        state = {"version": version}
        if code is None:
            return {**state, "signedIn": False, "hint": f"Couldn't run {self.name}: {err[:120]}"}
        if re.search(r"logged in using chatgpt", said, re.I):
            return {**state, "signedIn": True, "billing": "plan", "account": "ChatGPT"}
        if re.search(r"logged in using an? api key|logged in using api", said, re.I):
            # Signed in with an API key: it works, but each task bills that key.
            return {**state, "signedIn": True, "billing": "api_key", "account": "OpenAI API key"}
        if code == 0 and re.search(r"logged in", said, re.I) and not re.search(r"not logged in", said, re.I):
            return {**state, "signedIn": True, "billing": "plan", "account": "ChatGPT"}
        return {**state, "signedIn": False, "hint": "Sign in to Codex"}

    def user_config(self):
        """The few sign-in settings of the person's ~/.codex/config.toml (AUTH_KEYS) as -c overrides."""
        from pathlib import Path
        home = Path(self.env.get("CODEX_HOME") or Path(self.env.get("HOME") or "~").expanduser() / ".codex")
        try:
            import tomllib
            data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        except (ImportError, OSError, ValueError):
            return {}
        out = {key: data[key] for key in AUTH_KEYS if isinstance(data.get(key), str)}
        if isinstance(data.get("model"), str):
            out["model"] = data["model"]
        return out

    def effort(self):
        chosen = (self.env.get("MOBSTER_CODEX_EFFORT") or "").strip().lower()
        return chosen if chosen in EFFORTS else DEFAULT_EFFORT

    def argv(self, path, prompt, *, mcp_url, token_env, instructions, workdir, resume=None, model=None):
        mine = self.user_config()
        config = [
            'approval_policy="never"',
            'sandbox_mode="read-only"',
            f"mcp_servers.mobster.url={toml_string(mcp_url)}",
            f"mcp_servers.mobster.bearer_token_env_var={toml_string(token_env)}",
            # Exec denies MCP calls that need approval; Mobster's own card gates every irreversible act,
            # so Codex approves all.
            'mcp_servers.mobster.default_tools_approval_mode="approve"',
            # Required: an optional server gets about a second at startup, and the first turn could miss the tools.
            "mcp_servers.mobster.required=true",
            # Plain functions in the first tool list: Codex defers MCP tools behind code-mode search and a model that
            # skips it finds no phone (0.162).
            'mcp_servers.mobster.omit_tools_from=["deferred","code_mode"]',
            # An approval or a question waits for the person: the tool call may take minutes.
            "mcp_servers.mobster.tool_timeout_sec=900",
            "mcp_servers.mobster.startup_timeout_sec=30",
            f"developer_instructions={toml_string(instructions)}",
            f"model_reasoning_effort={toml_string(self.effort())}",
            'web_search="disabled"',
            "project_doc_max_bytes=0",
            "check_for_update_on_startup=false",
        ] + [f"features.{name}=false" for name in OFF_FEATURES] + [
            f"{key}={toml_string(value)}" for key, value in mine.items() if key in AUTH_KEYS]
        model = model or self.model() or mine.get("model")
        argv = [path, "exec"] + (["resume"] if resume else []) + [
            "--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
        if not resume:
            argv += ["--cd", str(workdir)]
        for item in config:
            argv += ["-c", item]
        if model:
            argv += ["-m", model]
        if resume:
            argv.append(resume)
        # The prompt comes on stdin ("-"), so the person's task never shows in a process list.
        argv.append("-")
        return Command(argv, model)  # the stream never names the model: the result says what Mobster asked for

    def stdin(self, prompt, instructions=None):
        return prompt

    def parse(self, obj):
        kind = obj.get("type") if isinstance(obj, dict) else None
        if kind == "thread.started":
            return [AgentEvent("session", session_id=str(obj.get("thread_id") or "") or None)]
        if kind == "turn.completed":
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            return [AgentEvent("usage", usage=_usage(usage))]
        if kind == "token_count" or (isinstance(obj, dict) and isinstance(obj.get("msg"), dict)
                                     and obj["msg"].get("type") == "token_count"):
            body = obj if kind == "token_count" else obj["msg"]
            out = []
            info = body.get("info") if isinstance(body.get("info"), dict) else {}
            last = info.get("last_token_usage")
            if isinstance(last, dict) and last:
                out.append(AgentEvent("step_usage", usage=_usage(last)))
            signal = _limits(body.get("rate_limits"))
            if signal is not None:
                out.append(AgentEvent("limits", limits=signal))
            return out
        if kind == "turn.failed":
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            return [self._error(error.get("message") or "Codex stopped on an error.", fatal=True)]
        if kind == "error":
            message = str(obj.get("message") or "")
            # Codex reports its own reconnects as errors ("Reconnecting... 2/5"): those aren't the end.
            if re.match(r"\s*(re-?connecting|retrying)", message, re.I):
                return []
            return [self._error(message, fatal=False)]
        if kind not in ("item.started", "item.updated", "item.completed"):
            return []
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = item.get("type") or item.get("item_type")
        done = kind == "item.completed"
        if itype == "agent_message" and done:
            return [AgentEvent("message", text=str(item.get("text") or ""))]
        if itype == "reasoning" and done:
            return [AgentEvent("reasoning", text=str(item.get("text") or ""))]
        if itype == "mcp_tool_call":
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            if kind == "item.started":
                return [AgentEvent("tool_start", tool=item.get("tool"), server=item.get("server"),
                                   arguments=arguments)]
            if done:
                error = item.get("error")
                failed = item.get("status") == "failed" or bool(error)
                text = (error.get("message") if isinstance(error, dict) else str(error or "")) if failed else ""
                return [AgentEvent("tool_end", tool=item.get("tool"), server=item.get("server"),
                                   arguments=arguments, ok=not failed, text=text)]
        if itype in ("command_execution", "file_change", "web_search", "collab_tool_call") and kind == "item.started":
            # Off for Mobster's runs; shown if a newer Codex does one anyway.
            what = item.get("command") or item.get("query") or itype.replace("_", " ")
            return [AgentEvent("tool_start", tool=itype, server=None, arguments={"what": str(what)[:200]})]
        if itype == "error" and done:
            return [AgentEvent("error", text=str(item.get("message") or ""), code="other", fatal=False)]
        return []

    def _error(self, message, fatal):
        message = unwrap(message)
        code = "model" if MODEL_REFUSED.search(message) else classify(message)
        resets = None
        found = RESETS.search(message)
        if code == "usage_limit" and found:
            resets = parse_reset(found.group(1))
        return AgentEvent("error", text=message[:2000], code=code, fatal=fatal, resets_at=resets)


MODEL_REFUSED = re.compile(r"model is not supported|model_not_found|does not exist or you do not have access", re.I)


def unwrap(message):
    """Codex passes an API error on as JSON text ({"type":"error","status":400,"error":{"message":...}})."""
    message = str(message or "").strip()
    if message.startswith("{"):
        try:
            data = json.loads(message)
        except ValueError:
            return message
        inner = data.get("error") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return data["message"]
    return message


def parse_reset(words, now=None):
    """Epoch seconds for Codex's "try again in 2 days 3 hours 5 minutes" or "at 3:15 PM", else None."""
    now = time.time() if now is None else now
    words = words.strip()
    total, matched = 0, False
    for amount, unit in re.findall(r"(\d+)\s*(day|hour|hr|minute|min|second|sec)s?", words, re.I):
        matched = True
        total += int(amount) * {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60,
                                "second": 1, "sec": 1}[unit.lower()]
    if matched:
        return now + total
    clock = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)?", words, re.I)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if clock.group(3):
            hour = hour % 12 + (12 if clock.group(3).upper() == "PM" else 0)
        local = time.localtime(now)
        target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1))
        return target if target > now else target + 86400
    return None
