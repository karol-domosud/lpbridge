# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Identify the source loaded at service startup, even after app replacement."""
import hashlib
from pathlib import Path
import time


def source_build_id(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    sources = list(root.rglob('*.py')) + list((root / 'data').glob('*.json.gz'))
    for path in sorted(sources):
        digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0')
        digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()


# Capture once, not at /api/health request time: on-disk files can be replaced
# while this Python process continues to run the old imported modules.
BUILD_ID = source_build_id()
STARTED_AT = time.time()
