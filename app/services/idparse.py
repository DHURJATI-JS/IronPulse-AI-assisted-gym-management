"""Deterministic ID-card text parsing and validators (no AI, no database).

* ``normalize_text``      NFKC, Cyrillic/Greek look-alikes -> Latin (OCR engines love "МЕНТА"), tidy whitespace
* ``verhoeff_valid``      Aadhaar checksum
* ``mrz_check_digit``     ICAO 9303 (weights 7-3-1); ``find_mrz`` parses TD3 (passports, 2 x 44) and TD1 (ID cards,
                          3 x 30) including common OCR confusions (O/0, I/1, S/5, B/8, chevrons read as K or «)
* ``extract_fields``      per-engine extraction: ID-number candidates (Aadhaar, PAN, EPIC, Indian DL, passport, MRZ,
                          generic), name, date of birth / year of birth, expiry
* ``build_consensus``     cross-engine agreement on the ID number (Levenshtein <= 1 tolerated, checksums enforced)
* ``name_similarity``     token-set ratio (difflib) - order-insensitive, tolerant to middle names and OCR noise
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from itertools import product

# ============================================================================ text normalisation
_CONFUSABLES = {
    # Cyrillic capitals
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y",
    "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S", "Ү": "Y", "Ԛ": "Q", "Ԝ": "W",
    # Cyrillic small letters that look Latin
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "һ": "h",
    "ԁ": "d", "ӏ": "l", "ԛ": "q", "ԝ": "w",
    # Greek
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "o", "ν": "v",
    # punctuation OCR engines emit
    "‹": "<", "〈": "<", "＜": "<", "‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"', "–": "-", "—": "-", "‐": "-",
    "‑": "-", "−": "-", "·": " ", "•": " ", "|": " ",
}
_CONFUSABLE_TABLE = str.maketrans(_CONFUSABLES)
_ZERO_WIDTH_RE = re.compile(r"[​-‏⁠﻿]")
_WS_RE = re.compile(r"[ \t  - 　]+")


def normalize_text(text: str | None) -> str:
    """NFKC + look-alike folding + whitespace tidy. Keeps line breaks (layout matters for labels and MRZ)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).replace("«", "<<").replace("»", "<<")
    t = _ZERO_WIDTH_RE.sub("", t).translate(_CONFUSABLE_TABLE)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in t.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def alnum_count(text: str | None) -> int:
    return sum(ch.isalnum() for ch in (text or ""))


# ============================================================================ OCR confusion maps
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "J": "1", "Z": "2", "S": "5",
                           "G": "6", "T": "7", "B": "8", "o": "0", "l": "1", "i": "1"})
_TO_ALPHA = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B", "4": "A", "7": "T"})


def to_digits(s: str) -> str:
    return s.translate(_TO_DIGIT)


def to_alpha(s: str) -> str:
    return s.translate(_TO_ALPHA)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def numbers_close(a: str, b: str) -> bool:
    """Same ID number after normalisation, tolerating one OCR error (substitution, insertion or deletion)."""
    if a == b:
        return True
    return abs(len(a) - len(b)) <= 1 and min(len(a), len(b)) >= 5 and levenshtein(a, b) <= 1


def normalize_number(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


# ============================================================================ Verhoeff (Aadhaar)
_VD = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VP = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_VINV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_valid(number: str) -> bool:
    n = re.sub(r"[\s-]", "", number or "")
    if not n.isdigit() or not n:
        return False
    c = 0
    for i, ch in enumerate(reversed(n)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(number: str) -> str:
    """Digit to append to ``number`` so that it passes ``verhoeff_valid`` (used by tests and the demo seed)."""
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VD[c][_VP[(i + 1) % 8][int(ch)]]
    return str(_VINV[c])


def aadhaar_valid(number: str) -> bool:
    n = re.sub(r"[\s-]", "", number or "")
    return len(n) == 12 and n[0] in "23456789" and verhoeff_valid(n)


# ============================================================================ ICAO 9303 MRZ
_MRZ_WEIGHTS = (7, 3, 1)


def mrz_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - 55
    return 0  # '<' filler


def mrz_check_digit(field_value: str) -> int:
    return sum(mrz_value(ch) * _MRZ_WEIGHTS[i % 3] for i, ch in enumerate(field_value)) % 10


def mrz_check_ok(field_value: str, check: str) -> bool:
    check = to_digits(check)
    if check == "<":
        return field_value.strip("<") == "" or mrz_check_digit(field_value) == 0
    return check.isdigit() and mrz_check_digit(field_value) == int(check)


@dataclass(slots=True)
class MrzData:
    format: str  # "TD3" | "TD1"
    doc_code: str
    issuer: str
    surname: str
    given_names: str
    number: str
    nationality: str
    dob: date | None
    sex: str
    expiry: date | None
    checks: dict[str, bool] = field(default_factory=dict)
    corrected: bool = False

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.given_names, self.surname) if p).strip()

    @property
    def valid(self) -> bool:
        """Document number, birth date, expiry and composite check digits all pass."""
        return all(self.checks.get(k, False) for k in ("number", "dob", "expiry", "composite"))


