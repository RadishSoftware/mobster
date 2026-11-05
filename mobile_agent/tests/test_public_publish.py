import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUT = ROOT / "scripts" / "cut-public-tree.sh"
PUBLISH = ROOT / "scripts" / "publish-public-tree.sh"

PUBLIC_ID, PRIVATE_ID = "1388733109", "1380642287"

FAKE_GH = """#!/bin/sh
echo "$*" >> "$GH_LOG"
[ "$1" = api ] || exit 1
slug=${2#repos/}
line=$(grep -i "^$slug " "$GH_REPOS" | head -1)
[ -n "$line" ] || { echo "HTTP 404: Not Found" >&2; exit 1; }
echo "${line#* }"
"""


@unittest.skipUnless(CUT.is_file() and PUBLISH.is_file(),
                     "the publish scripts are maintainer tooling and are not part of this copy")
class PublishTargetTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        (self.dir / "bin").mkdir()
        gh = self.dir / "bin" / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.dir / "gh.log"
        self.log.write_text("")
        self.answers({
            "RadishSoftware/mobster-cli": f"{PUBLIC_ID} false RadishSoftware/mobster-cli",
            "RadishSoftware/mobster": f"{PRIVATE_ID} true RadishSoftware/mobster-app",
            "RadishSoftware/mobster-app": f"{PRIVATE_ID} true RadishSoftware/mobster-app",
            "RadishSoftware/mobster-releases": "1388232707 false RadishSoftware/mobster-releases",
            "RadishSoftware/other-private": "1000000001 true RadishSoftware/other-private",
        })

    def answers(self, table):
        self.repos = self.dir / "repos.txt"
        self.repos.write_text("".join(f"{slug} {line}\n" for slug, line in table.items()))

    def run_script(self, script, *args, gh=True):
        path = f"{self.dir / 'bin'}:/usr/bin:/bin" if gh else "/usr/bin:/bin"
        env = {"PATH": path, "HOME": str(self.dir), "GH_LOG": str(self.log), "GH_REPOS": str(self.repos),
               "TMPDIR": str(self.dir), "LANG": "en_US.UTF-8"}
        return subprocess.run([str(script), *args], capture_output=True, text=True, env=env, cwd=ROOT,
                              stdin=subprocess.DEVNULL, timeout=60)

    def verify(self, repo, **kwargs):
        return self.run_script(CUT, "--verify-repo", "--repo", repo, **kwargs)

    def test_the_public_repository_passes_by_its_id(self):
        done = self.verify("RadishSoftware/mobster-cli")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "RadishSoftware/mobster-cli")

    def test_the_old_private_name_is_refused_although_it_is_not_mobster_app(self):
        for repo in ("RadishSoftware/mobster", "https://github.com/RadishSoftware/mobster.git",
                     "git@github.com:RadishSoftware/mobster.git", "ssh://git@github.com/RadishSoftware/mobster",
                     "https://x-access-token:abc@github.com/RadishSoftware/mobster"):
            with self.subTest(repo=repo):
                done = self.verify(repo)
                self.assertEqual(done.returncode, 2)
                self.assertIn("private repository", done.stderr)
                self.assertEqual(done.stdout, "")

    def test_after_the_rename_the_new_name_passes_by_the_same_id(self):
        self.answers({"RadishSoftware/mobster": f"{PUBLIC_ID} false RadishSoftware/mobster",
                      "RadishSoftware/mobster-cli": f"{PUBLIC_ID} false RadishSoftware/mobster"})
        for repo in ("RadishSoftware/mobster", "RadishSoftware/mobster-cli"):
            with self.subTest(repo=repo):
                done = self.verify(repo)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.strip(), "RadishSoftware/mobster")

    def test_the_private_name_is_refused_before_asking_github(self):
        for repo in ("RadishSoftware/mobster-app", "radishsoftware/Mobster-App",
                     "git@github.com:RadishSoftware/mobster-app.git"):
            with self.subTest(repo=repo):
                done = self.verify(repo)
                self.assertEqual(done.returncode, 2)
                self.assertIn("private repository", done.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_any_private_repository_and_any_other_id_are_refused(self):
        done = self.verify("RadishSoftware/other-private")
        self.assertEqual(done.returncode, 2)
        self.assertIn("is a private repository", done.stderr)
        done = self.verify("RadishSoftware/mobster-releases")
        self.assertEqual(done.returncode, 2)
        self.assertIn(f"not the public repository's {PUBLIC_ID}", done.stderr)

    def test_no_answer_from_github_is_a_refusal(self):
        done = self.verify("RadishSoftware/no-such-repo")
        self.assertEqual(done.returncode, 2)
        self.assertIn("did not answer", done.stderr)
        done = self.verify("RadishSoftware/mobster-cli", gh=False)
        self.assertEqual(done.returncode, 2)
        self.assertIn("gh is not installed", done.stderr)
        self.assertEqual(self.verify("not a repo").returncode, 2)

    def test_publish_refuses_the_old_private_name_before_it_cuts_or_clones(self):
        for mode in ("--dry-run", "--push"):
            with self.subTest(mode=mode):
                done = self.run_script(PUBLISH, mode, "--repo", "RadishSoftware/mobster")
                self.assertEqual(done.returncode, 2)
                self.assertIn("private repository", done.stderr)
        self.assertNotIn("clone", self.log.read_text())
        self.assertEqual([p.name for p in self.dir.glob("mobster-publish.*")], [])

    def test_publish_needs_a_mode_and_never_force_pushes(self):
        self.assertEqual(self.run_script(PUBLISH).returncode, 2)
        text = PUBLISH.read_text()
        commands = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        pushes = [line.strip() for line in commands if line.strip().startswith("git") and " push " in line]
        self.assertEqual(pushes, ['git -C "$CLONE" push origin HEAD:refs/heads/main'],
                         "one plain push of one new commit")


if __name__ == "__main__":
    unittest.main()
