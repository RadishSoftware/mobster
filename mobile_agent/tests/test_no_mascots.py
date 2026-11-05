import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PATHS = re.compile(
    r"(^|/)(mascots?(-[\w-]+)?|family-(geometry|tokens|mascot)|crew-parts|live-mascot|first-run-mo|use-gaze"
    r"|make_family\w*)(\.\w+)?(/|$)")
NAMES = re.compile(r"FamilyMascot|MascotParts|MascotPose|LiveMascot|FirstRunMo|MO_STATES|make_family"
                   r"|family-geometry|family-tokens"
                   r"|crew-parts|first-run-mo|use-gaze|brand/mascots|ui-mascot|fr-mascot|data-mascot")
HISTORY = re.compile(r"^(CHANGELOG\.md|docs/changelog\.mdx|research/)")
THIS_FILE = Path(__file__).relative_to(REPO).as_posix()


def tracked():
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], check=True, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return None
    return [name for name in out.split("\0") if name and (REPO / name).is_file()]


class NoMascotsTests(unittest.TestCase):
    def setUp(self):
        self.files = tracked()
        if self.files is None:
            self.skipTest("this tree has no git checkout to list")

    def test_no_file_is_a_mascot_or_draws_one(self):
        found = [name for name in self.files if PATHS.search(name) and not HISTORY.match(name)]
        self.assertEqual(found, [],
                         "a mascot's file is back: delete it, and use a real logo, the real UI or an icon instead")

    def test_no_source_names_a_mascot_component_or_generator(self):
        found = []
        for name in self.files:
            if name == THIS_FILE or HISTORY.match(name):
                continue
            path = REPO / name
            if path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            match = NAMES.search(text)
            if match:
                found.append(f"{name}: {match.group(0)}")
        self.assertEqual(found, [], "a mascot is named again")


if __name__ == "__main__":
    unittest.main()
