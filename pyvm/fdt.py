"""Minimal flattened device tree (DTB) writer, so no `dtc` is needed."""

import struct

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_END = 1, 2, 3, 9


def u32(*vals):
    return struct.pack('>%dI' % len(vals), *vals)


def u64(*vals):
    return struct.pack('>%dQ' % len(vals), *vals)


def cells64(*vals):
    """Encode 64-bit values as pairs of 32-bit cells (#address-cells = 2)."""
    out = []
    for v in vals:
        out += [v >> 32, v & 0xFFFFFFFF]
    return u32(*out)


def string(*vals):
    return b''.join(v.encode() + b'\0' for v in vals)


class Node:
    def __init__(self, name):
        self.name = name
        self.props = []
        self.children = []

    def prop(self, name, value=b''):
        if isinstance(value, str):
            value = string(value)
        elif isinstance(value, int):
            value = u32(value)
        self.props.append((name, value))
        return self

    def child(self, name):
        n = Node(name)
        self.children.append(n)
        return n


def build(root, boot_cpuid=0):
    strings = bytearray()
    str_off = {}
    struct_blk = bytearray()

    def name_off(name):
        if name not in str_off:
            str_off[name] = len(strings)
            strings.extend(name.encode() + b'\0')
        return str_off[name]

    def pad():
        while len(struct_blk) % 4:
            struct_blk.append(0)

    def emit(node):
        struct_blk.extend(u32(FDT_BEGIN_NODE))
        struct_blk.extend(node.name.encode() + b'\0')
        pad()
        for name, value in node.props:
            struct_blk.extend(u32(FDT_PROP, len(value), name_off(name)))
            struct_blk.extend(value)
            pad()
        for c in node.children:
            emit(c)
        struct_blk.extend(u32(FDT_END_NODE))

    emit(root)
    struct_blk.extend(u32(FDT_END))

    header_size = 40
    rsvmap = u64(0, 0)
    off_rsvmap = header_size
    off_struct = off_rsvmap + len(rsvmap)
    off_strings = off_struct + len(struct_blk)
    total = off_strings + len(strings)
    header = u32(FDT_MAGIC, total, off_struct, off_strings, off_rsvmap, 17, 16,
                 boot_cpuid, len(strings), len(struct_blk))
    return header + rsvmap + bytes(struct_blk) + bytes(strings)
