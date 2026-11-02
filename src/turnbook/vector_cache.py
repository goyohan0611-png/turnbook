"""Binary vector cache: float32 rows in one file, keys in a small index; float16 in memory.

The JSON cache this replaces held every dimension as a Python float object — a 1.1GB file became
3-4GB of RAM and took minutes to parse, which is what got two evaluation runs killed for memory
pressure. Same vectors as float32 are ~8x smaller and load by reading bytes. In memory they are
float16, half again (a million pieces: 1.5 GB, not 3): cosine moves by ~1e-4, far below the gaps
between ranks. The file stays float32, so caches written before read unchanged.

Layout:  <dir>/vectors-<id>.f32  rows of `dim` little-endian floats, appended in insertion order
         <dir>/index.json       {"dim", "model", "keys": {sha256: row}, "blob": "vectors-<id>.f32"}

Several processes share one cache. A save appends only when the committed row numbering agrees;
otherwise it writes a new blob. The committed prefix is never changed, and index.json is swapped
atomically after the data is written. Readers ignore any unpublished tail. Rewriting existing rows
in place could pair one process's index with another's vectors. Older caches still read vectors.f32.

    python vector_cache.py            # self-check
    python vector_cache.py old.json   # convert a legacy JSON cache next to it
"""
from __future__ import annotations

from array import array
from pathlib import Path
import json
import os
import sys
import threading
import time

import numpy as np

DISK = 4                                   # bytes per value in the file (float32)
CHUNK = 1 << 20                            # values converted at a time: no full-size float32 copy