def _mrz_clean(line: str) -> str:
    s = line.upper().replace(" ", "")
    s = s.replace("«", "<<").replace("»", "<<").replace("‹", "<")
    s = re.sub(r"[(\[{_]", "<", s)
    return "".join(ch for ch in s if ch.isascii() and (ch.isalnum() or ch == "<"))


def _mrz_like(line: str) -> bool:
    raw = line.replace(" ", "")
    if len(raw) < 24:
        return False
    good = sum(1 for ch in raw if ch.isupper() or ch.isdigit() or ch in "<«»‹(")
    return good / len(raw) >= 0.85 and ("<" in raw or "«" in raw)


def _fit(s: str, n: int) -> str:
    """Pad with fillers / trim trailing fillers to an exact MRZ line length."""
    if len(s) < n:
        return s + "<" * (n - len(s))
    if len(s) > n:
        trimmed = s.rstrip("<")
        if len(trimmed) <= n:
            return trimmed + "<" * (n - len(trimmed))
        return s[:n]
    return s


def _mrz_date(value: str, *, kind: str, today: date | None = None) -> date | None:
    v = to_digits(value)
    if not re.fullmatch(r"\d{6}", v):
        return None
    yy, mm, dd = int(v[:2]), int(v[2:4]), int(v[4:6])
    today = today or date.today()
    if kind == "dob":
        year = 1900 + yy if yy > today.year % 100 else 2000 + yy
    else:  # expiry: documents run for <= ~20 years
        year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


_LETTER_AS_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "Z": "2", "S": "5", "G": "6", "B": "8"}


def _repair_alnum(field_value: str, check: str, *, max_changes: int = 2) -> tuple[str, bool] | None:
    """Fix letter-for-digit OCR confusions (O->0, I->1, S->5 ...) in an alphanumeric MRZ field so that its check
    digit validates. Deliberately conservative - a mod-10 check digit validates 1 in 10 random variants:
    only letters are turned into digits, at most two of them, and only when the check digit itself was read as a
    digit (a check digit misread as a letter makes any "repair" meaningless)."""
    if mrz_check_ok(field_value, check):
        return field_value, False
    if not check.isdigit():
        return None
    positions = [i for i, ch in enumerate(field_value) if ch in _LETTER_AS_DIGIT]
    if not positions or len(positions) > 9:
        return None
    valid: dict[int, list[str]] = {}
    for flips in product((False, True), repeat=len(positions)):
        changes = sum(flips)
        if changes == 0 or changes > max_changes:
            continue
        chars = list(field_value)
        for pos, flip in zip(positions, flips):
            if flip:
                chars[pos] = _LETTER_AS_DIGIT[chars[pos]]
        cand = "".join(chars)
        if mrz_check_ok(cand, check):
            valid.setdefault(changes, []).append(cand)
    if not valid:
        return None
    fewest = valid[min(valid)]
    return (fewest[0], True) if len(fewest) == 1 else None  # two equally small repairs: ambiguous, trust neither


def _mrz_names(region: str) -> tuple[str, str]:
    region = to_alpha(region)
    core = region.split("<<<")[0]  # real names never contain three fillers in a row
    surname, _, given = core.partition("<<")

    def clean(s: str) -> str:
        parts = [p for p in re.split(r"<+", s) if p.isalpha()]
        while len(parts) > 1 and len(parts[-1]) == 1 and parts[-1] in "KCLSE":  # '<' misread as K / C ...
            parts.pop()
        return " ".join(parts)

    return clean(surname), clean(given)


def _parse_number_field(number: str, check: str, overflow: str = "") -> tuple[str, bool, bool]:
    """-> (number, check ok, corrected). TD1 numbers longer than 9 chars continue in the optional field."""
    if check == "<" and overflow:
        tail = overflow.split("<")[0]
        if len(tail) >= 2:
            full = number + tail[:-1]
            return full.replace("<", ""), mrz_check_ok(full, tail[-1]), False
    repaired = _repair_alnum(number, check)
    if repaired is None:
        return number.replace("<", ""), False, False
    fixed, corrected = repaired
    return fixed.replace("<", ""), True, corrected


