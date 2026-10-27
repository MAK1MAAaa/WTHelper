"""Read only new, bounded native-log lines for the chest cooldown error."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import BinaryIO


MAX_READ_BYTES = 64 * 1024
MAX_LINE_BYTES = 8 * 1024
CHEST_REQUEST = "POST api/frontend/chests/open"
COOLDOWN_CODE = re.compile(r"(?<![A-Za-z0-9_])code=70006(?![0-9])")


class ChestErrorTail:
    """Identify a new 70006 chest error without returning native log contents."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._armed = False
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._discard_line = False

    def _forget_file(self) -> None:
        self._identity = None
        self._offset = 0
        self._partial = b""
        self._discard_line = False

    def _anchor(self, file: BinaryIO, info: os.stat_result) -> None:
        """Start at EOF, inspecting only the preceding byte for a partial line."""
        self._identity = (info.st_dev, info.st_ino)
        self._offset = info.st_size
        self._partial = b""
        self._discard_line = False
        if info.st_size:
            file.seek(info.st_size - 1)
            self._discard_line = file.read(1) != b"\n"

    def mark(self) -> None:
        """Start observing at the current EOF; old records are never scanned."""
        self._armed = True
        self._forget_file()
        try:
            with self.path.open("rb") as file:
                info = os.fstat(file.fileno())
                if stat.S_ISREG(info.st_mode):
                    self._anchor(file, info)
        except OSError:
            self._forget_file()

    def poll(self) -> str | None:
        """Return '70006' once for a matching complete line written after mark."""
        if not self._armed:
            return None
        try:
            with self.path.open("rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode):
                    self._forget_file()
                    return None
                identity = (info.st_dev, info.st_ino)
                if (self._identity != identity or info.st_size < self._offset
                        or info.st_size - self._offset > MAX_READ_BYTES):
                    self._anchor(file, info)
                    return None
                file.seek(self._offset)
                data = file.read(info.st_size - self._offset)
                self._offset += len(data)
        except OSError:
            self._forget_file()
            return None

        data = self._partial + data
        self._partial = b""
        if self._discard_line:
            index = data.find(b"\n")
            if index < 0:
                return None
            data = data[index + 1:]
            self._discard_line = False

        lines = data.split(b"\n")
        tail = lines.pop()
        if len(tail) > MAX_LINE_BYTES:
            self._discard_line = True
        else:
            self._partial = tail

        found = False
        for line in lines:
            if len(line) > MAX_LINE_BYTES:
                continue
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if CHEST_REQUEST in text and COOLDOWN_CODE.search(text):
                found = True
        return "70006" if found else None
