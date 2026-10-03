#!/usr/bin/env python3
"""mytar -- 自研 tar 归档工具（仅标准库）。

手写实现 ustar 格式，并兼容两类常见扩展，保证与 GNU tar、
Python tarfile 等工具双向互操作：
  - GNU 长名称扩展（'L' 长文件名 / 'K' 长链接名条目）
  - POSIX pax 扩展头（'x' 条目中的 path / linkname / mtime）

用法：
  mytar.py create ARCHIVE [-C DIR] [NAME ...]
  mytar.py list   ARCHIVE
  mytar.py extract ARCHIVE [-C DIR]
"""

import argparse
import os
import stat
import sys

BLOCK = 512
RECORD = 10240

TYPE_REG = b"0"
TYPE_DIR = b"5"
TYPE_SYMLINK = b"2"
TYPE_LONGLINK = b"L"
TYPE_LONGLINK_SYM = b"K"
TYPE_PAX = b"x"
TYPE_GLOBAL_PAX = b"g"

TYPE_TO_FLAG = {"file": TYPE_REG, "dir": TYPE_DIR, "symlink": TYPE_SYMLINK}


class Entry:
    __slots__ = ("name", "mode", "mtime", "type", "linkname", "data")

    def __init__(self, name, mode, mtime, type_, linkname="", data=b""):
        self.name = name
        self.mode = mode
        self.mtime = mtime
        self.type = type_
        self.linkname = linkname
        self.data = data

    def __repr__(self):
        return "Entry(%r, %s, %o)" % (self.name, self.type, self.mode)


def _enc(text):
    return text.encode("utf-8", "surrogateescape")


def _dec(raw):
    return raw.split(b"\0")[0].decode("utf-8", "surrogateescape")


def _field_num(value, length):
    raw = ("%0*o" % (length - 1, value)).encode("ascii")
    if len(raw) > length - 1:
        raise ValueError("numeric field overflow: %r" % (value,))
    return raw + b"\0"


def _field_str(text, length):
    return _enc(text)[:length].ljust(length, b"\0")


def _make_header(name, mode, size, mtime, typeflag, linkname=""):
    header = bytearray(BLOCK)
    header[0:100] = _field_str(name, 100)
    header[100:108] = _field_num(mode & 0o7777, 8)
    header[108:116] = _field_num(0, 8)
    header[116:124] = _field_num(0, 8)
    header[124:136] = _field_num(size, 12)
    header[136:148] = _field_num(int(mtime), 12)
    header[148:156] = b"        "
    header[156:157] = typeflag
    header[157:257] = _field_str(linkname, 100)
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    header[265:297] = _field_str("mytar", 32)
    header[297:329] = _field_str("mytar", 32)
    header[148:156] = ("%06o\0 " % sum(header)).encode("ascii")
    return bytes(header)


def _write_raw(out, name, mode, mtime, typeflag, linkname, data):
    out.write(_make_header(name, mode, len(data), mtime, typeflag, linkname))
    out.write(data)
    pad = (-len(data)) % BLOCK
    if pad:
        out.write(b"\0" * pad)


def _write_record(out, name, mode, mtime, typeflag, linkname, data):
    if len(_enc(name)) > 100:
        _write_raw(out, "././@LongLink", 0, mtime, TYPE_LONGLINK, "", _enc(name))
    if len(_enc(linkname)) > 100:
        _write_raw(out, "././@LongLink", 0, mtime, TYPE_LONGLINK_SYM, "", _enc(linkname))
    if typeflag == TYPE_DIR and not name.endswith("/"):
        name += "/"
    _write_raw(out, name, mode, mtime, typeflag, linkname, data)


def write_archive(archive_path, entries):
    with open(archive_path, "wb") as out:
        for entry in entries:
            _write_record(out, entry.name, entry.mode, entry.mtime,
                          TYPE_TO_FLAG[entry.type], entry.linkname, entry.data)
        out.write(b"\0" * BLOCK * 2)
        out.write(b"\0" * ((-out.tell()) % RECORD))


