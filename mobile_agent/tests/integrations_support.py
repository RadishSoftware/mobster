"""Temporary client homes and CLI helpers for offline integration tests."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import cli_parser, harness_api
from mobile_agent.integrations import cli, clients

MOBSTER = "/opt/mobster/bin/mobster"
SERVER = clients.Server(MOBSTER, ["mcp"])
KEY = "sk-test-0123456789abcdefghijklmnop"


class Home:
    """A temporary home folder in which every client looks installed (each one's folder exists)."""

    def __init__(self, folder, which=None, detect=True):
        self.path = Path(folder)
        if detect:
            for part in (".claude", ".codex", ".cursor", "Library/Application Support/Code/User",
                         "Library/Application Support/Claude", ".config/opencode", ".gemini", ".codeium/windsurf",
                         ".copilot", ".config/zed", ".config/goose", ".config/amp", ".cline", ".omp/agent",
                         "Library/Developer/Xcode/CodingAssistant"):
                (self.path / part).mkdir(parents=True, exist_ok=True)
        self.env = clients.Env(home=self.path, environ={"HOME": str(self.path), "PATH": "/usr/bin:/bin",
                                                        "OPENAI_API_KEY": KEY, "ANTHROPIC_API_KEY": KEY},
                               which=which or (lambda name, path=None: None), applications=())

    def file(self, client_id):
        return clients.BY_ID[client_id].paths(self.env)[0]


def parse(*argv):
    with mock.patch.object(harness_api.hooks(), "cli", []):
        return cli_parser.build_parser().parse_args(["mcp", *argv])


def run_install(home, *argv, server=None):
    args = parse("install", *argv)
    server = server or clients.Server(MOBSTER, ["mcp", *cli.server_args(args)])
    out = io.StringIO()
    code = cli.install(args, env=home.env, server=server, stream=out)
    return code, out.getvalue()


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Home(self.folder.name)