def parse_td3(line1: str, line2: str, today: date | None = None) -> MrzData | None:
    l1, l2 = _fit(_mrz_clean(line1), 44), _mrz_clean(line2)
    if not l1.startswith("P") or len(l2) < 40:
        return None
    head = l2[:28]
    if len(head) < 28:
        return None
    tail = l2[28:]
    composite_ch = tail[-1] if tail else "<"
    personal_check = tail[-2] if len(tail) >= 2 else "<"
    personal = _fit(tail[:-2] if len(tail) >= 2 else "", 14)
    number, number_ok, corrected = _parse_number_field(head[0:9], head[9])
    nationality = to_alpha(head[10:13])
    dob_raw, dob_check = to_digits(head[13:19]), head[19]
    sex = {"M": "M", "F": "F"}.get(head[20], "X")
    exp_raw, exp_check = to_digits(head[21:27]), head[27]
    number_field = _fit(number, 9) if len(number) <= 9 else number
    composite_src = number_field + to_digits(head[9]) + dob_raw + to_digits(dob_check) + exp_raw + to_digits(exp_check) + personal + to_digits(personal_check)
    checks = {
        "number": number_ok,
        "dob": mrz_check_ok(dob_raw, dob_check),
        "expiry": mrz_check_ok(exp_raw, exp_check),
        "personal": mrz_check_ok(personal, personal_check),
        "composite": mrz_check_ok(composite_src, composite_ch),
    }
    surname, given = _mrz_names(l1[5:44])
    return MrzData("TD3", l1[0:2].replace("<", ""), to_alpha(l1[2:5]).replace("<", ""), surname, given, number,
                   nationality.replace("<", ""), _mrz_date(dob_raw, kind="dob", today=today), sex,
                   _mrz_date(exp_raw, kind="expiry", today=today), checks, corrected)


def parse_td1(line1: str, line2: str, line3: str, today: date | None = None) -> MrzData | None:
    l1, l2, l3 = _fit(_mrz_clean(line1), 30), _fit(_mrz_clean(line2), 30), _fit(_mrz_clean(line3), 30)
    if l1[0] not in "IAC":
        return None
    number, number_ok, corrected = _parse_number_field(l1[5:14], l1[14], l1[15:30])
    dob_raw, dob_check = to_digits(l2[0:6]), l2[6]
    sex = {"M": "M", "F": "F"}.get(l2[7], "X")
    exp_raw, exp_check = to_digits(l2[8:14]), l2[14]
    nationality = to_alpha(l2[15:18]).replace("<", "")
    l1_fixed = l1[:5] + (_fit(number, 9) if len(number) <= 9 else l1[5:14]) + l1[14:]
    composite_src = l1_fixed[5:30] + dob_raw + to_digits(dob_check) + exp_raw + to_digits(exp_check) + l2[18:29]
    checks = {
        "number": number_ok,
        "dob": mrz_check_ok(dob_raw, dob_check),
        "expiry": mrz_check_ok(exp_raw, exp_check),
        "composite": mrz_check_ok(composite_src, l2[29]),
    }
    surname, given = _mrz_names(l3)
    return MrzData("TD1", l1[0:2].replace("<", ""), to_alpha(l1[2:5]).replace("<", ""), surname, given, number,
                   nationality, _mrz_date(dob_raw, kind="dob", today=today), sex,
                   _mrz_date(exp_raw, kind="expiry", today=today), checks, corrected)


def find_mrz(text: str, today: date | None = None) -> MrzData | None:
    """Locate and parse a TD3 (2 x 44) or TD1 (3 x 30) machine-readable zone. Best (most checks passing) wins."""
    lines = [ln for ln in normalize_text(text).split("\n") if _mrz_like(ln)]
    found: list[MrzData] = []
    cleaned = [_mrz_clean(ln) for ln in lines]
    for i in range(len(cleaned)):
        a = cleaned[i]
        if i + 1 < len(cleaned) and len(a) >= 38 and len(cleaned[i + 1]) >= 38 and a.startswith("P"):
            m = parse_td3(lines[i], lines[i + 1], today)
            if m:
                found.append(m)
        if i + 2 < len(cleaned) and all(24 <= len(cleaned[j]) <= 34 for j in (i, i + 1, i + 2)) and a[:1] in "IAC":
            m = parse_td1(lines[i], lines[i + 1], lines[i + 2], today)
            if m:
                found.append(m)
    if not found:
        return None
    return max(found, key=lambda m: (sum(m.checks.values()), bool(m.surname)))


# ============================================================================ dates
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "SEPT": 9,
          "OCT": 10, "NOV": 11, "DEC": 12}
