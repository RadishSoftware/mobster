"""Track registration: the run listener, only with MOBSTER_EXPERIMENTAL_CORPUS on."""

from . import enabled


def register(api):
    if enabled():
        from .listener import CorpusListener
        api.register_run_listener(CorpusListener())
