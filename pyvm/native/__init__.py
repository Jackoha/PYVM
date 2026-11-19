"""Optional native core (core.c), compiled on first use and loaded with ctypes.

pyvm stays a Python program with no dependencies: if there is no C compiler,
or PYVM_NATIVE=0 / --no-native is given, load() returns None and the Python
interpreter and region compiler run everything, exactly as before.

With the core, the hart's common instructions run in C (see core.c for what
is and is not covered) and the rest still run in Python.
"""

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig

ENABLED = os.environ.get('PYVM_NATIVE', '1') != '0'
TLB_SIZE = 4096
EXIT_BUDGET, EXIT_TRAP, EXIT_PYTHON, EXIT_STOP = 0, 1, 2, 3

_HERE = os.path.dirname(os.path.abspath(__file__))
_lib = None
_tried = False
error = None            # why the core is not available, for --stats


class Hart(ctypes.Structure):
    _fields_ = [
        ('pc', ctypes.c_uint64),
        ('mstatus', ctypes.c_uint64),
        ('satp', ctypes.c_uint64),
        ('priv', ctypes.c_uint64),
        ('status', ctypes.c_uint64),
        ('cause', ctypes.c_uint64),
        ('tval', ctypes.c_uint64),
        ('count', ctypes.c_uint64),
        ('ram', ctypes.c_void_p),
        ('ram_size', ctypes.c_uint64),
        ('x', ctypes.c_void_p),
        ('f', ctypes.c_void_p),
        ('mtime', ctypes.c_uint64),
        ('mcounteren', ctypes.c_uint64),
        ('scounteren', ctypes.c_uint64),
        ('mip', ctypes.c_uint64),
        ('mie', ctypes.c_uint64),
        ('mideleg', ctypes.c_uint64),
        ('reservation', ctypes.c_uint64),
        ('instret', ctypes.c_uint64),
        ('medeleg', ctypes.c_uint64),
        ('stvec', ctypes.c_uint64),
        ('sepc', ctypes.c_uint64),
        ('scause', ctypes.c_uint64),
        ('stval', ctypes.c_uint64),
        ('sscratch', ctypes.c_uint64),
        ('fflags', ctypes.c_uint64),
        ('frm', ctypes.c_uint64),
        ('tlb', ctypes.c_uint64 * (3 * TLB_SIZE)),     # (tag, off, perm) entries
    ]


def _cache_dir():
    return os.environ.get('PYVM_CACHE_DIR') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'),
        'pyvm')


def _build():
    global error
    src = os.path.join(_HERE, 'core.c')
    with open(src, 'rb') as f:
        code = f.read()
    extra = os.environ.get('PYVM_NATIVE_CFLAGS', '').split()
    tag = hashlib.sha1(code + sys.platform.encode() + ' '.join(extra).encode() +
                       (sysconfig.get_platform() or '').encode()).hexdigest()[:16]
    out_dir = _cache_dir()
    so = os.path.join(out_dir, 'core-%s.so' % tag)
    if os.path.exists(so):
        return so
    cc = os.environ.get('CC') or shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
    if not cc:
        error = 'no C compiler found'
        return None
    try:
        os.makedirs(out_dir, exist_ok=True)
        tmp = '%s.%d.tmp' % (so, os.getpid())
        p = subprocess.run([cc, '-O2', '-frounding-math', '-shared', '-fPIC'] + extra + ['-o', tmp, src, '-lm'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        if p.returncode:
            error = 'compiling core.c failed: %s' % p.stdout.decode(errors='replace')[-300:]
            return None
        os.replace(tmp, so)
    except (OSError, subprocess.SubprocessError) as e:
        error = 'compiling core.c failed: %s' % e
        return None
    return so


def load():
    """The ctypes library, or None when the core is off or cannot be built."""
    global _lib, _tried, error
    if not ENABLED:
        error = 'disabled'
        return None
    if _tried:
        return _lib
    _tried = True
    so = _build()
    if so is None:
        return None
    try:
        lib = ctypes.CDLL(so)
    except OSError as e:
        error = 'loading the core failed: %s' % e
        return None
    lib.run.argtypes = [ctypes.POINTER(Hart), ctypes.c_uint64]
    lib.run.restype = None
    lib.flush.argtypes = [ctypes.POINTER(Hart)]
    lib.flush.restype = None
    _lib = lib
    return lib