_MON = r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEPT?|OCT|NOV|DEC)[A-Z]*\.?"
_DATE_PATTERNS = [
    ("dmy", re.compile(r"(?<!\d)(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})(?!\d)")),
    ("ymd", re.compile(r"(?<!\d)(\d{4})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?!\d)")),
    ("d_mon_y", re.compile(r"(?<!\d)(\d{1,2})\s*[-/. ]?\s*" + _MON + r"\s*[-/., ]?\s*(\d{4})(?!\d)", re.I)),
    ("mon_d_y", re.compile(_MON + r"\s+(\d{1,2}),?\s+(\d{4})(?!\d)", re.I)),
]
DOB_LABEL = re.compile(r"\b(D\s*\.?\s*O\s*\.?\s*B|DATE\s*OF\s*BIRTH|BIRTH\s*DATE|BIRTH|BORN|DOB)\b|जन्म", re.I)
YOB_LABEL = re.compile(r"\b(YEAR\s*OF\s*BIRTH|YOB)\b\D{0,12}((?:19|20)\d{2})(?!\d)", re.I)
EXPIRY_LABEL = re.compile(r"\b(EXPIRY|EXPIRES|EXPIRATION|EXP|DATE\s*OF\s*EXPIRY|VALID\s*(TILL|UPTO|UP\s*TO|UNTIL|THRU|THROUGH|TO)|VALIDITY)\b", re.I)
ISSUE_LABEL = re.compile(r"\b(ISSUED?|DOI|DATE\s*OF\s*ISSUE|ISSUE\s*DATE)\b", re.I)


def _mk_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if 1900 <= y <= 2100 else None
    except ValueError:
        return None


def parse_dates(line: str, *, country: str = "IN") -> list[tuple[int, date]]:
    """All dates in a line with their character offset (dd/mm/yyyy, yyyy-mm-dd, dd MMM yyyy, MMM dd, yyyy)."""
    found: dict[int, date] = {}
    month_first = (country or "").upper() in ("US", "PH", "FM")
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(line):
            d: date | None = None
            if kind == "dmy":
                a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if month_first and a <= 12 and b <= 31:
                    d = _mk_date(y, a, b) or _mk_date(y, b, a)
                else:
                    d = _mk_date(y, b, a) or (_mk_date(y, a, b) if b > 12 else None)
            elif kind == "ymd":
                d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind == "d_mon_y":
                d = _mk_date(int(m.group(3)), MONTHS[m.group(2).upper()], int(m.group(1)))
            else:
                d = _mk_date(int(m.group(3)), MONTHS[m.group(1).upper()], int(m.group(2)))
            if d is not None and m.start() not in found:
                found[m.start()] = d
    return sorted(found.items())


def parse_date(value: str, *, country: str = "IN") -> date | None:
    dates = parse_dates(value, country=country)
    return dates[0][1] if dates else None