def _half(raw: bytes) -> array:
    """float32 bytes from the file -> float16 values (as raw uint16) for memory."""
    out = array("H")
    for start in range(0, len(raw), CHUNK * DISK):
        part = np.frombuffer(raw, dtype="<f4", count=min(CHUNK, (len(raw) - start) // DISK),
                             offset=start)
        out.frombytes(part.astype(np.float16).tobytes())
    return out


def _full(rows: array, start: int = 0):
    """float16 values in memory -> float32 bytes for the file, a chunk at a time."""
    for at in range(start, len(rows), CHUNK):
        part = np.array(rows[at:at + CHUNK], dtype=np.uint16).view(np.float16)
        yield part.astype("<f4").tobytes()


class VectorCache:
    def __init__(self, directory, dim: int | None = None, model: str = ""):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        meta, rows, self._stamp = self._read()
        self.blob_path = self.dir / meta.get("blob", "vectors.f32")
        self.dim = meta.get("dim") or dim
        self.model = meta.get("model") or model
        self.keys: dict[str, int] = meta.get("keys", {})
        self._rows = rows
        self._dirty = 0
        # bumped by clear(): a process holding an older generation drops it on its next refresh,
        # or its next save would write the discarded vectors back
        self.generation = meta.get("generation", 0)
        # refresh() and put() both append rows; a background embedding thread and a question's
        # refresh interleaving would pair keys with other texts' rows
        self._lock = threading.RLock()

    def _read(self):
        """(index, rows, index mtime) as saved on disk; empty when nothing is saved yet."""
        for _ in range(20):                # a save between reading the index and its blob
            try:
                stamp = self.index_path.stat().st_mtime_ns
                meta = json.loads(self.index_path.read_text(encoding="utf-8"))
                rows = array("H")
                if meta.get("dim"):
                    count = (max(meta.get("keys", {}).values(), default=-1) + 1) * meta["dim"]
                    with (self.dir / meta.get("blob", "vectors.f32")).open("rb") as handle:
                        rows = _half(handle.read(count * DISK))
                # An index whose blob is gone or short (another process saved and removed it in
                # between) must never load: its keys would point at rows that this process then
                # fills with other texts' vectors and saves. Every vector silently wrong.
                if meta.get("keys") and len(rows) < (max(meta["keys"].values()) + 1) * meta["dim"]:
                    raise ValueError("index ahead of its blob")
                return meta, rows, stamp
            except FileNotFoundError:
                if not self.index_path.exists():
                    return {}, array("H"), None
                time.sleep(0.05)
            except (OSError, ValueError):
                time.sleep(0.05)
        return {}, array("H"), None        # start empty: re-embedding costs time, never accuracy

    def refresh(self) -> int:
        with self._lock:
            return self._refresh()

    def _refresh(self) -> int:
        """Take in the vectors other processes saved since this one last looked. The capture
        service embeds new turns as it stores them; a memory server that started earlier sees
        them here instead of embedding them all again. Cheap when nothing changed (one stat)."""
        try:
            if self.index_path.stat().st_mtime_ns == self._stamp:
                return 0
        except OSError:
            return 0
        meta, rows, stamp = self._read()
        if meta and meta.get("generation", 0) < self.generation:
            return 0                       # this process is clearing it: nothing on disk counts
        if meta and meta.get("generation", 0) > self.generation:
            self.keys, self._rows = meta.get("keys", {}), rows
            self.dim, self.generation = meta.get("dim") or self.dim, meta.get("generation", 0)
            self.blob_path = self.dir / meta.get("blob", "vectors.f32")
            self._stamp, self._dirty = stamp, 0
            return len(self.keys)
        dim, added = meta.get("dim"), 0
        if dim and (self.dim is None or dim == self.dim):
            self.dim = self.dim or dim
            for key, row in meta.get("keys", {}).items():
                if key not in self.keys:
                    self.keys[key] = len(self._rows) // dim
                    self._rows.extend(rows[row * dim:(row + 1) * dim])
                    added += 1
        self._stamp = stamp
        return added

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def __len__(self) -> int:
        return len(self.keys)

    def get(self, key: str):
        with self._lock:
            row = self.keys.get(key)
            if row is None:
                return None
            start = row * self.dim
            return np.array(self._rows[start:start + self.dim], dtype=np.uint16).view(
                np.float16).astype(np.float32)

    def put(self, key: str, vector) -> None:
        with self._lock:
            self._put(key, vector)

    def _put(self, key: str, vector) -> None:
        if key in self.keys:
            return
        vector = array("H", np.asarray(vector, dtype=np.float16).view(np.uint16).tobytes())
        if self.dim is None:
            self.dim = len(vector)
        if len(vector) != self.dim:
            raise ValueError(f"expected {self.dim} dims, got {len(vector)}")
        self.keys[key] = len(self._rows) // self.dim
        self._rows.extend(vector)
        self._dirty += 1

    def flush(self) -> None:
        with self._lock:
            self._flush()

    def _flush(self) -> None:
        if not self._dirty and self.blob_path.exists():
            return
        import fasteners
        # One saver at a time, and each merges what is on disk first: otherwise the last writer
        # wins and the vectors only the other process had are dropped and embedded again.
        with fasteners.InterProcessLock(str(self.dir / "save.lock")):
            self._refresh()
            meta = (json.loads(self.index_path.read_text(encoding="utf-8"))
                    if self.index_path.exists() else {})
            keys = meta.get("keys", {})
            committed = (max(keys.values(), default=-1) + 1) * (self.dim or 0)
            blob = self.dir / meta.get("blob", "vectors.f32")
            appendable = (bool(meta) and meta.get("dim") == self.dim
                          and self.index_path.stat().st_mtime_ns == self._stamp
                          and meta.get("generation", 0) == self.generation
                          and blob.exists()
                          and blob.stat().st_size >= committed * DISK
                          and all(self.keys.get(k) == row for k, row in keys.items()))
            if appendable:
                with blob.open("r+b") as handle:
                    handle.seek(committed * DISK)
                    handle.truncate()       # discard an uncommitted tail after a crash
                    for part in _full(self._rows, committed):
                        handle.write(part)
            else:
                blob = self.dir / f"vectors-{os.getpid()}-{time.time_ns()}.f32"
                with blob.open("wb") as handle:
                    for part in _full(self._rows):
                        handle.write(part)
            staged = self.dir / f"index.{os.getpid()}.tmp"
            staged.write_text(json.dumps({"dim": self.dim, "model": self.model, "keys": self.keys,
                                          "blob": blob.name, "generation": self.generation}),
                              encoding="utf-8")
            for attempt in range(50):
                try:
                    os.replace(staged, self.index_path)
                    break
                except PermissionError:      # Windows: a reader has index.json open this instant
                    if attempt == 49:
                        raise
                    time.sleep(0.02)
            self._stamp = self.index_path.stat().st_mtime_ns
            self.blob_path = blob
            for old in self.dir.glob("vectors*.f32"):   # still under the writer lock
                if old != blob:
                    try:
                        old.unlink()
                    except OSError:                 # another process is reading it right now
                        pass
        self._dirty = 0


    def clear(self) -> None:
        """Discard every vector, here and for every process sharing the cache (a new
        generation): for a cache found holding wrong vectors. They are embedded again."""
        with self._lock:
            import fasteners
            with fasteners.InterProcessLock(str(self.dir / "save.lock")):
                meta, _, _ = self._read()
                self.generation = max(self.generation, meta.get("generation", 0)) + 1
                self.keys, self._rows, self._dirty = {}, array("H"), 1
                self._stamp = None
            self._flush()


def convert(json_path, directory=None, model: str = "") -> VectorCache:
    """One-time migration of a legacy {key: [floats]} JSON cache."""
    json_path = Path(json_path)
    cache = VectorCache(directory or json_path.with_suffix(".vec"), model=model)
    for key, vector in json.loads(json_path.read_text(encoding="utf-8")).items():
        cache.put(key, vector)
    cache.flush()
    return cache


def _self_check():
    import tempfile
    directory = Path(tempfile.mkdtemp()) / "vec"
    cache = VectorCache(directory, model="test")
    cache.put("a", [0.5, -0.25, 1.0])
    cache.put("b", [1.0, 0.0, 0.0])
    cache.put("a", [9, 9, 9])                      # duplicate key keeps the first vector
    assert len(cache) == 2 and "a" in cache and "zz" not in cache
    assert list(cache.get("a")) == [0.5, -0.25, 1.0]
    assert cache.get("zz") is None
    cache.flush()

    reopened = VectorCache(directory)
    assert len(reopened) == 2 and reopened.dim == 3 and reopened.model == "test"
    assert list(reopened.get("b")) == [1.0, 0.0, 0.0]
    reopened.put("c", [2.0, 2.0, 2.0])
    reopened.flush()
    assert list(VectorCache(directory).get("c")) == [2.0, 2.0, 2.0]

    # two processes, same cache, different new vectors, saving in turn: whichever index a reader
    # gets, every key it lists must come back with ITS vector, never the other writer's row
    first, second = VectorCache(directory), VectorCache(directory)
    first.put("only-first", [7.0, 7.0, 7.0])
    second.put("only-second", [8.0, 8.0, 8.0])
    second.put("also-second", [6.0, 6.0, 6.0])
    first.flush()
    second.flush()
    after = VectorCache(directory)
    assert list(after.get("only-first")) == [7.0, 7.0, 7.0], "the second save kept the first's"
    assert list(after.get("only-second")) == [8.0, 8.0, 8.0]
    assert list(after.get("also-second")) == [6.0, 6.0, 6.0]
    assert list(after.get("c")) == [2.0, 2.0, 2.0]
    assert len(list(directory.glob("vectors*.f32"))) == 1, "stale blobs are removed"

    # a reader that loaded before another process saved picks the new vectors up on refresh
    reader, writer = VectorCache(directory), VectorCache(directory)
    writer.put("late", [3.0, 1.0, 4.0])
    writer.flush()
    assert "late" not in reader and reader.refresh() == 1
    assert list(reader.get("late")) == [3.0, 1.0, 4.0] and reader.refresh() == 0
    assert list(reader.get("only-first")) == [7.0, 7.0, 7.0]

    # a cache found wrong is cleared for everyone: a process still holding the old vectors drops
    # them on refresh instead of saving them back
    holder, fixer = VectorCache(directory), VectorCache(directory)
    fixer.clear()
    assert len(VectorCache(directory)) == 0
    assert holder.refresh() == 0 and len(holder) == 0
    holder.put("new", [1.0, 1.0, 1.0])
    holder.flush()
    assert list(VectorCache(directory).keys) == ["new"]

    legacy = directory.parent / "legacy.json"
    legacy.write_text(json.dumps({"k1": [1.0, 2.0, 3.0], "k2": [4.0, 5.0, 6.0]}), encoding="utf-8")
    migrated = convert(legacy)
    assert len(migrated) == 2 and list(migrated.get("k2")) == [4.0, 5.0, 6.0]
    print("vector_cache self-check ok")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cache = convert(sys.argv[1])
        print(f"converted {len(cache)} vectors -> {cache.dir}")
    else:
        _self_check()
