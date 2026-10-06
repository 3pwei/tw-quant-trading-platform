"""Inspect a stable SQLite + committed WAL snapshot entirely in RAM.

Never open the Production database through SQLite: even mode=ro can create shm.
Concurrent changes fail closed; no checkpoint, backup, temp file, or retry.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import struct

MAX_BYTES = 128 * 1024 * 1024


def require(condition, code):
    if not condition:
        raise ValueError(code)


def read_file(path):
    require(path.is_file() and not path.is_symlink() and path.resolve() == path,
            'sqlite-file-boundary')
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, 'sqlite-snapshot-too-large')
    return data


def checksum(data, state, endian):
    require(len(data) % 8 == 0, 'sqlite-wal-invalid')
    a, b = state
    for x, y in struct.iter_unpack(endian + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def committed_image(database, wal):
    require(database[:16] == b'SQLite format 3\x00' and len(database) >= 100,
            'sqlite-header-invalid')
    page_size = int.from_bytes(database[16:18], 'big')
    page_size = 65536 if page_size == 1 else page_size
    require(512 <= page_size <= 65536 and page_size & (page_size - 1) == 0 and
            len(database) % page_size == 0, 'sqlite-header-invalid')
    image = bytearray(database)
    if wal:
        require(len(wal) >= 32, 'sqlite-wal-invalid')
        magic, version, wal_page = struct.unpack('>III', wal[:12])
        require(magic in (0x377f0682, 0x377f0683) and version == 3007000 and
                wal_page == page_size, 'sqlite-wal-invalid')
        endian = '<' if magic == 0x377f0682 else '>'
        state = checksum(wal[:24], (0, 0), endian)
        require(state == struct.unpack('>II', wal[24:32]), 'sqlite-wal-invalid')
        frame_size = page_size + 24
        require((len(wal) - 32) % frame_size == 0, 'sqlite-wal-invalid')
        pending = {}
        for offset in range(32, len(wal), frame_size):
            header = wal[offset:offset + 24]
            page = wal[offset + 24:offset + frame_size]
            number, size = struct.unpack('>II', header[:8])
            require(header[8:16] == wal[16:24] and number > 0 and
                    number * page_size <= MAX_BYTES and size * page_size <= MAX_BYTES,
                    'sqlite-wal-invalid')
            state = checksum(header[:8] + page, state, endian)
            require(state == struct.unpack('>II', header[16:24]), 'sqlite-wal-invalid')
            pending[number] = page
            if size:
                length = size * page_size
                if length > len(image):
                    image.extend(b'\x00' * (length - len(image)))
                for number, content in pending.items():
                    if number <= size:
                        image[(number - 1) * page_size:number * page_size] = content
                del image[length:]
                pending.clear()
        # Uncommitted trailing frames are deliberately excluded.
    # deserialize uses an in-memory rollback-journal DB, not external WAL files.
    image[18:20] = b'\x01\x01'
    return bytes(image)


def inventory(image):
    con = sqlite3.connect(':memory:')
    try:
        con.deserialize(image)
        con.execute('PRAGMA query_only=ON')
        con.execute('PRAGMA temp_store=MEMORY')
        require(con.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'sqlite-integrity')
        hashes = {}
        tables = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for name, schema in tables:
            if name.startswith('sqlite_'):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            rows = con.execute('SELECT * FROM ' + quoted).fetchall()
            encoded = sorted(json.dumps(row, default=lambda v: {'bytes': v.hex()}, separators=(',', ':'))
                             for row in rows)
            hashes[name] = hashlib.sha256(json.dumps([schema, encoded], separators=(',', ':')).encode()).hexdigest()
        require('execution_targets' in hashes, 'durable-lock-missing')
        unlocked = con.execute("SELECT count(*) FROM execution_targets WHERE status IS NULL OR status != 'locked'").fetchone()[0]
        require(unlocked == 0, 'target-not-locked')
        count = con.execute('SELECT count(*) FROM execution_targets').fetchone()[0]
        return {'tables': hashes, 'targets': count, 'active_targets': unlocked, 'sqlite_integrity': 'ok'}
    finally:
        con.close()


def snapshot(path):
    path = Path(path)
    wal_path = path.with_name(path.name + '-wal')
    journal = path.with_name(path.name + '-journal')
    require(not journal.exists(), 'sqlite-journal-present')
    def read():
        return read_file(path), read_file(wal_path) if wal_path.exists() else b''
    first = read()
    result = inventory(committed_image(*first))
    require(read() == first and not journal.exists(), 'sqlite-changed-during-read')
    return result
