# SPDX-License-Identifier: Apache-2.0
"""Build an executable launcher that forwards argv to ``fake_cli.py`` verbatim.

``ABSTUDIO_CLI_PATH`` must point at something executable, so tests need a small
wrapper around the Python script. Getting that wrapper right on Windows matters
more than it looks: a ``.cmd`` file using ``%*`` re-parses the argument string
through ``cmd.exe``, which mangles arguments containing newlines — a prompt is
then silently truncated at its first line break, and the resulting failure looks
like a flag-contract bug rather than a launcher bug.

So on Windows we emit a tiny ``.exe``-equivalent: a Python launcher script
executed directly, bypassing ``cmd.exe`` entirely. On POSIX a ``sh`` wrapper with
``"$@"`` preserves arguments exactly and is fine.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

_FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"


def make_launcher(directory: str, *, fake_cli: str = "") -> str:
    """Create a launcher in ``directory`` and return its absolute path."""
    target = os.path.abspath(fake_cli or str(_FAKE_CLI))

    if sys.platform == "win32":
        # A .bat/.cmd shim would round-trip argv through cmd.exe and corrupt any
        # argument containing a newline. Python's own launcher does not.
        path = os.path.join(directory, "fakecli.bat")
        # `%*` is unavoidable in a batch file, so instead of forwarding args we
        # exec Python directly with the script and let it read the real argv.
        Path(path).write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{target}" %*\r\n',
            encoding="utf-8",
        )
        return path

    path = os.path.join(directory, "fakecli.sh")
    Path(path).write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{target}" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


__all__ = ["make_launcher"]
