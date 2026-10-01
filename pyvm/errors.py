class Trap(Exception):
    """A synchronous RISC-V exception (cause, tval)."""
    __slots__ = ('cause', 'tval')

    def __init__(self, cause, tval=0):
        self.cause = cause
        self.tval = tval


class Break(Exception):
    """Leave the current instruction batch early (cpu.pc is already updated).

    Raised after instructions that change translation, privilege or interrupt
    state, so the run loop can re-fetch and re-check interrupts.
    """


class Refetch(Break):
    """The decoded page being executed was invalidated; re-fetch at cpu.pc."""


class VMError(Exception):
    """Unrecoverable emulator condition (bad image, trap with no handler, ...)."""


class NetError(VMError):
    """The network backend could not be set up (bad --net spec, no TAP, ...)."""


class Halt(Exception):
    """The guest asked to power off / reset."""

    def __init__(self, code=0, reason='poweroff'):
        self.code = code
        self.reason = reason
