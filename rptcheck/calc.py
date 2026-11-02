"""S5: deterministic calculators. Integer paise only, no floats, unit tested.

Three separate checks, because the law has three separate gates:
 1. Disclosure        Ind AS 24 / LODR Reg 23(9)        every RPT, no threshold
 2. Audit committee   LODR Reg 23(2), Companies Act s.177(4)(iv)
                      every RPT needs prior AC approval (omnibus allowed, with a limit)
 3. Shareholders      LODR Reg 23(4): MATERIAL RPTs (Schedule XII from 19 Dec 2025)
                      Companies Act s.188 + Rule 15(3): only if NOT in ordinary course
                      or NOT at arm's length, which is a human judgement, so we flag it.
Exemption coded: LODR Reg 23(5)(b), holding company and wholly owned subsidiary
whose accounts are consolidated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

CR = 10**9          # one crore rupees, in paise
LAKH = 10**7

SCHEDULE_XII_FROM = date(2025, 12, 19)   # 30th day from gazette publication on 19 Nov 2025


@dataclass(frozen=True)
class Threshold:
    value_paise: int
    rule_version: str
    formula: str


def lodr_materiality(consolidated_turnover: int, on: date) -> Threshold:
    t = consolidated_turnover
    if on < SCHEDULE_XII_FROM:
        v = min(1000 * CR, t // 10)
        return Threshold(v, "LODR Reg 23(1) pre amendment", "lower of Rs 1,000 crore or 10% of consolidated turnover")
    if t <= 20000 * CR:
        return Threshold(t // 10, "LODR Schedule XII (2025) band 1", "10% of consolidated turnover")
    if t <= 40000 * CR:
        return Threshold(2000 * CR + (t - 20000 * CR) * 5 // 100, "LODR Schedule XII (2025) band 2",
                         "Rs 2,000 crore + 5% of turnover above Rs 20,000 crore")
    v = min(3000 * CR + (t - 40000 * CR) * 25 // 1000, 5000 * CR)
    return Threshold(v, "LODR Schedule XII (2025) band 3",
                     "Rs 3,000 crore + 2.5% of turnover above Rs 40,000 crore, capped at Rs 5,000 crore")


def rule15_threshold(nature: str, turnover: int, net_worth: int) -> Threshold | None:
    """Companies (Meetings of Board and its Powers) Rules 2014, Rule 15(3)."""
    if nature in ("purchase_goods", "sale_goods", "services", "freight", "rent"):
        return Threshold(turnover // 10, "Rule 15(3)(a)", "10% of turnover (goods, services, leasing)")
    if nature in ("property_sale", "property_purchase"):
        return Threshold(net_worth // 10, "Rule 15(3)(a)(ii)", "10% of net worth")
    return None


def subsidiary_rpt_needs_parent_ac(value: int, subsidiary_turnover: int, parent_threshold: int) -> bool:
    """LODR Reg 23(2) proviso as amended in 2025: an RPT by a subsidiary (listed parent not a party)
    needs the parent's audit committee approval if it exceeds Rs 1 crore AND exceeds the lower of
    10% of the subsidiary's standalone turnover or the parent's Schedule XII threshold.
    (Not exercised by the synthetic ledger, which holds only the parent's own transactions.)"""
    return value > 1 * CR and value > min(subsidiary_turnover // 10, parent_threshold)


def fmt(paise: int) -> str:
    rupees, p = divmod(paise, 100)
    if rupees >= 10**7:
        s = f"Rs {rupees / 10**7:,.2f} crore"
    elif rupees >= 10**5:
        s = f"Rs {rupees / 10**5:,.2f} lakh"
    else:
        s = f"Rs {rupees:,}"
    return s + (f" ({paise} paise)" if p else "")


@dataclass
class CalcFinding:
    code: str
    citation: str
    detail: str
    on_date: str
    amount_paise: int
    formula: str
    needs_human: bool = False


def approval_checks(txns: list[dict], approvals: list[dict], company: dict, exempt_wos: bool) -> tuple[list[CalcFinding], dict]:
    """txns: [{date, amount_paise, nature, txn_id}] for ONE related party, already merged across vendor records."""
    txns = sorted(txns, key=lambda t: (t["date"], t["txn_id"]))
    total = sum(int(t["amount_paise"]) for t in txns)
    summary = {"total_paise": total, "count": len(txns)}
    out: list[CalcFinding] = []
    if not txns:
        return out, summary
    if exempt_wos:
        summary["exemption"] = "LODR Reg 23(5)(b): holding company and wholly owned subsidiary with consolidated accounts"
        return out, summary
    ac = [a for a in approvals if a["approval_type"].startswith("audit_committee")]
    sh = [a for a in approvals if a["approval_type"] == "shareholder"]
    turnover = company["prev_fy_consolidated_turnover_paise"]
    standalone = company["prev_fy_standalone_turnover_paise"]
    net_worth = company["prev_fy_net_worth_paise"]

    if not ac:
        out.append(CalcFinding("AC_APPROVAL_MISSING", "LODR Reg 23(2); Companies Act s.177(4)(iv)",
                               f"{len(txns)} transactions, {fmt(total)}, with no audit committee approval on record",
                               txns[0]["date"], total, "sum of transactions in the year"))
    running, flagged = 0, set()
    by_nature: dict[str, int] = {}
    for t in txns:
        amt = int(t["amount_paise"])
        running += amt
        by_nature[t["nature"]] = by_nature.get(t["nature"], 0) + amt
        d = date.fromisoformat(t["date"])
        limit = sum(int(a["limit_paise"]) for a in ac if a["valid_from"] <= t["date"] <= a["valid_to"])
        if ac and running > limit and "limit" not in flagged:
            flagged.add("limit")
            out.append(CalcFinding("AC_LIMIT_EXCEEDED", "LODR Reg 23(3) omnibus approval limit",
                                   f"cumulative {fmt(running)} passed the approved limit {fmt(limit)} at {t['txn_id']}",
                                   t["date"], running, "running total vs approved limit"))
        th = lodr_materiality(turnover, d)
        if running > th.value_paise and not sh and "material" not in flagged:
            flagged.add("material")
            out.append(CalcFinding("MATERIAL_RPT_NO_SHAREHOLDER_APPROVAL", f"LODR Reg 23(4); {th.rule_version}",
                                   f"cumulative {fmt(running)} exceeded materiality {fmt(th.value_paise)} at {t['txn_id']}",
                                   t["date"], running, th.formula))
        r15 = rule15_threshold(t["nature"], standalone, net_worth)
        if r15 and by_nature[t["nature"]] >= r15.value_paise and "r15" not in flagged:
            flagged.add("r15")
            out.append(CalcFinding("SEC188_RULE15_THRESHOLD", "Companies Act s.188(1) third proviso; Rule 15(3)",
                                   f"{t['nature']} reached {fmt(by_nature[t['nature']])} vs {fmt(r15.value_paise)}. "
                                   "Shareholder approval needed only if NOT in ordinary course or NOT at arm's length: needs auditor judgement",
                                   t["date"], by_nature[t["nature"]], r15.formula, needs_human=True))
    return out, summary