def _age(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# ============================================================================ names
HONORIFICS = {"MR", "MRS", "MS", "MISS", "DR", "SHRI", "SMT", "KUMARI", "KU", "SRI", "MASTER", "PROF", "SMT.", "SHRIMATI"}
_NAME_NOISE = {"NAME", "NAAM", "GOVERNMENT", "INDIA", "OF", "REPUBLIC", "CARD", "IDENTITY", "NATIONAL", "PASSPORT",
               "LICENCE", "LICENSE", "DRIVING", "INCOME", "TAX", "DEPARTMENT", "ELECTION", "COMMISSION", "MALE", "FEMALE",
               "DOB", "BIRTH", "DATE", "AADHAAR", "UNIQUE", "AUTHORITY", "SIGNATURE", "PERMANENT", "ACCOUNT", "NUMBER",
               "FATHER", "FATHERS", "MOTHER", "HUSBAND", "GUARDIAN", "ADDRESS", "VALID", "EXPIRY", "ISSUE", "GOVT",
               "SPECIMEN", "DEMO", "NOT", "REAL", "DOCUMENT", "HOLDER", "SEX", "GENDER", "NATIONALITY", "TYPE", "CODE",
               "TO", "THE", "YEAR", "ENROLMENT", "VID", "NO", "ID", "COUNTRY", "STATE", "TRANSPORT", "UNION"}


def name_tokens(name: str | None) -> list[str]:
    if not name:
        return []
    ascii_name = unicodedata.normalize("NFKD", normalize_text(name)).encode("ascii", "ignore").decode()
    toks = [t for t in re.split(r"[^A-Z]+", ascii_name.upper()) if t and t not in HONORIFICS]
    if len([t for t in toks if len(t) > 1]) >= 2:
        toks = [t for t in toks if len(t) > 1]  # drop initials when the name has enough real parts
    return toks


def name_similarity(a: str | None, b: str | None) -> float:
    """Token-set ratio (like fuzzywuzzy's): 1.0 when one name's tokens are a subset of the other's."""
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = sorted(ta & tb)
    s_inter = " ".join(inter)
    s1 = " ".join([s_inter] + sorted(ta - tb)).strip()
    s2 = " ".join([s_inter] + sorted(tb - ta)).strip()
    ratios = [SequenceMatcher(None, s1, s2).ratio()]
    if inter:
        ratios += [SequenceMatcher(None, s_inter, s1).ratio(), SequenceMatcher(None, s_inter, s2).ratio()]
    # OCR-tolerant pairing: each token of the shorter name against its best match in the longer one
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    paired = [max(SequenceMatcher(None, t, u).ratio() for u in long_) for t in short]
    if paired and len(short) >= max(1, min(2, len(long_))):
        ratios.append(sum(paired) / len(paired) * (0.97 if len(short) < len(long_) else 1.0))
    return round(max(ratios), 4)


def name_in_text(name: str | None, text: str | None) -> float:
    """Share of the account name (by letters) that appears in the OCR text, token by token (fuzzy >= 0.8)."""
    toks = [t for t in name_tokens(name) if len(t) >= 2]
    if not toks or not text:
        return 0.0
    words = set(re.findall(r"[A-Z]{2,}", unicodedata.normalize("NFKD", normalize_text(text)).encode("ascii", "ignore").decode().upper()))
    if not words:
        return 0.0
    total = sum(len(t) for t in toks)
    hit = 0
    for t in toks:
        if t in words or max(SequenceMatcher(None, t, w).ratio() for w in words) >= 0.8:
            hit += len(t)
    return round(hit / total, 4)


_NAME_LABEL = re.compile(r"^(?:.*?\b)?(NAME|NAAM)\b\s*[:.\-/]?\s*(.*)$", re.I)
_RELATIVE_LABEL = re.compile(r"\b(FATHER|MOTHER|HUSBAND|GUARDIAN|SPOUSE|S\s*/\s*O|D\s*/\s*O|W\s*/\s*O|C\s*/\s*O|SON\s+OF|DAUGHTER\s+OF|WIFE\s+OF)\b", re.I)


def _clean_name(value: str) -> str | None:
    value = re.sub(r"[^A-Za-z .'-]", " ", value)
    toks = [t for t in re.split(r"\s+", value.strip()) if t]
    toks = [t for t in toks if t.upper().strip(".'-") not in _NAME_NOISE]
    letters = sum(len(t) for t in toks)
    if not toks or len(toks) > 5 or letters < 3:
        return None
    return " ".join(t.strip(".'-") for t in toks).upper().strip() or None


def _label_name(lines: list[str]) -> str | None:
    for i, ln in enumerate(lines):
        m = _NAME_LABEL.match(ln)
        if not m or _RELATIVE_LABEL.search(ln[: m.start(1) + 4]):
            continue
        value = _clean_name(m.group(2) or "")
        if value is None and i + 1 < len(lines) and not _RELATIVE_LABEL.search(lines[i + 1]):
            value = _clean_name(lines[i + 1])
        if value:
            return value
    return None


def _heuristic_name(lines: list[str]) -> str | None:
    """Aadhaar-style cards print the name without a label, on the line just above the DOB line."""
    for i, ln in enumerate(lines):
        if DOB_LABEL.search(ln) or YOB_LABEL.search(ln):
            for j in (i - 1, i - 2):
                if j < 0:
                    break
                cand = lines[j]
                if any(ch.isdigit() for ch in cand) or _RELATIVE_LABEL.search(cand):
                    continue
                words = [w for w in re.split(r"\s+", cand) if w]
                if 2 <= len(words) <= 4 and all(re.fullmatch(r"[A-Za-z.'-]+", w) for w in words):
                    value = _clean_name(cand)
                    if value and len(value.split()) >= 2:
                        return value
    return None


# ============================================================================ ID numbers
IN_STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CH", "CG", "CT", "DD", "DL", "DN", "GA", "GJ", "HP", "HR", "JH", "JK", "KA", "KL",
    "LA", "LD", "MH", "ML", "MN", "MP", "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TN", "TG", "TS", "TR", "UK",
    "UA", "UP", "WB",
}
CHECKSUM_KINDS = {"aadhaar", "mrz_td3", "mrz_td1"}
KIND_LABELS = {"aadhaar": "Aadhaar", "pan": "PAN", "epic": "Voter ID (EPIC)", "dl_in": "Driving licence",
               "passport": "Passport number", "mrz_td3": "Passport MRZ", "mrz_td1": "ID-card MRZ", "generic": "ID number"}
DOC_TYPE_KINDS = {
    "aadhaar": ("aadhaar",),
    "pan": ("pan",),
    "voter_id": ("epic", "generic"),
    "driving_license": ("dl_in", "mrz_td1", "generic"),
    "passport": ("mrz_td3", "passport", "generic"),
    "national_id": ("mrz_td1", "aadhaar", "mrz_td3", "generic"),
    "residence_permit": ("mrz_td1", "mrz_td3", "generic"),
    "student_id": ("generic",),
    "other": ("mrz_td3", "mrz_td1", "aadhaar", "pan", "epic", "dl_in", "passport", "generic"),
}
# PAN: AAAAA9999A where the 4th letter is the holder type - P individual, C company, H Hindu undivided family, F firm,
# A association of persons, T trust, B body of individuals, L local authority, J artificial juridical person, G government
PAN_HOLDER_TYPES = "PCHFATBLJG"
PAN_SHAPE_RE = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
PAN_VALID_RE = re.compile(rf"[A-Z]{{3}}[{PAN_HOLDER_TYPES}][A-Z][0-9]{{4}}[A-Z]")
# numbers printed on specimen / sample cards (the Income Tax Department's example PAN): never a real person's card
SPECIMEN_NUMBERS = {"ABCDE1234F": "PAN"}


