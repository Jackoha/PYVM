"""Background region compiler (a child process; see jit.JIT.request).

Compiling Python source costs ~100 us per guest block, about as long as the
interpreter takes to run a thousand guest instructions, and a Linux boot meets
thousands of blocks that are hot enough to compile.  Doing that work in a
second process keeps it off the emulator's thread: the code keeps running in
tier 0 until its region arrives.

Protocol (stdin -> stdout), each message a 4-byte little-endian length and a
marshal blob:
    request   (req_id, page bytes, hot entry offsets)
    reply     (req_id, entries, marshalled code object, marshalled line table,
               call insns)
The worker exits when its stdin closes.
"""

import marshal
import os
import queue
import struct
import sys
import threading


def main():
    try:
        serve()
    except (OSError, EOFError, KeyboardInterrupt):
        pass
    # the emulator went away: leave without flushing into a closed pipe
    os._exit(0)


def serve():
    from pyvm.jit import build_region, compile_source
    rd = sys.stdin.buffer
    wr = sys.stdout.buffer
    hdr = struct.Struct('<I')
    todo = queue.Queue()

    def reader():
        # Drain requests as they come, even while a reply is blocked on a full
        # pipe; otherwise the emulator could block writing to us while we
        # block writing to it.
        try:
            while True:
                h = rd.read(4)
                if len(h) < 4:
                    break
                n, = hdr.unpack(h)
                todo.put(rd.read(n))
        finally:
            todo.put(None)
    threading.Thread(target=reader, daemon=True).start()
    while True:
        blob = todo.get()
        if blob is None:
            return
        req_id, data, seeds = marshal.loads(blob)
        try:
            entries, src, lines, calls = build_region(data, seeds)
            code = marshal.dumps(compile_source(src))
            lines = marshal.dumps(lines)
        except Exception:                   # report a failure as an empty reply
            import traceback
            traceback.print_exc()
            entries, code, lines, calls = [], b'', b'', []
        out = marshal.dumps((req_id, entries, code, lines, calls))
        wr.write(hdr.pack(len(out)) + out)
        wr.flush()


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
