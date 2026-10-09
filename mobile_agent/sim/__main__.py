"""`python -m mobile_agent.sim …`: the same commands as `mobster sim …`, through the same add_arguments."""

import argparse
import sys


def build_parser():
    from .. import __main__ as mobster
    from . import cli
    parser = argparse.ArgumentParser(allow_abbrev=False, prog="mobster sim", formatter_class=mobster.Formatter,
                                     description="Manage the headless simulators that `mobster verify` and "
                                                 "`mobster mcp` run on.")
    cli.add_arguments(parser, {"env_option": mobster._env_option, "path_type": mobster.path_type,
                               "bounded": mobster._bounded})
    mobster._tidy(parser)  # "show this help and exit", as under `mobster`
    return parser


def main(argv=None):
    from . import cli
    args = build_parser().parse_args(argv)
    return cli.run(args)


if __name__ == "__main__":
    sys.exit(main())
