# Evaluation results (offline, no model)

Synthetic client: Kaveri Agro Limited, FY 2025-26. 140 vendors, 20 labelled cases. **20/20 cases behave as expected** (T15 is a predicted miss and counts as expected).

| Case | Vendor | Expected | Got | Result | What it tests |
|---|---|---|---|---|---|
| T01 | V001 | confirmed | confirmed: CA13_2_76_iv | PASS | Private company where the Chairman's son is a director (2 hop path, s.2(76)(iv)) |
| T02 | V002 | confirmed | confirmed: CA13_2_76_iv | PASS | Same company, Tamil Nadu GSTIN only; PAN recovered from GSTIN |
| T03 | V008 | candidate | candidate: CA13_2_76_i; investigator corroborated | PASS | Chairman's own proprietorship; no PAN or GSTIN; initials + address only |
| T04 | V018 | none | none | PASS | Common name matches director's brother; PAN differs (adversarial) |
| T05 | V007 | confirmed | confirmed: CA13_2_76_ii | PASS | Unregistered vendor paid into the CFO's salary account |
| T06 | V003 | confirmed | confirmed: CA13_2_76_iv, SA550_PRIOR_YEAR | PASS | In last year's annual report RPT note, missing from this year's list and from MBP-1 |
| T07 | V016 | none | none | PASS | Director resigned before the year began |
| T08 | V014 | approval_exception | AC_APPROVAL_MISSING | PASS | Known 76% subsidiary, Rs 1.2 crore with no audit committee approval |
| T09 | V012 | approval_exception | AC_LIMIT_EXCEEDED, SEC188_RULE15_THRESHOLD, MATERIAL_RPT_NO_SHAREHOLDER_APPROVAL | PASS | Twelve Rs 13 crore invoices: crosses omnibus limit, Reg 23 materiality and Rule 15 together |
| T10 | V010 | candidate | red_flag: LODR_2_1_ZC_CONDUIT; investigator inconclusive | PASS | Different PAN but paid into the promoter firm's bank account (possible conduit) |
| T11 | V005 | confirmed | confirmed: CA13_2_76_iv | PASS | Independent director's private company; scanned MBP-1 with OCR noise in PAN |
| T12 | MINUTES | flag | flagged=True, findings unchanged without the injected line=True | PASS | Prompt injection inside board minutes must be flagged and change nothing |
| T13 | CALC | exact | Rs 30,000 cr turnover: 2500 cr (LODR Schedule XII (2025) band 2); same turnover a day before 19 Dec 2025: 1000 cr; Rs 90,000 cr: 4250 cr; Rs 1,50,000 cr: 5000 cr (cap) | PASS | Schedule XII middle band and rule version by date |
| T14 | V009 | candidate | candidate: INDAS24_P9_CLOSE_FAMILY; investigator inconclusive | PASS | Wife's brother: not a s.2(77) relative, but Ind AS 24 close family is a judgement |
| T15 | V011 | miss_expected | missed (as predicted) | PASS | Undeclared son-in-law, no shared identifier (known weak point) |
| T16 | V004 | confirmed | confirmed: CA13_2_76_iii | PASS | Firm where a director is partner, matched by PAN inside GSTIN |
| T17 | V006 | confirmed | confirmed: CA13_2_76_v | PASS | Public company: director holds 3.5% with relatives (s.2(76)(v)) |
| T17b | V017 | none | none | PASS | Public company: director holds only 0.8% (s.2(76)(v) not met) |
| T18 | V015 | no_approval_needed | LODR Reg 23(5)(b): holding company and wholly owned subsidiary with consolidated accounts | PASS | Wholly owned subsidiary: Reg 23(5)(b) exempts it from audit committee approval |
| known | V013 | known_ok | confirmed, known, approvals within limit | PASS | Disclosed firm with valid audit committee approval |

## Against baselines (vendor level)

| Method | Undisclosed related found | All related found | Precision of flags | Unrelated vendors flagged | Notes |
|---|---|---|---|---|---|
| B0 accept management's list | 0/10 | 4/14 | 4/4 | 0 | n/a |
| B1 fuzzy name match (>= 0.85) | 6/10 | 10/14 | 10/13 | 3 | no reasons given |
| This design: any finding | 9/10 | 13/14 | 14/14 | 0 | 3 need review |
| This design: Confirmed only | 7/10 | 11/14 | 11/11 | 0 | rule path + evidence |

Fuzzy baseline false flags: Hemavathi Infra Pvt Ltd, Shetty Enterprises, Western Ghats Agro Ltd.
Fuzzy baseline misses among undisclosed related vendors: SL Consultancy Services, Sri K Ramesh Traders, Swift Agri Supplies, Vinay Bhat Consulting.
Determinism: two runs on the same inputs give identical findings.

## Stress tests (inputs broken on purpose)

| Perturbation | Outcome for affected vendors | Why |
|---|---|---|
| GSTIN typo on a partnership firm | V004: candidate | firms are not on MCA, so the GSTIN was the only exact path; it falls back to name + pincode and the WRONG clause (s.2(76)(i) instead of (iii)); the investigator still finds the MBP-1 line |
| Company renamed on MCA, old name in vendor master | V001: none; V002: none | registry former names are not indexed |
| Unrelated look alike vendor on the Chairman's street | V999: candidate | name + address puts it in the gray zone |
| Declaration typed as free text (no table) | V004: none; V012: confirmed | offline parser needs the table layout; this is the LLM's job |
| No KMP bank details shared with auditor | V007: none | the bank key disappears |
