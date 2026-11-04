#!/usr/bin/env python3
"""Fetch selected .map tiles from the pinned client-data ZIP using HTTP Range."""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import re
import urllib.request
import zipfile

URL = "https://github.com/wowgaming/client-data/releases/download/v20.0/Data.zip"
TILE_NAME = re.compile(r"^[0-9]{7}[.]map$")


class HTTPRangeFile(io.RawIOBase):
    def __init__(self, url: str):
        self.url = url
        self.pos = 0
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            self.size = int(response.headers["Content-Length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self.pos + offset
        elif whence == io.SEEK_END:
            target = self.size + offset
        else:
            raise ValueError("invalid whence")
        if target < 0:
            raise ValueError("negative seek")
        self.pos = target
        return self.pos

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("unbounded remote reads are disabled")
        if size == 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + size, self.size) - 1
        request = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 206:
                raise RuntimeError("server ignored the Range request")
            data = response.read()
        if len(data) != end - self.pos + 1:
            raise RuntimeError("short Range response")
        self.pos += len(data)
        return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tiles", nargs="+", help="seven-digit tile names, e.g. 0013240.map")
    parser.add_argument("--dest", type=Path, required=True, help="empty/new output directory")
    args = parser.parse_args()
    invalid = [name for name in args.tiles if not TILE_NAME.fullmatch(name)]
    if invalid:
        parser.error(f"invalid tile names: {invalid}")
    if args.dest.exists() and any(args.dest.iterdir()):
        parser.error("--dest must be a new or empty directory")
    args.dest.mkdir(parents=True, exist_ok=True)

    remote = HTTPRangeFile(URL)
    with zipfile.ZipFile(remote) as archive:
        for tile in args.tiles:
            name = f"maps/{tile}"
            data = archive.read(name)  # zipfile verifies the entry CRC.
            output = args.dest / tile
            temporary = output.with_suffix(".map.part")
            temporary.write_bytes(data)
            os.replace(temporary, output)
            print(f"{tile}: {len(data)} bytes, CRC verified")


if __name__ == "__main__":
    main()
