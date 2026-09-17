#!/usr/bin/env python3
import pathlib
import sys

binary = pathlib.Path(sys.argv[1])
size = binary.stat().st_size
print(f"{binary}: {size:,} bytes ({size / 1_000_000:.3f} MB)")
if size >= 6_000_000:
    raise SystemExit("Release rejected: binary must be smaller than 6 MB.")
