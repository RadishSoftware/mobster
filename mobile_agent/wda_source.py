"""The WebDriverAgent release the managed setup builds onto the user's phone.

Pinned so every user builds the source that was tested on the real phone, not
whatever appium's master holds on the day of their first setup. The tag names
the release; the commit is what is checked, since a tag can be moved upstream.
To bump: test the new release on the phone, then change both together.
"""

import subprocess

REPOSITORY = "https://github.com/appium/WebDriverAgent.git"
REF = "v16.12.10"
COMMIT = "00c38220c3e84906c965b996ffc4c12d09fef62f"


def clone_command(git, destination):
    return [git, "-c", "advice.detachedHead=false", "clone", "--depth", "1", "--branch", REF,
            REPOSITORY, str(destination)]


def checkout_commit(git, project):
    """The commit a checkout holds, or None when it is not a readable git checkout."""
    try:
        result = subprocess.run([git, "-C", str(project), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def is_pinned(git, project):
    return checkout_commit(git, project) == COMMIT


def verify(git, project):
    commit = checkout_commit(git, project)
    if commit != COMMIT:
        raise RuntimeError(f"WebDriverAgent {REF} is at {commit or 'an unknown commit'}, expected {COMMIT}")
