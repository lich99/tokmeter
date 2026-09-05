"""Discover source logs. File contents are read only by the native parser."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Sources:
    claude: Path
    codex: Path

    @classmethod
    def defaults(cls):
        return cls(
            Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects",
            Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser(),
        )

    def discover(self):
        files, errors = [], []
        # Archived sessions remain part of lifetime usage.
        roots = [(0, self.claude), (1, self.codex / "sessions"), (1, self.codex / "archived_sessions")]
        for source, root in roots:
            try:
                root.stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                errors.append(str(error))
                continue

            def failed(error):
                errors.append(str(error))

            for directory, _, names in os.walk(root, onerror=failed):
                for name in names:
                    if not name.endswith(".jsonl"):
                        continue
                    path = Path(directory) / name
                    try:
                        stat = path.stat()
                    except FileNotFoundError:
                        continue  # A concurrent rename is picked up on the next scan.
                    except OSError as error:
                        errors.append(str(error))
                        continue
                    project = decode_project(path.relative_to(root).parts[0]) if source == 0 else "(unknown)"
                    files.append(
                        dict(
                            path=str(path),
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                            identity=f"{stat.st_dev}:{stat.st_ino}",
                            source=source,
                            project=project,
                            sub="subagents" in path.parts,
                        )
                    )
        return files, errors


@lru_cache(maxsize=2048)
def decode_project(encoded):
    """Best-effort fallback for old Claude logs without an explicit cwd."""
    if not encoded.startswith("-"):
        return encoded
    parts = encoded[1:].split("-")
    path, i = Path("/"), 0
    while i < len(parts):
        found = None
        for length in range(min(16, len(parts) - i), 0, -1):
            candidate = "-".join(parts[i : i + length])
            if (path / candidate).is_dir():
                found = candidate, length
                break
        if found is None:
            found = parts[i], 1
        path /= found[0]
        i += found[1]
    return str(path)


def display_project(path):
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + os.sep) else path
