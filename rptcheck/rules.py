"""S4: legal definitions as code. Each rule is a path query over the graph,
carries its clause citation, effective dates and a list type:

* closed     : the law gives an exhaustive list (e.g. s.2(77) relatives via Rule 4).
               A hit is CONFIRMED if every identity edge on its path is Tier 1.
* judgement  : the standard says "may be expected to influence ... including"
               (Ind AS 24 para 9). A hit is never more than a CANDIDATE.
* exception  : an audit exception, not a legal conclusion (prior year mismatch).
* red_flag   : pattern needing human judgement (LODR 2(1)(zc) 'purpose and effect').

The model never decides relatedness. It can only contribute Tier 3 identity edges,
which by construction cap a hit at CANDIDATE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .extract import RULE4
from .graph import KnowledgeGraph, RelEdge


@dataclass(frozen=True)
class RuleDef:
    id: str
    citation: str
    text: str
    list_type: str
    effective_from: str
    effective_to: str | None = None
    version: str = "1"


RULES = {r.id: r for r in [
    RuleDef("CA13_2_76_i", "Companies Act 2013 s.2(76)(i)", "a director or his relative", "closed", "2014-04-01"),
    RuleDef("CA13_2_76_ii", "Companies Act 2013 s.2(76)(ii)", "a key managerial personnel or his relative", "closed", "2014-04-01"),
    RuleDef("CA13_2_76_iii", "Companies Act 2013 s.2(76)(iii)", "a firm in which a director, manager or his relative is a partner", "closed", "2014-04-01"),
    RuleDef("CA13_2_76_iv", "Companies Act 2013 s.2(76)(iv)", "a private company in which a director or manager or his relative is a member or director", "closed", "2014-04-01"),
    RuleDef("CA13_2_76_v", "Companies Act 2013 s.2(76)(v)", "a public company in which a director or manager is a director and holds, along with his relatives, more than two per cent of its paid up share capital", "closed", "2014-04-01"),
    RuleDef("CA13_2_76_viii", "Companies Act 2013 s.2(76)(viii)", "a holding, subsidiary or associate company", "closed", "2014-04-01"),
    RuleDef("INDAS24_P9_CLOSE_FAMILY", "Ind AS 24 para 9", "close member of the family of KMP: those who may be expected to influence, or be influenced by, that person (list is inclusive, not exhaustive)", "judgement", "2016-04-01"),
    RuleDef("SA550_PRIOR_YEAR", "SA 550 (with prior year Ind AS 24 note)", "party disclosed as related in the prior year is missing from management's current list", "exception", "2016-04-01"),
    RuleDef("LODR_2_1_ZC_CONDUIT", "SEBI LODR Reg 2(1)(zc)", "transaction with an unrelated party whose purpose and effect may be to benefit a related party", "red_flag", "2022-04-01"),
]}


@dataclass
class Hit:
    vendor: str
    rule: str
    status: str                      # confirmed | candidate | exception | red_flag
    path: list[dict] = field(default_factory=list)
    anchor: str = ""
    note: str = ""

    @property
    def evidence(self) -> list[str]:
        out = []
        for step in self.path:
            for e in step.get("evidence", []):
                if e not in out:
                    out.append(e)
        return out


def _overlaps(valid_from: str | None, valid_to: str | None, fy0: date, fy1: date) -> bool:
    vf = date.fromisoformat(valid_from) if valid_from else date.min
    vt = date.fromisoformat(valid_to) if valid_to else date.max
    return vf <= fy1 and vt >= fy0


def _step(e: RelEdge) -> dict:
    return dict(kind=e.kind, src=e.src, dst=e.dst, provenance=e.provenance, evidence=list(e.evidence), **e.attrs)


class RuleEngine:
    def __init__(self, kg: KnowledgeGraph):
        self.kg = kg
        c = kg.company
        self.fy0, self.fy1 = date.fromisoformat(c["fy_start"]), date.fromisoformat(c["fy_end"])
        self.outside_period: list[dict] = []

    # -------- standing of a person cluster: in office? relative of someone in office?
    def office(self, cluster: set[str]) -> list[tuple[RelEdge, dict]]:
        out = []
        for e in self.kg.rel_from(cluster, "holds_office"):
            if _overlaps(e.attrs["valid_from"], e.attrs["valid_to"], self.fy0, self.fy1):
                out.append((e, e.attrs))
            else:
                self.outside_period.append({"edge": _step(e), "why": "office not held during the year"})
        return out

    def standings(self, person_rec: str) -> list[dict]:
        """Ways a person is connected to the client: directly in office, or as relative of someone in office."""
        cl = self.kg.cluster(person_rec)
        res = []
        for e, a in self.office(cl):
            res.append(dict(type="self", is_director=a["is_director"], is_kmp=a["is_kmp"],
                            path=self.kg.identity_path(person_rec, e.src) + [_step(e)], relation="self"))
        for rel in self.kg.rel_from(cl, "relative_of"):
            owner_cl = self.kg.cluster(rel.dst)
            for e, a in self.office(owner_cl):
                res.append(dict(type="relative", is_director=a["is_director"], is_kmp=a["is_kmp"],
                                relation=rel.attrs["relation"], relation_text=rel.attrs["relation_text"],
                                path=self.kg.identity_path(person_rec, rel.src) + [_step(rel)]
                                + self.kg.identity_path(rel.dst, e.src) + [_step(e)]))
        return res

    # -------- evaluation for one vendor record
    def evaluate(self, vid: str) -> list[Hit]:
        kg, hits = self.kg, []
        cl = kg.cluster(vid)

        def finish(rule: str, path: list[dict], anchor: str, note: str = "", force: str | None = None):
            worst = max([s.get("tier", 1) for s in path if s["kind"] == "same_entity"], default=1)
            rdef = RULES[rule]
            status = force or ("candidate" if rdef.list_type == "judgement" or worst > 1 else "confirmed")
            hits.append(Hit(vid, rule, status, path, anchor, note + (f" (weakest identity link: Tier {worst})" if worst > 1 else "")))

        # (viii) group companies
        for e in kg.rel_from(cl, "group_entity"):
            finish("CA13_2_76_viii", kg.identity_path(vid, e.src) + [_step(e)], e.src, f"{e.attrs['relationship']}, {e.attrs['holding_pct']}%")

        # (i)/(ii)/Ind AS 24: the vendor IS a person (proprietor or payee)
        for rid in cl:
            r = kg.records[rid]
            if r.kind != "person":
                continue
            for s in self.standings(rid):
                path = kg.identity_path(vid, rid) + s["path"]
                base = "CA13_2_76_i" if s["is_director"] else "CA13_2_76_ii"
                if s["type"] == "self" or s["relation"] in RULE4:
                    finish(base, path, rid, f"{s['relation']}")
                else:
                    finish("INDAS24_P9_CLOSE_FAMILY", path, rid, f"relation '{s['relation_text']}' is not in Rule 4")

        # (iii)/(iv)/(v): the vendor is a firm or company in which someone connected has an interest
        org_types = {kg.records[r].holder_type for r in cl}
        classes = {kg.records[r].attrs.get("company_class") for r in cl} - {None}
        is_firm = "firm" in org_types
        is_private = "private" in classes or any(kg.records[r].name.lower().replace(".", "").find("pvt") >= 0
                                                 or "private limited" in kg.records[r].name.lower() for r in cl)
        is_public = "public" in classes and not is_private
        links = [(e, "interest") for e in kg.rel_to(cl, "interest_in")] + \
                [(e, "registry") for e in kg.rel_to(cl, "registry_director_of")]
        for e, src_kind in links:
            if src_kind == "registry" and not _overlaps(e.attrs.get("valid_from"), e.attrs.get("valid_to"), self.fy0, self.fy1):
                self.outside_period.append({"edge": _step(e), "why": "directorship ended before the year"})
                continue
            nature = (e.attrs.get("nature") or e.attrs.get("designation") or "").lower()
            for s in self.standings(e.src):
                if s["type"] == "relative" and s["relation"] not in RULE4:
                    continue  # a non Rule 4 relative's company is out of s.2(76); flagged only via Ind AS 24 on persons
                path = kg.identity_path(vid, e.dst) + [_step(e)] + s["path"]
                who = "director" if s["is_director"] else "KMP"
                rel = "" if s["type"] == "self" else f" ({s['relation']} of a {who})"
                if is_firm and "partner" in nature:
                    finish("CA13_2_76_iii", path, e.dst, f"partner{rel}")
                elif is_private and ("director" in nature or "member" in nature):
                    finish("CA13_2_76_iv", path, e.dst, f"{nature}{rel}")
                elif is_public and s["type"] == "self" and s["is_director"] and "director" in nature:
                    pct = self._holding(cl, e.src)
                    if pct is None:
                        finish("CA13_2_76_v", path, e.dst, "director of a public company; holding not known: verify", force="candidate")
                    elif pct > 2.0:
                        finish("CA13_2_76_v", path, e.dst, f"director holding {pct}% with relatives")
                    else:
                        self.outside_period.append({"vendor": vid, "why": f"public company holding {pct}% is not above 2%"})

        # prior year disclosure missing from the current list
        if not kg.in_management_list(cl):
            for e in kg.rel_from(cl, "prior_year_rp"):
                finish("SA550_PRIOR_YEAR", kg.identity_path(vid, e.src) + [_step(e)], e.src, e.attrs["category"], force="exception")
        return _dedupe(hits)

    def _holding(self, org_cluster: set[str], person_rec: str) -> float | None:
        pcl = self.kg.cluster(person_rec)
        vals = [e.attrs.get("pct") for e in self.kg.rel_to(org_cluster, "interest_in")
                if e.src in pcl and e.attrs.get("pct") is not None]
        return max(vals) if vals else None

    def conduit_pass(self, hits_by_vendor: dict[str, list[Hit]]) -> list[Hit]:
        """Shared bank (or name) key with a related party but a different PAN: red flag, never auto resolved."""
        related_vendors = {v for v, hs in hits_by_vendor.items() if any(h.status == "confirmed" for h in hs)}
        out = []
        for e in self.kg.rel:
            if e.kind != "shared_key_conflict":
                continue
            for a, b in ((e.src, e.dst), (e.dst, e.src)):
                if a in self.kg.records and self.kg.records[a].kind == "vendor" and b in related_vendors and a not in related_vendors:
                    out.append(Hit(a, "LODR_2_1_ZC_CONDUIT", "red_flag", [_step(e)], b,
                                   f"shares {e.attrs['key']} with {b} (a related party) but has a different PAN"))
        return out


def _dedupe(hits: list[Hit]) -> list[Hit]:
    best: dict[tuple, Hit] = {}
    rank = {"confirmed": 0, "exception": 1, "candidate": 2, "red_flag": 3}
    for h in hits:
        k = (h.vendor, h.rule)
        if k not in best or (rank[h.status], len(h.path)) < (rank[best[k].status], len(best[k].path)):
            best[k] = h
    return list(best.values())
