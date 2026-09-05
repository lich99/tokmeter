"""Native adapter and snapshot publication. Request handlers never scan source files."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace

import numpy as np
import polars as pl

from .pricing import Catalog
from .sources import Sources, display_project

RAW_COLUMNS = (
    "ts",
    "model",
    "project",
    "source",
    "is_sub",
    "tier",
    "in_t",
    "cached_t",
    "out_t",
    "reason_t",
    "cw5m",
    "cw1h",
    "cr",
)


@dataclass(frozen=True)
class Snapshot:
    frame: pl.DataFrame
    models: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    generation: int = 0
    ready: bool = False
    scanned_at: float = 0
    scan: dict | None = None
    error: str | None = None
    load_seconds: float = 0


class Store:
    def __init__(self, sources: Sources, catalog: Catalog, workers=8, interval=1.0):
        from ._native import Engine

        self.native, self.sources, self.catalog = Engine(), sources, catalog
        self.workers, self.interval = workers, interval
        empty = pl.DataFrame(schema={name: pl.Int64 for name in RAW_COLUMNS})
        self._snapshot = Snapshot(catalog.apply(empty, []))
        self._writer = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._republish = False

    def snapshot(self):
        return self._snapshot  # One immutable reference, replaced only after a complete build.

    def refresh(self):
        with self._writer:
            started = time.perf_counter()
            files, discovery_errors = self.sources.discover()
            if discovery_errors:
                # Do not interpret an unreadable directory as deleted historical data.
                raise OSError("; ".join(discovery_errors[:5]))
            metadata, buffer = self.native.refresh(
                json.dumps(files, separators=(",", ":")), self.workers, self._republish
            )
            meta = json.loads(metadata)
            old = self._snapshot
            if buffer is not None:
                self._republish = True
                if meta.get("schema") != 1 or len(buffer) != meta["rows"] * len(RAW_COLUMNS) * 8:
                    raise ValueError("Unsupported or truncated native data buffer")
                values = np.frombuffer(buffer, dtype="<i8").reshape((-1, len(RAW_COLUMNS)))
                frame = pl.DataFrame({name: values[:, i] for i, name in enumerate(RAW_COLUMNS)})
                frame = self.catalog.apply(frame, meta["models"]).sort("ts")
                old = Snapshot(
                    frame,
                    tuple(meta["models"]),
                    tuple(map(display_project, meta["projects"])),
                    old.generation + 1,
                    True,
                )
            self._snapshot = replace(
                old,
                scanned_at=time.time(),
                scan=meta["scan"],
                error=None,
                load_seconds=time.perf_counter() - started,
            )
            self._republish = False
            return self._snapshot

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Store already started")
        self._thread = threading.Thread(target=self._loop, name="tokmeter-reader", daemon=True)
        self._thread.start()

    def request_refresh(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.refresh()
            except Exception as error:
                self._snapshot = replace(self._snapshot, error=str(error))
            self._wake.wait(self.interval)

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()
