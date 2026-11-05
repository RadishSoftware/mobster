import sys

from ..netxray import cli as network_cli
from . import app_cli, diff_cli

SUBCOMMANDS = (("app", app_cli), ("network", network_cli), ("diff", diff_cli))


def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="xray_command", metavar="<subcommand>")
    for name, module in SUBCOMMANDS:
        module.add_arguments(subs.add_parser(name, help=module.HELP, description=module.DESCRIPTION,
                                             epilog=module.EPILOG, formatter_class=parser.formatter_class))


def run(args):
    for name, module in SUBCOMMANDS:
        if args.xray_command == name:
            return module.run(args)
    print("mobster xray: choose a subcommand: " + ", ".join(name for name, _ in SUBCOMMANDS), file=sys.stderr)
    return 2
