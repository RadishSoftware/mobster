"""`python -m mobile_agent.verify …`: the same command as `mobster verify`, with the same flags and exit codes."""

import argparse
import os
from pathlib import Path
import sys


def build_parser():
    from .. import __main__ as mobster
    from ..devtools import TEXT
    from . import cli
    help_text, description, epilog = TEXT["verify"]
    parser = argparse.ArgumentParser(prog="mobster verify", description=description, epilog=epilog,
                                     formatter_class=mobster.Formatter)
    cli.add_arguments(parser, {"env_option": mobster._env_option, "path_type": mobster.path_type,
                               "bounded": mobster._bounded})
    mobster._tidy(parser)  # "show this help and exit", as under `mobster`
    return parser


def main(argv=None):
    from .. import tls
    from ..config import load_env_file
    from . import cli
    args = build_parser().parse_args(argv)
    env_file = getattr(args, "env_file", None)
    if env_file is None and os.environ.get("MOBSTER_ENV_FILE"):
        env_file = Path(os.environ["MOBSTER_ENV_FILE"]).expanduser()
    if env_file:
        for warning in load_env_file(env_file).warnings:
            print(f"mobster: {warning}", file=sys.stderr, flush=True)
    tls.ensure_ca_bundle()
    try:
        return cli.run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