def pan_shaped(number: str | None) -> bool:
    """Five letters, four digits, one letter - the outline of a PAN, whatever its holder-type letter."""
    return bool(number) and PAN_SHAPE_RE.fullmatch(number or "") is not None


def pan_valid(number: str | None) -> bool:
    """A PAN with a valid holder-type 4th letter (``PAN_HOLDER_TYPES``)."""
    return bool(number) and PAN_VALID_RE.fullmatch(number or "") is not None


def specimen_kind(number: str | None) -> str | None:
    """The card kind whose specimen (sample) card prints this number, e.g. 'PAN' for ABCDE1234F - else None."""
    return SPECIMEN_NUMBERS.get(normalize_number(number)) if number else None


def with_article(label: str) -> str:
    """'an ID number', 'an Aadhaar', 'a PAN' - the indefinite article for a label (every label here starts with a
    letter whose sound decides it)."""
    return ("an " if label[:1].upper() in "AEIOU" else "a ") + label


_D = r"[0-9OISBZQD]"
AADHAAR_RE = re.compile(r"(?<![0-9A-Z])(?<![0-9][ -])([2-9OISBZ]" + _D + r"{3})[ -]?(" + _D + r"{4})[ -]?(" + _D + r"{4})(?![ -]?[0-9])")
PAN_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{5})(" + _D + r"{4})([A-Z0-9])(?![A-Z0-9])")
EPIC_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{3})(" + _D + r"{7})(?![A-Z0-9])")
DL_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2})[ -]?(\d{2})[ -]?((?:19|20)\d{2})[ -]?(\d{7})(?!\d)")
PASSPORT_RE = re.compile(r"(?<![A-Z0-9])([A-Z])[ ]?(\d{7})(?![A-Z0-9])")
GENERIC_RE = re.compile(r"(?<![A-Z0-9])([A-Z0-9][A-Z0-9-]{4,22}[A-Z0-9])(?![A-Z0-9])")
DIGIT_GROUPS_RE = re.compile(r"(?<![0-9A-Z])(\d{3,4}(?:[ -]\d{3,6}){1,3})(?![0-9A-Z])")
NUMBER_LABEL = re.compile(r"\b(NO|NUMBER|NUM|ID|CARD|DOCUMENT|DOC|LICEN[CS]E|DL|PASSPORT|EPIC|PAN|AADHAAR)\b|#", re.I)


@dataclass(slots=True)
class IdCandidate:
    kind: str
    number: str  # normalised: upper-case letters + digits only
    checksum: str  # "valid" | "invalid" | "none"
    score: float = 0.0
    labelled: bool = False

    @property
    def has_checksum(self) -> bool:
        return self.kind in CHECKSUM_KINDS


@dataclass(slots=True)
class Extraction:
    chars: int = 0
    candidates: list[IdCandidate] = field(default_factory=list)
    name: str | None = None
    name_source: str | None = None  # "mrz" | "label" | "heuristic"
    dob: date | None = None
    yob: int | None = None
    expiry: date | None = None
    mrz: MrzData | None = None

    @property
    def primary(self) -> IdCandidate | None:
        return self.candidates[0] if self.candidates else None


def kind_preference(doc_type: str, kind: str) -> int:
    kinds = DOC_TYPE_KINDS.get(doc_type, DOC_TYPE_KINDS["other"])
    if kinds and kind == kinds[0]:
        return 2
    return 1 if kind in kinds else 0


def _score(c: IdCandidate, doc_type: str) -> float:
    base = {"valid": 3.0, "none": 1.5, "invalid": -1.0}[c.checksum]
    if c.kind == "generic":
        base = 0.5 + (0.8 if c.labelled else 0.0) + min(0.4, len(c.number) / 40)
    return base + 2.0 * kind_preference(doc_type, c.kind) + (0.3 if c.labelled else 0.0)


