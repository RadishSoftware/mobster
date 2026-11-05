"""Known Mobster MCP launch options and safe CLI command display."""

# A saved launch option that this version cannot understand must stop a reinstall: dropping it could expand
# the set of phones or app builds the server can use.
MCP_VALUES = {"--env-file", "--device", "--allow-device", "--allow-testflight", "--runtime", "--out", "--log"}
MCP_SWITCHES = {"--keyless", "--allow-files", "--allow-photos"}


def _options(argv):
    """Parse a saved Mobster MCP launch as known (flag, value) pairs."""
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError("its arguments aren't a list of strings")
    tail = argv[2:] if argv[:2] == ["-m", "mobile_agent"] else argv
    options = []
    before_mcp, index = True, 0
    while index < len(tail):
        if before_mcp and tail[index] == "mcp":
            before_mcp = False
            index += 1
            continue
        flag, separator, inline = tail[index].partition("=")
        if before_mcp and flag not in {"--env-file", "--device"}:
            raise ValueError("it has an unrecognized option before `mcp`")
        if flag in MCP_VALUES:
            value = inline if separator else (tail[index + 1] if index + 1 < len(tail) else "")
            if not value or value.startswith("--"):
                raise ValueError(f"{flag} has no value")
            options.append((flag, value))
            index += 1 if separator else 2
        elif flag in MCP_SWITCHES and not separator:
            options.append((flag, None))
            index += 1
        else:
            raise ValueError("it has an unrecognized server option")
    if before_mcp:
        raise ValueError("its arguments don't name `mobster mcp`")
    return options


def _redact_cli_env(argv):
    safe = list(argv)
    for index, item in enumerate(safe[:-1]):
        if item in ("-e", "--env"):
            key = safe[index + 1].partition("=")[0]
            safe[index + 1] = f"{key}=<redacted>"
    return safe
