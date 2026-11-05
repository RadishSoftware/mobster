"""Claude Code as a Mobster task's agent, on the person's Claude plan (Pro or Max), with only the phone's tools."""

import json
import re

from .cli import AgentEvent, CliAgent, Command, child_env, classify, limit_signal, run_quietly, usage_counts

TOOL = re.compile(r"mcp__([A-Za-z0-9_-]+?)__(.+)")


class ClaudeCode(CliAgent):
    id = "claude_code"
    name = "Claude Code"
    binary = "claude"
    plan = "Claude"
    vendor = "Anthropic"
    install_url = "https://code.claude.com/docs/en/setup"
    install_command = "curl -fsSL https://claude.ai/install.sh | bash"
    sign_in_command = "claude, then /login"
    path_env = "MOBSTER_CLAUDE_CODE_PATH"
    model_env = "MOBSTER_CLAUDE_CODE_MODEL"
    # An API key bills per token instead of the plan; a parent session's variables make Claude Code think it is nested.
    drop_env = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_MODEL")

    def read_status(self, path):
        env = child_env(path, env=self.env, drop=self.drop_env)
        code, out, err = run_quietly([path, "auth", "status"], env)
        version = None
        vcode, vout, _ = run_quietly([path, "--version"], env)
        if vcode == 0:
            found = re.search(r"(\d+\.\d+\.\d+)", vout)
            version = found.group(1) if found else None
        state = {"version": version}
        if code is None:
            return {**state, "signedIn": False, "hint": f"Couldn't run {self.name}: {err[:120]}"}
        try:
            data = json.loads(out)
        except ValueError:
            data = {}
        if not isinstance(data, dict) or not data.get("loggedIn"):
            return {**state, "signedIn": False, "hint": "Sign in to Claude Code"}
        method = str(data.get("authMethod") or "")
        tier = str(data.get("subscriptionType") or "").strip()
        if method in ("claude.ai", "oauth", "subscription") or tier:
            name = {"max": "Max", "pro": "Pro", "team": "Team", "enterprise": "Enterprise"}.get(tier.lower(), tier)
            return {**state, "signedIn": True, "billing": "plan",
                    "account": f"Claude {name}".strip() if name else "Claude"}
        return {**state, "signedIn": True, "billing": "api_key", "account": "Claude API key"}

    def mcp_config(self, mcp_url, token):
        """The MCP config file's text (Mobster writes it 0600 in a private folder and removes it after the run)."""
        # alwaysLoad skips the tool-search hop; the timeout lets an approval wait.
        return json.dumps({"mcpServers": {"mobster": {"type": "http", "url": mcp_url,
                                                      "headers": {"Authorization": f"Bearer {token}"},
                                                      "alwaysLoad": True, "timeout": 900000}}})

    def argv(self, path, prompt, *, mcp_url, token_env, instructions, workdir, resume=None, model=None,
             config_path=None):
        argv = [path, "-p", "--output-format", "stream-json", "--verbose",
                "--tools", "", "--strict-mcp-config", "--mcp-config", str(config_path),
                "--allowedTools", "mcp__mobster__*", "--permission-mode", "dontAsk",
                "--setting-sources", "", "--disable-slash-commands", "--max-turns", "120",
                "--append-system-prompt", instructions]
        if version_at_least(self.version, (2, 1, 259)):
            # Anything that would still prompt is refused and Claude is told not to retry it.
            argv += ["--permission-prompts", "none"]
        if resume:
            argv += ["--resume", resume]
        if model or self.model():
            argv += ["--model", model or self.model()]
        # The prompt comes on stdin, so the person's task never shows in a process list.
        return Command(argv)

    def stdin(self, prompt, instructions=None):
        return prompt

    def extra_env(self, token_env, token):
        # Milliseconds: an approval or a question waits for the person.
        return {"MCP_TOOL_TIMEOUT": "900000", "MCP_TIMEOUT": "30000", "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

    def parse(self, obj):
        kind = obj.get("type") if isinstance(obj, dict) else None
        if kind == "system" and obj.get("subtype") == "init":
            out = [AgentEvent("session", session_id=obj.get("session_id") or None, model=obj.get("model"),
                              code=str(obj.get("apiKeySource") or "") or None)]
            servers = obj.get("mcp_servers") if isinstance(obj.get("mcp_servers"), list) else []
            mobster = next((s for s in servers if isinstance(s, dict) and s.get("name") == "mobster"), None)
            if mobster is not None:
                ok = mobster.get("status") in ("connected", "ok", None)
                out.append(AgentEvent("mcp", ok=ok, text=str(mobster.get("status") or "")))
            return out
        if kind == "assistant":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            out = []
            if isinstance(message.get("usage"), dict) and message.get("id"):
                out.append(AgentEvent("step_usage", call_id=str(message["id"])[:128],
                                      model=message.get("model") if isinstance(message.get("model"), str) else None,
                                      usage=_counts(message["usage"])))
            for block in message.get("content") or ():
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    out.append(AgentEvent("message", text=block["text"]))
                elif block.get("type") == "thinking" and str(block.get("thinking") or "").strip():
                    out.append(AgentEvent("reasoning", text=block["thinking"]))
                elif block.get("type") == "tool_use":
                    server, tool = _split(block.get("name"))
                    out.append(AgentEvent("tool_start", tool=tool, server=server,
                                          arguments=block.get("input") if isinstance(block.get("input"), dict) else {}))
            error = obj.get("error")
            if error:
                code = {"rate_limit": "usage_limit", "billing_error": "usage_limit",
                        "authentication_failed": "signed_out"}.get(str(error))
                said = " ".join(e.text for e in out if e.kind == "message") or str(error)
                event = self._error(said, fatal=False)
                event.code = code or event.code
                out = [e for e in out if e.kind != "message"] + [event]
            return out
        if kind == "user":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            out = []
            for block in message.get("content") or () if isinstance(message.get("content"), list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    failed = bool(block.get("is_error"))
                    out.append(AgentEvent("tool_end", ok=not failed, text=_text(block.get("content")) if failed
                                          else ""))
            return out
        if kind == "rate_limit_event":
            info = obj.get("rate_limit_info") if isinstance(obj.get("rate_limit_info"), dict) else {}
            out = []
            signal = limit_signal(info.get("status"), info.get("unifiedWindows"), kind=info.get("rateLimitType"),
                                  resets_at=info.get("resetsAt"))
            if signal is not None:
                out.append(AgentEvent("limits", limits=signal))
            if info.get("status") == "rejected":
                resets = info.get("resetsAt")
                out.append(AgentEvent("error", text="usage limit reached", code="usage_limit", fatal=False,
                                      resets_at=float(resets) if isinstance(resets, (int, float)) else None))
            return out
        if kind == "result":
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            models = {}
            for name, figures in (obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}).items():
                if isinstance(name, str) and isinstance(figures, dict):
                    cost = figures.get("costUSD")
                    models[name[:200]] = {
                        "usage": usage_counts(figures.get("inputTokens"), figures.get("outputTokens"),
                                              figures.get("cacheReadInputTokens"),
                                              figures.get("cacheCreationInputTokens"), input_includes_cache=False),
                        "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                        else None}
            cost, latency = obj.get("total_cost_usd"), obj.get("duration_api_ms")
            out = [AgentEvent("usage", usage=_counts(usage), models=models or None,
                              cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                              else None,
                              latency_ms=int(latency) if isinstance(latency, (int, float))
                              and not isinstance(latency, bool) and latency >= 0 else None)]
            text = obj.get("result") if isinstance(obj.get("result"), str) else ""
            failed = bool(obj.get("is_error")) or str(obj.get("subtype") or "success") != "success"
            if failed:
                out.append(self._error(text or str(obj.get("subtype") or "Claude Code stopped on an error."),
                                       fatal=True))
            out.append(AgentEvent("done", text=text if not failed else "", ok=not failed,
                                  session_id=obj.get("session_id") or None))
            return out
        return []

    def _error(self, message, fatal):
        code = classify(message)
        resets = None
        found = re.search(r"\|(\d{9,11})\b", message)  # older: "Claude AI usage limit reached|1760000000"
        if found:
            resets, code = float(found.group(1)), "usage_limit"
        clock = re.search(r"resets\s+(?:\w{3}\s+)?(\d{1,2}(?::\d{2})?\s*[ap]m)", message, re.I)
        if clock and code == "usage_limit":
            from .codex import parse_reset
            words = clock.group(1)
            resets = parse_reset(words if ":" in words else re.sub(r"(\d+)", r"\1:00", words, 1))
        return AgentEvent("error", text=message[:2000], code=code, fatal=fatal, resets_at=resets)


def _counts(usage):
    return usage_counts(usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cache_read_input_tokens"),
                        usage.get("cache_creation_input_tokens"), input_includes_cache=False)


def version_at_least(version, minimum):
    try:
        return tuple(int(part) for part in str(version).split(".")[:3]) >= minimum
    except ValueError:
        return False


def _split(name):
    found = TOOL.fullmatch(str(name or ""))
    return (found.group(1), found.group(2)) if found else (None, str(name or ""))


def _text(content):
    if isinstance(content, str):
        return content[:500]
    if isinstance(content, list):
        return " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))[:500]
    return ""
