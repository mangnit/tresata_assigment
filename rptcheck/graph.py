"""S2 + S3: entity resolution into a knowledge graph with provenance.

Two kinds of edges, kept apart on purpose:
* identity edges ("these two records are the same legal person"), each with a
  tier: 1 exact key, 2 scored match, 3 gray zone (model judged or abstained);
* relationship edges ("director of", "relative of", "partner in" ...), each from a
  named source (client master, signed declaration, public registry, prior year AR).

Records are never merged in place. A cluster is a connected component of identity
edges, and every path that a rule uses can be replayed edge by edge.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from .evidence import Ledger
from .extract import parse_declaration, parse_prior_year_note
from .ids import (bank_key, normalize_gstin, normalize_pan, pan_from_gstin, pan_holder_type,
                  parse_cin)
from .llm import redacted_card
from .names import address_score, company_key, looks_like_company, person_name_score

TIER_WEIGHT = {1: 1, 2: 5, 3: 10}


@dataclass
class Record:
    id: str
    kind: str                  # client | person | org | vendor
    name: str
    source: str
    pan: str | None = None
    din: str | None = None
    cin: str | None = None
    address: str = ""
    bank: str | None = None
    evidence: list[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)

    @property
    def holder_type(self) -> str:
        if self.pan:
            return pan_holder_type(self.pan)
        if self.cin:
            return "company"
        return "company" if looks_like_company(self.name) else "unknown"


@dataclass
class RelEdge:
    kind: str                  # holds_office | relative_of | interest_in | registry_director_of | group_entity | prior_year_rp
    src: str
    dst: str
    provenance: str            # client_master | declaration | registry | prior_year_ar
    evidence: list[str]
    attrs: dict = field(default_factory=dict)


class KnowledgeGraph:
    def __init__(self, data_dir: Path, llm=None):
        self.dir = Path(data_dir)
        self.llm = llm
        self.ev = Ledger()
        self.records: dict[str, Record] = {}
        self.rel: list[RelEdge] = []
        self.ident = nx.Graph()
        self.conflicts: list[dict] = []
        self.log: list[dict] = []
        self.declarations = []
        self.company = json.loads((self.dir / "company.json").read_text())
        self.ev_company = self.ev.add(self.dir / "company.json", "whole file", self.company["name"], "client_master")

    # ------------------------------------------------------------ building
    def add(self, r: Record) -> Record:
        self.records[r.id] = r
        self.ident.add_node(r.id)
        return r

    def build(self) -> "KnowledgeGraph":
        d = self.dir
        c = self.company
        self.add(Record("C", "client", c["name"], "company.json", pan=c["pan"], cin=c["cin"], evidence=[self.ev_company]))
        for g in c["group"]:
            e = self.ev.add(d / "company.json", f"group[{g['name']}]", json.dumps(g), "client_master")
            r = self.add(Record(f"G:{g['pan']}", "org", g["name"], "company.json", pan=g["pan"], cin=g["cin"], evidence=[e]))
            self.rel.append(RelEdge("group_entity", r.id, "C", "client_master", [e], dict(g)))

        with open(d / "register_directors_kmp.csv") as f:
            for i, row in enumerate(csv.DictReader(f), start=2):
                e = self.ev.add(d / "register_directors_kmp.csv", f"row {i}", f"{row['name']}, {row['role']}", "client_master")
                r = self.add(Record(f"R:{row['person_id']}", "person", row["name"], "register", pan=row["pan"] or None,
                                    din=row["din"] or None, address=row["address"],
                                    bank=bank_key(row["bank_account"], row["ifsc"]), evidence=[e]))
                self.rel.append(RelEdge("holds_office", r.id, "C", "client_master", [e], dict(
                    role=row["role"], is_director=row["is_director"] == "Y", is_kmp=row["is_kmp"] == "Y",
                    valid_from=row["appointed_on"], valid_to=row["ceased_on"] or None)))

        with open(d / "management_rp_list.csv") as f:
            for i, row in enumerate(csv.DictReader(f), start=2):
                e = self.ev.add(d / "management_rp_list.csv", f"row {i}", f"{row['name']}: {row['relationship']}", "client_master")
                self.add(Record(f"M:{i}", "org" if looks_like_company(row["name"]) or "&" in row["name"] else "person",
                                row["name"], "management_list", pan=normalize_pan(row["pan"]) if row["pan"] else None,
                                evidence=[e], attrs={"relationship": row["relationship"], "mgmt_list": True}))

        self._load_declarations()
        self._load_registry()

        for n, (name, cat) in enumerate(parse_prior_year_note(d / "prior_year_ar_note38.txt"), start=1):
            e = self.ev.add(d / "prior_year_ar_note38.txt", f"Note 38, {cat}", name, "prior_year_ar")
            r = self.add(Record(f"PY:{n}", "org" if looks_like_company(name) or "&" in name else "person", name,
                                "prior_year_ar", evidence=[e], attrs={"category": cat}))
            self.rel.append(RelEdge("prior_year_rp", r.id, "C", "prior_year_ar", [e], {"category": cat}))

        with open(d / "vendor_master.csv") as f:
            for i, row in enumerate(csv.DictReader(f), start=2):
                gst = normalize_gstin(row["gstin"]) if row["gstin"] else None
                pan = normalize_pan(row["pan"]) if row["pan"] else None
                derived = pan_from_gstin(gst) if gst else None
                e = self.ev.add(d / "vendor_master.csv", f"row {i}", f"{row['vendor_id']} {row['name']}", "client_master")
                attrs = {"gstin": gst, "pan_from_gstin": derived}
                if pan and derived and pan != derived:
                    self.conflicts.append({"record": row["vendor_id"], "issue": "PAN differs from PAN inside GSTIN"})
                self.add(Record(row["vendor_id"], "vendor", row["name"], "vendor_master", pan=pan or derived,
                                address=row["address"], bank=bank_key(row["bank_account"], row["ifsc"]),
                                evidence=[e], attrs=attrs))
        self._resolve()
        return self

    def _load_declarations(self):
        people = {r.din: r for r in self.records.values() if r.kind == "person" and r.din}
        by_name = {company_key(r.name): r for r in self.records.values() if r.source == "register"}
        for path in sorted((self.dir / "declarations").glob("*.txt")):
            dec = parse_declaration(path, llm=self.llm)
            self.declarations.append(dec)
            owner = people.get(dec.din) or by_name.get(company_key(dec.declarant))
            if owner is None:
                self.log.append({"stage": "S1", "warn": f"declarant not in register: {dec.file}"})
                continue
            for k, it in enumerate(dec.interests, start=1):
                e = self.ev.add(path, f"page {it.page}", it.raw_quoted_span, "declaration", extra={"span_check": it.span_check})
                r = self.add(Record(f"I:{path.stem}:{k}", "org", it.entity_name, "declaration", pan=it.pan, evidence=[e]))
                self.rel.append(RelEdge("interest_in", owner.id, r.id, "declaration", [e],
                                        {"nature": it.nature, "pct": it.shareholding_pct}))
            for k, rv in enumerate(dec.relatives, start=1):
                e = self.ev.add(path, f"page {rv.page}", rv.raw_quoted_span, "declaration", extra={"span_check": rv.span_check})
                r = self.add(Record(f"REL:{path.stem}:{k}", "person", rv.name, "declaration", pan=rv.pan, din=rv.din, evidence=[e]))
                self.rel.append(RelEdge("relative_of", r.id, owner.id, "declaration", [e],
                                        {"relation": rv.relation, "relation_text": rv.relation_text}))
            for rej in dec.rejected:
                self.log.append({"stage": "S1", "rejected": rej, "file": dec.file})

    def _load_registry(self):
        reg = json.loads((self.dir / "registry_snapshot.json").read_text())
        path = self.dir / "registry_snapshot.json"
        for cin, comp in reg["companies"].items():
            info = parse_cin(cin)
            e = self.ev.add(path, f"companies[{cin}]", comp["name"], "registry")
            rid = "C" if cin == self.company["cin"] else f"CIN:{cin}"
            if rid == "C":
                continue
            self.add(Record(rid, "org", comp["name"].title(), "registry", cin=cin, evidence=[e],
                            attrs={"company_class": info.company_class if info else "unknown"}))
        for din, dd in reg["directors"].items():
            e0 = self.ev.add(path, f"directors[{din}]", dd["name"], "registry", retrieved_on=dd["retrieved_on"], ttl_days=30)
            p = self.add(Record(f"DIN:{din}", "person", dd["name"].title(), "registry", din=din, evidence=[e0]))
            for comp in dd["companies"]:
                tgt = "C" if comp["cin"] == self.company["cin"] else f"CIN:{comp['cin']}"
                cname = reg["companies"].get(comp["cin"], {}).get("name", comp["cin"])
                e = self.ev.add(path, f"directors[{din}] -> {comp['cin']}", f"{dd['name']}: {comp['designation']} of {cname} from {comp['appointed']}"
                                + (f" to {comp['ceased']}" if comp["ceased"] else ""), "registry",
                                retrieved_on=dd["retrieved_on"], ttl_days=30)
                self.rel.append(RelEdge("registry_director_of", p.id, tgt, "registry", [e], {
                    "designation": comp["designation"], "valid_from": comp["appointed"], "valid_to": comp["ceased"] or None}))

    # ------------------------------------------------------------ resolution
    def _link(self, a: str, b: str, tier: int, method: str, evidence_text: str, score: float = 1.0, **extra):
        e = self.ev.add(None, f"{a} <-> {b}", evidence_text, "resolution")
        if self.ident.has_edge(a, b) and self.ident[a][b]["tier"] <= tier:
            return
        self.ident.add_edge(a, b, tier=tier, method=method, score=score, evidence=[e], **extra)

    def _resolve(self):
        recs = list(self.records.values())
        index: dict[tuple, list[Record]] = {}
        for r in recs:
            keys = []
            if r.pan:
                keys.append(("pan", r.pan))
            if r.din:
                keys.append(("din", r.din))
            if r.cin:
                keys.append(("cin", r.cin))
            if r.bank:
                keys.append(("bank", r.bank))
            if r.kind in ("org", "vendor", "client") and (r.cin or looks_like_company(r.name)):
                keys.append(("cname", company_key(r.name)))
            if r.kind == "person" or (r.kind == "org" and r.source in ("prior_year_ar", "management_list")):
                keys.append(("exactname", company_key(r.name)))
            for k in keys:
                index.setdefault(k, []).append(r)

        # Tier 1: shared exact keys (with a PAN veto)
        for (ktype, kval), group in index.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if a.pan and b.pan and a.pan != b.pan:
                        if ktype in ("bank", "cname"):
                            self.conflicts.append({"a": a.id, "b": b.id, "issue": f"same {ktype}, different PAN"})
                            self._link_conflict(a.id, b.id, ktype)
                        continue
                    if ktype == "exactname":
                        # exact full name is not a key for people: it is a strong Tier 2 signal only
                        self._link(a.id, b.id, 2, "exact_full_name", f"exact name '{a.name}'", 0.9)
                    else:
                        self._link(a.id, b.id, 1, ktype, f"{ktype} {kval}")

        # Tier 2 / 3: vendors still not tied to any known party, scored against people with addresses
        people = [r for r in recs if r.kind == "person" and r.address]
        for v in [r for r in recs if r.kind == "vendor"]:
            if any(self.records[n].kind != "vendor" for n in nx.node_connected_component(self.ident, v.id)):
                continue
            for p in people:
                if v.pan and p.pan and v.pan != p.pan:
                    continue                                   # PAN veto
                ns, note = person_name_score(v.name, p.name)
                ad = address_score(v.address, p.address)
                if ad < 0.5 or ns < 0.6:                       # a name alone is never evidence
                    continue
                combined = round(0.5 * ns + 0.5 * ad, 3)
                if combined >= 0.8 and note.startswith("0 "):
                    self._link(v.id, p.id, 2, "name+address", f"name {ns}, address {ad}", combined)
                elif combined >= 0.55:
                    self._tier3(v, p, ns, ad, combined)

    def _link_conflict(self, a: str, b: str, ktype: str):
        e = self.ev.add(None, f"{a} <-> {b}", f"same {ktype} but different PAN", "resolution")
        self.rel.append(RelEdge("shared_key_conflict", a, b, "resolution", [e], {"key": ktype}))

    def _tier3(self, v: Record, p: Record, ns: float, ad: float, combined: float):
        if self.llm is None:
            decision = {"decision": "cannot_tell", "reason": "no model configured: abstained, routed to human"}
        else:
            a = redacted_card(v.name, v.address, v.holder_type) | {"address_similarity": ad}
            b = redacted_card(p.name, p.address, "individual") | {"address_similarity": ad}
            try:
                decision = self.llm.judge_match(a, b)
            except Exception as exc:  # model failure = abstain, never a silent match
                decision = {"decision": "cannot_tell", "reason": f"model error: {exc}"}
        self.log.append({"stage": "S2-tier3", "vendor": v.id, "person": p.id, "scores": [ns, ad, combined], **decision})
        if decision["decision"] in ("match", "cannot_tell"):
            self._link(v.id, p.id, 3, f"gray_zone:{decision['decision']}", decision.get("reason", ""), combined,
                       model_decision=decision["decision"])

    # ------------------------------------------------------------ queries
    def cluster(self, rid: str) -> set[str]:
        return nx.node_connected_component(self.ident, rid)

    def identity_path(self, a: str, b: str) -> list[dict]:
        if a == b:
            return []
        nodes = nx.shortest_path(self.ident, a, b, weight=lambda u, v, d: TIER_WEIGHT[d["tier"]])
        return [dict(kind="same_entity", src=u, dst=v, **self.ident[u][v]) for u, v in zip(nodes, nodes[1:])]

    def rel_from(self, rid_set: set[str], kind: str) -> list[RelEdge]:
        return [e for e in self.rel if e.kind == kind and e.src in rid_set]

    def rel_to(self, rid_set: set[str], kind: str) -> list[RelEdge]:
        return [e for e in self.rel if e.kind == kind and e.dst in rid_set]

    def display_name(self, cluster: set[str]) -> str:
        """Prefer the registered name, then the client's group master, then a vendor name."""
        for src in ("registry", "register", "company.json", "vendor_master"):
            for r in sorted(cluster):
                if self.records[r].source == src:
                    return self.records[r].name
        return self.records[sorted(cluster)[0]].name

    def in_management_list(self, cluster: set[str]) -> bool:
        return any(self.records[r].attrs.get("mgmt_list") for r in cluster)
