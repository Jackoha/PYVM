"""Host terminal <-> guest serial console.

Input is read by a background thread (so the emulator never blocks on stdin).
When stdin is a terminal it is switched to raw mode; press Ctrl-A then X to
quit the VM (Ctrl-A Ctrl-A sends a literal Ctrl-A).
"""

import os
import sys
import threading
from collections import deque


class Console:
    def __init__(self, stdin_fd=0, stdout=None):
        self.out = stdout or sys.stdout.buffer
        self.buf = bytearray()
        self.inq = deque()
        self.event = threading.Event()
        self.quit_requested = False
        self.fd = stdin_fd
        self.saved_tty = None
        self.tty = False
        try:
            self.tty = os.isatty(stdin_fd)
        except OSError:
            pass
        if self.tty:
            import termios
            self.saved_tty = termios.tcgetattr(stdin_fd)
            t = termios.tcgetattr(stdin_fd)
            t[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR | termios.IGNCR)
            t[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            t[6][termios.VMIN] = 1
            t[6][termios.VTIME] = 0
            termios.tcsetattr(stdin_fd, termios.TCSANOW, t)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        escape = False
        while True:
            try:
                data = os.read(self.fd, 256)
            except OSError:
                return
            if not data:
                return
            for b in data:
                if self.tty:
                    if escape:
                        escape = False
                        if b in (ord('x'), ord('X')):
                            self.quit_requested = True
                            self.event.set()
                            return
                        if b != 1:
                            continue
                    elif b == 1:
                        escape = True
                        continue
                self.inq.append(b)
            self.event.set()

    # ------------------------------------------------------------------ guest side
    def write(self, data):
        self.buf += data

    def flush(self):
        if self.buf:
            try:
                self.out.write(self.buf)
                self.out.flush()
            except (BrokenPipeError, ValueError):
                pass
            self.buf.clear()

    def has_input(self):
        return bool(self.inq)

    def getc(self):
        try:
            return self.inq.popleft()
        except IndexError:
            return None

    def wake(self):
        """Interrupt a thread sleeping in wait() (used by the network backend)."""
        self.event.set()

    def wait(self, timeout):
        """Sleep until input arrives or `timeout` seconds pass.  True if input."""
        self.flush()
        if self.inq:
            return True
        self.event.wait(timeout)
        self.event.clear()
        return bool(self.inq)

    def close(self):
        self.flush()
        if self.saved_tty is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved_tty)
            self.saved_tty = None


class BufferConsole(Console):
    """Headless console for tests: output is collected, input is pushed with feed()."""

    def __init__(self):
        self.out = None
        self.buf = bytearray()
        self.output = bytearray()
        self.inq = deque()
        self.event = threading.Event()
        self.quit_requested = False
        self.tty = False
        self.saved_tty = None

    def feed(self, data):
        self.inq.extend(data)
        self.event.set()

    def flush(self):
        self.output += self.buf
        self.buf.clear()
