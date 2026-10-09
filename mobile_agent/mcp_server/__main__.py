"""`python -m mobile_agent.mcp_server`: the same server as `mobster mcp`, with the same flags."""

import argparse
import sys

DESCRIPTION = (
    "Serve Mobster's tools to a coding agent over MCP (stdio). The agent starts a check\n"
    "with verify_start, drives the app on a headless simulator, and gets the verdict\n"
    "from verify_finish. With an OpenAI or Anthropic key, verify runs the steps itself\n"
    "(Smart), on the model Smart picks for that key or the one MOBSTER_SMART_MODEL names.")


def build_parser():
    from pathlib import Path
    from .cli import add_arguments
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.mcp_server", description=DESCRIPTION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    add_arguments(parser, {"path_type": lambda value: Path(value).expanduser()})
    return parser


def main(argv=None):
    from .cli import run
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
