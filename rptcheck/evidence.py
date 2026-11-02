"""Evidence ledger. Every fact the system uses gets an ID, a source, a locator,
a document hash and, for registry facts, a time to live (TTL).

The TTL and checksum ideas follow the 'Data Facts' proposal (Mehta, Raskar et al.,
arXiv 2606.26211): stale facts are flagged, not silently used.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class Evidence:
    id: str
    source: str            # file name
    locator: str           # page/line, row, or field
    content: str           # quoted span or value
    sha256: str
    kind: str              # client_master | declaration | registry | prior_year_ar | ledger | minutes | resolution | human
    retrieved_on: str = ""  # ISO date, for registry facts
    ttl_days: int | None = None
    extra: dict = field(default_factory=dict)

    def is_stale(self, as_of: date) -> bool:
        if not self.ttl_days or not self.retrieved_on:
            return False
        return (as_of - date.fromisoformat(self.retrieved_on)).days > self.ttl_days


class Ledger:
    def __init__(self) -> None:
        self.items: dict[str, Evidence] = {}
        self._hash_cache: dict[str, str] = {}

    def file_hash(self, path: Path) -> str:
        key = str(path)
        if key not in self._hash_cache:
            self._hash_cache[key] = sha256_file(path)[:16]
        return self._hash_cache[key]

    def add(self, source_path: Path | None, locator: str, content: str, kind: str, **kw) -> str:
        """source_path None = a fact computed by the pipeline (e.g. an identity link), not read from a file."""
        eid = f"E{len(self.items) + 1:03d}"
        name = Path(source_path).name if source_path else "computed by resolver"
        digest = self.file_hash(source_path) if source_path else "-"
        self.items[eid] = Evidence(eid, name, locator, content, digest, kind, **kw)
        return eid

    def get(self, eid: str) -> Evidence:
        return self.items[eid]

    def as_dicts(self) -> list[dict]:
        return [asdict(e) for e in self.items.values()]