def entries_from_tree(root, names=None):
    entries = []

    def add(rel):
        full = os.path.join(root, rel)
        st = os.lstat(full)
        mode = stat.S_IMODE(st.st_mode)
        mtime = int(st.st_mtime)
        if stat.S_ISLNK(st.st_mode):
            entries.append(Entry(rel, mode, mtime, "symlink", os.readlink(full)))
        elif stat.S_ISDIR(st.st_mode):
            entries.append(Entry(rel, mode, mtime, "dir"))
            for child in sorted(os.listdir(full)):
                add(rel + "/" + child if rel else child)
        else:
            with open(full, "rb") as fh:
                entries.append(Entry(rel, mode, mtime, "file", data=fh.read()))

    for name in (names if names is not None else sorted(os.listdir(root))):
        add(name)
    return entries


def _parse_num(raw):
    if raw and raw[0] & 0x80:
        value = 0
        for byte in raw[1:]:
            value = (value << 8) | byte
        return value
    text = raw.split(b"\0")[0].strip()
    return int(text, 8) if text else 0


def _parse_pax(data):
    records = {}
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        length = int(data[pos:space])
        key, _, value = data[space + 1:pos + length - 1].partition(b"=")
        records[key.decode("ascii")] = value.decode("utf-8", "surrogateescape")
        pos += length
    return records


def read_archive(archive_path):
    entries = []
    long_name = None
    long_link = None
    pax = {}
    with open(archive_path, "rb") as fh:
        while True:
            block = fh.read(BLOCK)
            if len(block) < BLOCK or block == b"\0" * BLOCK:
                break
            stored_sum = _parse_num(block[148:156])
            check = bytearray(block)
            check[148:156] = b"        "
            if sum(check) != stored_sum:
                raise ValueError("bad header checksum near offset %d" % (fh.tell() - BLOCK))
            size = _parse_num(block[124:136])
            padded = (size + BLOCK - 1) // BLOCK * BLOCK
            data = fh.read(padded)[:size]
            typeflag = block[156:157]
            if typeflag == TYPE_LONGLINK:
                long_name = _dec(data)
                continue
            if typeflag == TYPE_LONGLINK_SYM:
                long_link = _dec(data)
                continue
            if typeflag == TYPE_PAX:
                pax.update(_parse_pax(data))
                continue
            if typeflag == TYPE_GLOBAL_PAX:
                continue
            name = pax.pop("path", None) or long_name or _dec(block[0:100])
            linkname = pax.pop("linkname", None) or long_link or _dec(block[157:257])
            if "mtime" in pax:
                mtime = float(pax.pop("mtime"))
            else:
                mtime = _parse_num(block[136:148])
            pax.clear()
            long_name = long_link = None
            if typeflag == TYPE_DIR or name.endswith("/"):
                entries.append(Entry(name, _parse_num(block[100:108]), mtime, "dir"))
            elif typeflag == TYPE_SYMLINK:
                entries.append(Entry(name, _parse_num(block[100:108]), mtime, "symlink", linkname))
            else:
                entries.append(Entry(name, _parse_num(block[100:108]), mtime, "file", data=data))
    return entries


def extract_archive(archive_path, dest):
    entries = read_archive(archive_path)
    dirs = []
    for entry in entries:
        target = os.path.join(dest, entry.name)
        if entry.type == "dir":
            os.makedirs(target, exist_ok=True)
            dirs.append(entry)
        elif entry.type == "symlink":
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            if os.path.lexists(target):
                os.unlink(target)
            os.symlink(entry.linkname, target)
        else:
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(entry.data)
            os.chmod(target, entry.mode)
            os.utime(target, (entry.mtime, entry.mtime))
    for entry in reversed(dirs):
        target = os.path.join(dest, entry.name)
        os.chmod(target, entry.mode)
        os.utime(target, (entry.mtime, entry.mtime))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mytar")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create")
    p_create.add_argument("archive")
    p_create.add_argument("-C", "--directory", default=".")
    p_create.add_argument("names", nargs="*")
    p_list = sub.add_parser("list")
    p_list.add_argument("archive")
    p_extract = sub.add_parser("extract")
    p_extract.add_argument("archive")
    p_extract.add_argument("-C", "--directory", default=".")
    args = parser.parse_args(argv)

    if args.cmd == "create":
        names = args.names or None
        write_archive(args.archive, entries_from_tree(args.directory, names))
    elif args.cmd == "list":
        for entry in read_archive(args.archive):
            print(entry.name)
    elif args.cmd == "extract":
        extract_archive(args.archive, args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