def _id_candidates(lines: list[str], doc_type: str, country: str, mrz: MrzData | None) -> list[IdCandidate]:
    found: dict[str, IdCandidate] = {}

    def add(kind: str, number: str, checksum: str, labelled: bool = False) -> None:
        number = normalize_number(number)
        if len(number) < 5:
            return
        c = IdCandidate(kind, number, checksum, labelled=labelled)
        c.score = _score(c, doc_type)
        prev = found.get(number)
        if prev is None or c.score > prev.score:
            found[number] = c

    if mrz is not None and mrz.number:
        add("mrz_td3" if mrz.format == "TD3" else "mrz_td1", mrz.number, "valid" if mrz.checks.get("number") else "invalid", True)

    mrz_lines = {ln for ln in lines if _mrz_like(ln)}
    for idx, ln in enumerate(lines):
        if ln in mrz_lines:
            continue
        up = ln.upper()
        labelled = bool(NUMBER_LABEL.search(up)) or (idx > 0 and bool(NUMBER_LABEL.search(lines[idx - 1].upper())))
        vid_line = bool(re.search(r"\bVID\b", up))
        for m in AADHAAR_RE.finditer(up):
            raw = "".join(m.groups())
            if vid_line or sum(ch.isdigit() for ch in raw) < 9:
                continue
            digits = to_digits(raw)
            add("aadhaar", digits, "valid" if aadhaar_valid(digits) else "invalid", labelled)
        for m in PAN_RE.finditer(up):
            head, mid, last = m.groups()
            if sum(ch.isalpha() for ch in head) < 4 or not last.isalpha() and last not in "0158":
                continue
            pan = to_alpha(head) + to_digits(mid) + to_alpha(last)
            if pan_valid(pan):
                add("pan", pan, "none", labelled)
        for m in EPIC_RE.finditer(up):
            head, digits = m.groups()
            if sum(ch.isalpha() for ch in head) < 2:
                continue
            epic = to_alpha(head) + to_digits(digits)
            if re.fullmatch(r"[A-Z]{3}[0-9]{7}", epic):
                add("epic", epic, "none", labelled)
        for m in DL_RE.finditer(up):
            state, rto, year, serial = m.groups()
            if state in IN_STATE_CODES and 1950 <= int(year) <= date.today().year:
                add("dl_in", state + rto + year + serial, "none", labelled)
        if doc_type == "passport" or re.search(r"PASSPORT", up):
            for m in PASSPORT_RE.finditer(up):
                add("passport", m.group(1) + m.group(2), "none", labelled)
        # generic numbers: 6-20 alphanumerics containing digits (+ spaced digit groups such as "1234 5678 9012")
        for m in GENERIC_RE.finditer(up):
            tok = m.group(1).replace("-", "")
            if not 6 <= len(tok) <= 20 or sum(ch.isdigit() for ch in tok) < 2 or tok in found:
                continue
            if parse_dates(m.group(1)) or re.fullmatch(r"(19|20)\d{2}", tok):
                continue
            add("generic", tok, "none", labelled)
        for m in DIGIT_GROUPS_RE.finditer(up):
            tok = re.sub(r"[ -]", "", m.group(1))
            if 8 <= len(tok) <= 20 and tok not in found and not parse_dates(m.group(1)):
                add("generic", tok, "none", labelled)
    return sorted(found.values(), key=lambda c: (-c.score, c.number))


def extract_fields(text: str | None, *, doc_type: str = "other", country: str = "IN", today: date | None = None) -> Extraction:
    """Everything one OCR engine's text tells us about the card (never raises)."""
    today = today or date.today()
    norm = normalize_text(text)
    ex = Extraction(chars=alnum_count(norm))
    if not norm:
        return ex
    lines = norm.split("\n")
    try:
        ex.mrz = find_mrz(norm, today)
    except Exception:  # noqa: BLE001 - malformed OCR must never break extraction
        ex.mrz = None
    ex.candidates = _id_candidates(lines, doc_type, country, ex.mrz)

    # ---- name
    if ex.mrz is not None and ex.mrz.surname and (ex.mrz.checks.get("number") or ex.mrz.checks.get("dob")):
        ex.name, ex.name_source = ex.mrz.name.upper(), "mrz"
    else:
        label = _label_name(lines)
        if label:
            ex.name, ex.name_source = label, "label"
        else:
            guess = _heuristic_name(lines)
            if guess:
                ex.name, ex.name_source = guess, "heuristic"
            elif ex.mrz is not None and ex.mrz.surname:
                ex.name, ex.name_source = ex.mrz.name.upper(), "mrz"

    # ---- dates
    labelled_dob: date | None = None
    labelled_exp: date | None = None
    unlabelled: list[date] = []
    for i, ln in enumerate(lines):
        dates = parse_dates(ln, country=country)
        if not dates:
            continue
        is_dob = bool(DOB_LABEL.search(ln)) or (i > 0 and bool(DOB_LABEL.search(lines[i - 1])) and not parse_dates(lines[i - 1]))
        is_exp = bool(EXPIRY_LABEL.search(ln)) or (i > 0 and bool(EXPIRY_LABEL.search(lines[i - 1])) and not parse_dates(lines[i - 1]))
        is_issue = bool(ISSUE_LABEL.search(ln))
        if is_dob and labelled_dob is None:
            labelled_dob = min(d for _pos, d in dates)
        elif is_exp and labelled_exp is None:
            labelled_exp = max(d for _pos, d in dates)
        elif not is_issue:
            unlabelled += [d for _pos, d in dates]
    m = YOB_LABEL.search(norm)
    if m:
        ex.yob = int(m.group(2))
    mrz_ok = ex.mrz is not None
    if labelled_dob is not None:
        ex.dob = labelled_dob
    elif mrz_ok and ex.mrz.dob and ex.mrz.checks.get("dob"):  # type: ignore[union-attr]
        ex.dob = ex.mrz.dob  # type: ignore[union-attr]
    else:
        plausible = [d for d in unlabelled if 5 <= _age(d, today) <= 110]
        if plausible and ex.yob is None:
            ex.dob = min(plausible)
    if labelled_exp is not None:
        ex.expiry = labelled_exp
    elif mrz_ok and ex.mrz.expiry and ex.mrz.checks.get("expiry"):  # type: ignore[union-attr]
        ex.expiry = ex.mrz.expiry  # type: ignore[union-attr]
    if ex.yob is None and ex.dob is not None:
        ex.yob = ex.dob.year
    return ex


