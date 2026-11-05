"""The terminal under `mobster chat`: lines printed above a prompt that keeps what you're typing."""

import codecs
import os
import select
import sys
import threading


class Term:
    """Lines printed above a prompt that keeps what you're typing."""

    def __init__(self, stdin=None, stdout=None, prompt="› "):
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.prompt, self.buffer = prompt, ""
        self.tty = isatty(self.stdin) and isatty(self.stdout)
        self.saved = None
        self.lock = threading.RLock()
        self.shown = False
        self.prompting = True       # whether the prompt line is drawn (one message: only while something waits)
        # A character split across two reads (an emoji typed or pasted) decodes once, whole.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def __enter__(self):
        if self.tty:
            import termios
            import tty
            try:
                fd = self.stdin.fileno()
                self.saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                mode = termios.tcgetattr(fd)
                mode[3] &= ~termios.ECHO  # this prompt draws what you type itself
                termios.tcsetattr(fd, termios.TCSANOW, mode)
            except (termios.error, OSError, ValueError):
                self.saved = None
        return self

    def __exit__(self, *exc):
        self.clear()
        if self.saved is not None:
            import termios
            try:
                termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self.saved)
            except (termios.error, OSError, ValueError):
                pass
        return False

    def write(self, text):
        try:
            self.stdout.write(text)
            self.stdout.flush()
        except (OSError, ValueError):
            pass

    def clear(self):
        with self.lock:
            if self.tty and self.shown:
                self.write("\r\033[2K")
            self.shown = False

    def show(self):
        with self.lock:
            if self.tty and self.prompting:
                self.write("\r\033[2K" + self.prompt + self.buffer)
                self.shown = True

    def print(self, text=""):
        with self.lock:
            self.clear()
            self.write(str(text) + "\n")
            self.show()

    def keys(self, timeout):
        """What was typed within ``timeout`` seconds ("" for nothing; None at the end of input)."""
        if not self.tty:
            line = self.stdin.readline()
            return None if line == "" else line if line.endswith("\n") else line + "\n"
        ready, _, _ = select.select([self.stdin], [], [], timeout)
        if not ready:
            return ""
        data = os.read(self.stdin.fileno(), 1024)
        return None if not data else self.decoder.decode(data)

    def discard_typeahead(self):
        """Forget every key typed before now that this prompt hasn't read yet (the terminal's input queue)."""
        if not self.tty:
            return
        import termios
        try:
            termios.tcflush(self.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass
        self.decoder.reset()


def isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
