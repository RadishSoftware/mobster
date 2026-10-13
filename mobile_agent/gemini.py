"""The operator's gcloud login for Google's models on Vertex AI, and whether a helper model is set up.

Tokens are captured in memory, sent only to Google's fixed endpoint, never logged.
No API enablement, billing changes, credential-file edits, or automatic retries.
"""
import os
import re
import subprocess
import threading
import time

from .transport import Deadline, TransportError


def gcloud_path():
    """gcloud, also when the desktop app was opened from Finder with a minimal PATH."""
    from pathlib import Path
    import shutil
    for candidate in ('/opt/homebrew/bin/gcloud', '/usr/local/bin/gcloud',
                      str(Path.home() / 'google-cloud-sdk' / 'bin' / 'gcloud'),
                      '/opt/homebrew/share/google-cloud-sdk/bin/gcloud'):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which('gcloud') or 'gcloud'


class GCloudToken:
    _lock = threading.Lock()
    _token = ""
    _until = 0
    # gcloud may return an already-cached token, so its default one-hour lifetime is
    # not one fresh hour from this call. Keep our extra cache short and invalidate on401.
    CACHE_SECONDS = 300

    @classmethod
    def get(cls, timeout=10):
        deadline = Deadline(timeout)
        if not cls._lock.acquire(timeout=deadline.remaining()):
            raise TimeoutError("Google authentication is busy")
        try:
            if cls._token and time.monotonic() < cls._until:
                deadline.remaining()
                return cls._token
            try:
                result = subprocess.run([gcloud_path(), 'auth', 'print-access-token', '--quiet'],
                    capture_output=True, text=True, timeout=min(10, deadline.remaining()), check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise TransportError("Existing Google Cloud credentials are unavailable") from None
            token = result.stdout.strip()
            if (result.returncode or not token or len(token) > 16384
                    or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token)):
                raise TransportError("Existing Google Cloud credentials could not authenticate")
            deadline.remaining()
            cls._token, cls._until = token, time.monotonic() + cls.CACHE_SECONDS
            return token
        finally:
            cls._lock.release()

    @classmethod
    def invalidate(cls, token):
        if not cls._lock.acquire(blocking=False):
            return  # Another request is already refreshing; do not extend this one's deadline.
        try:
            if cls._token == token:
                cls._token, cls._until = '', 0
        finally:
            cls._lock.release()


# Image input limits for one request: enough for a screen of grid cells or all
# photos of one profile, small enough that one call never uploads megabytes.
def configured():
    if os.environ.get('TEXT_MODEL_PROVIDER') == 'vertex':
        return bool(os.environ.get('GOOGLE_CLOUD_PROJECT') and os.environ.get('TEXT_MODEL'))
    return bool(os.environ.get('TEXT_MODEL_API_KEY') and os.environ.get('TEXT_MODEL'))