# ============================================================================ consensus
@dataclass(slots=True)
class Consensus:
    number: str | None  # accepted number (None when the engines do not establish one)
    kind: str | None
    checksum: str  # "valid" | "invalid" | "none" | "missing"
    agreeing: list[str] = field(default_factory=list)  # engines that read the accepted (or best-guess) number
    with_output: list[str] = field(default_factory=list)  # engines that produced readable text
    found_by: list[str] = field(default_factory=list)  # engines that found any ID number
    outcome: str = "none"  # "agreement" | "checksum" | "single" | "disagreement" | "checksum_invalid" | "none"
    best_guess: str | None = None

    @property
    def score(self) -> float:
        return round(len(self.agreeing) / len(self.with_output), 4) if self.with_output else 0.0

    @property
    def accepted(self) -> bool:
        return self.number is not None


def build_consensus(per_engine: dict[str, Extraction], *, doc_type: str = "other", min_chars: int = 1) -> Consensus:
    """An ID number is accepted when >= 2 engines agree (Levenshtein <= 1) and its checksum validates where one
    exists, or when a single engine reads it with a valid checksum / MRZ check digit."""
    with_output = [e for e, x in per_engine.items() if x.chars >= min_chars]
    top = {e: x.candidates[:3] for e, x in per_engine.items() if x.candidates}
    found_by = list(top)
    if not top:
        return Consensus(None, None, "missing", [], with_output, [], "none", None)

    pool: dict[str, IdCandidate] = {}
    for cands in top.values():
        for c in cands:
            prev = pool.get(c.number)
            if prev is None:
                pool[c.number] = c
            elif (c.checksum == "valid") != (prev.checksum == "valid"):
                if c.checksum == "valid":  # one engine misread a check digit: the valid reading wins
                    pool[c.number] = c
            elif c.score > prev.score:
                pool[c.number] = c

    def support(number: str) -> list[str]:
        return [e for e, cands in top.items() if any(numbers_close(number, c.number) for c in cands)]

    def exact_support(number: str) -> list[str]:
        return [e for e, cands in top.items() if any(number == c.number for c in cands)]

    ranked = sorted(
        pool.values(),
        key=lambda c: (kind_preference(doc_type, c.kind), c.checksum == "valid", len(support(c.number)),
                       len(exact_support(c.number)), c.checksum != "invalid", c.score, c.number),
        reverse=True,
    )
    best = ranked[0]
    agreeing = support(best.number)
    if best.has_checksum:
        if best.checksum == "valid":
            outcome = "agreement" if len(agreeing) >= 2 else "checksum"
            return Consensus(best.number, best.kind, "valid", agreeing, with_output, found_by, outcome, best.number)
        exact = exact_support(best.number)
        if len(exact) >= 2:  # several engines read the very same number and it fails its checksum
            return Consensus(None, best.kind, "invalid", exact, with_output, found_by, "checksum_invalid", best.number)
        return Consensus(None, best.kind, "invalid", agreeing if len(agreeing) > 1 else [], with_output, found_by,
                         "disagreement" if len(found_by) > 1 else "single", best.number)
    if len(agreeing) >= 2:
        # pick the variant most engines read exactly (OCR noise on one engine must not change the stored number)
        votes = Counter(c.number for e in agreeing for c in top[e] if numbers_close(best.number, c.number))
        number = max(votes, key=lambda n: (votes[n], pool[n].score if n in pool else 0, n))
        return Consensus(number, best.kind, "none", agreeing, with_output, found_by, "agreement", number)
    outcome = "single" if len(found_by) == 1 else "disagreement"
    return Consensus(None, best.kind, "none", [], with_output, found_by, outcome, best.number)
