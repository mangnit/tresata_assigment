"""Indian identifiers: validation, parsing and OCR repair.

Everything here is deterministic. No model touches these functions.

Practitioner facts encoded here:
* A GSTIN is 2 digit state code + the holder's 10 character PAN + entity number
  + 'Z' + a mod 36 check character. So characters 3 to 12 ARE the PAN, and GST
  registrations in different states collapse to one legal person.
* The 4th character of a PAN says what kind of holder it is (P individual,
  C company, F firm or LLP, H HUF ...). A proprietorship has no PAN of its own,
  so its GSTIN embeds the proprietor's personal 'P' PAN.
* A CIN says listed (L) or unlisted (U) and the company class (PTC private,
  PLC public ...). Sec 2(76)(iv) applies to private companies and 2(76)(v) to
  public ones, so the CIN alone routes a company to the right clause.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
DIN_RE = re.compile(r"^[0-9]{8}$")
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")

PAN_HOLDER_TYPE = {
    "P": "individual", "C": "company", "F": "firm", "H": "huf", "A": "aop",
    "T": "trust", "B": "boi", "L": "local_authority", "J": "ajp", "G": "government",
}

CIN_CLASS = {
    "PTC": "private", "OPC": "private", "FTC": "private",   # FTC: private subsidiary of a foreign company
    "PLC": "public", "FLC": "public", "GOI": "public", "SGC": "public",
    "NPL": "section8", "ULL": "unlimited", "ULT": "unlimited",
}

# OCR confusions, applied only where the format says a digit or a letter must be.
_TO_DIGIT = {"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "i": "1",
             "|": "1", "Z": "2", "z": "2", "S": "5", "s": "5", "B": "8", "G": "6", "b": "6"}
_TO_LETTER = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}


def _clean(raw: str) -> str:
    return re.sub(r"[\s.\-/]", "", raw or "")


def _repair(raw: str, pattern: str) -> str:
    """pattern: 'D' digit, 'L' letter, 'X' alphanumeric, or a literal char."""
    s = _clean(raw)
    if len(s) != len(pattern):
        return s.upper()
    out = []
    for ch, want in zip(s, pattern):
        if want == "D":
            out.append(_TO_DIGIT.get(ch, ch))
        elif want == "L":
            out.append(_TO_LETTER.get(ch, ch).upper())
        elif want == "X":
            out.append(ch.upper())
        else:
            out.append(want if (ch.upper() == want or _TO_LETTER.get(ch) == want) else ch.upper())
    return "".join(out).upper()


# ---------------- PAN ----------------
def normalize_pan(raw: str) -> str | None:
    s = _repair(raw, "LLLLLDDDDL")
    return s if PAN_RE.match(s) else None


def pan_holder_type(pan: str) -> str:
    return PAN_HOLDER_TYPE.get(pan[3], "other") if pan and len(pan) == 10 else "unknown"


# ---------------- GSTIN ----------------
def gstin_check_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = ALNUM.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return ALNUM[(36 - total % 36) % 36]


def is_valid_gstin(g: str) -> bool:
    return bool(g) and bool(GSTIN_RE.match(g)) and gstin_check_char(g[:14]) == g[14]


def make_gstin(state_code: str, pan: str, entity_no: str = "1") -> str:
    body = f"{state_code}{pan}{entity_no}Z"
    return body + gstin_check_char(body)


def normalize_gstin(raw: str) -> str | None:
    s = _repair(raw, "DDLLLLLDDDDLXZX")
    return s if is_valid_gstin(s) else None   # never guess past a failed checksum


def pan_from_gstin(gstin: str) -> str | None:
    return gstin[2:12] if gstin and is_valid_gstin(gstin) else None


# ---------------- DIN / CIN ----------------
def normalize_din(raw: str) -> str | None:
    s = _repair(raw, "DDDDDDDD")
    return s if DIN_RE.match(s) else None


def normalize_cin(raw: str) -> str | None:
    s = _repair(raw, "LDDDDDLLDDDDLLLDDDDDD")
    return s if CIN_RE.match(s) else None


@dataclass(frozen=True)
class CinInfo:
    listed: bool
    state: str
    year: int
    company_class: str


def parse_cin(cin: str) -> CinInfo | None:
    if not cin or not CIN_RE.match(cin):
        return None
    return CinInfo(listed=cin[0] == "L", state=cin[6:8], year=int(cin[8:12]),
                   company_class=CIN_CLASS.get(cin[12:15], "other"))


# ---------------- bank ----------------
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def bank_key(account: str, ifsc: str) -> str | None:
    acc = _clean(account)
    ifsc = _clean(ifsc).upper()
    if not acc or not IFSC_RE.match(ifsc):
        return None
    return f"{acc}@{ifsc}"
