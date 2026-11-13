"""Random junk for the load test - ONE generator for the seeded database (seed_loadtest.py) and the JunkUser requests
(locustfile.py). Standard library only (the seeder runs it with the project interpreter, Locust with .venv-loadtest).

What "junk" means here (README "Random junk data"):
* emoji and other 4-byte characters (ZWJ sequences, flags, skin tones, CJK extension B, mathematical alphanumerics)
* right-to-left and mixed-direction text (Arabic, Hebrew, Urdu with Latin and digits, bidi controls such as RLO/LRI)
* zero-width and combining characters (ZWSP / ZWJ / word joiner / BOM inside words, "Zalgo" stacks of diacritics)
* maximum-length strings (exactly the column's / field's limit, single-byte or 4-byte characters, no spaces)
* whitespace-heavy strings (tabs, newlines, NBSP, em / ideographic spaces, line / paragraph separators)
* HTML / JS / SQL / template-looking strings ("<script>alert(1)</script>", "' OR 1=1 --", "{{7*7}}" ...)
* control characters (BEL, backspace, ANSI escapes, DEL, C1) - and, in REQUESTS only, NUL bytes (PostgreSQL text
  cannot store NUL, so the seeded data never contains one)

Every database value keeps at least one visible character and stays within the column's length (code points, as
PostgreSQL's char_length counts them). Request junk may break every rule on purpose: oversized, empty, missing,
duplicated, wrongly typed, malformed - see ``value`` / ``query`` / ``json_body`` / ``junk_file``.
"""

from __future__ import annotations

import json
import random
import string
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlsplit

# ------------------------------------------------------------------------------------------------ character pools
EMOJI = ["💪", "🏋️‍♀️", "🏋🏽‍♂️", "🔥", "😀", "🤸🏾", "🧘‍♀️", "🥗", "🍫", "🇮🇳", "🏳️‍🌈", "👨‍👩‍👧‍👦", "🫀", "🦵🏿", "🥇",
         "❤️‍🔥", "🧑🏻‍🦽", "🤌", "🫶🏼", "🌶️", "☕", "🚴‍♂️", "⛹🏻‍♀️", "🏊", "🥊"]
ASTRAL = ["𝓘𝓻𝓸𝓷", "𝕻𝖚𝖑𝖘𝖊", "𝐆𝐲𝐦", "𝟙𝟚𝟛", "𠜎𠜱𠝹", "𡃁𡃅", "𐐷𐑊", "𝄞", "🀄", "𓀀𓁐", "𐌰𐌱", "𞤀𞤁"]
RTL = ["مرحبا بالعالم", "نادي الحديد", "שלום עולם", "חדר כושר", "پاور جم", "ژیم آهنی", "تمرين ٣ مرات",
       "قوة 💪 100%", "קרוספיט 24/7", "ورزش 🏋️ صبح"]
BIDI = ["‎", "‏", "‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩",
        "؜"]
ZERO_WIDTH = ["​", "‌", "‍", "⁠", "﻿", "᠎", "͏"]
COMBINING = [chr(c) for c in range(0x300, 0x370)] + ["⃝", "҉", "᪰", "︠"]
SPACES = [" ", "  ", "\t", "\n", "\r\n", " ", " ", " ", " ", " ", "　", " ",
          " ", " ", " "]
CONTROL = ["\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1b[31m", "\x1b[0m", "\x1f", "\x7f", "\x85", "\x9b", "\x1a"]
LATIN = ["Iron", "Pulse", "gym", "Squat", "protein", "Leg day", "Zoë", "Ångström", "naïve", "façade", "Straße",
         "İstanbul", "ǅemal", "ﬁt", "Ⅻ", "½", "№7", "Ω", "Привет", "Γειά", "日本語", "한국어", "हिन्दी", "தமிழ்", "ไทย"]
MARKUP = ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "\"><svg onload=alert(1)>", "' OR 1=1 --",
          "' OR '1'='1", "{{7*7}}", "${7*7}", "{% raw %}{{x}}{% endraw %}", "<b>bold</b><i>it", "</textarea>",
          "</title><h1>junk</h1>", "Robert'); DROP TABLE students;--", "\\\"; alert(1); //", "&amp;&lt;&gt;&quot;&#39;",
          "%3Cscript%3E", "javascript:alert(1)", "<!--", "]]>", "@{{constructor.constructor('alert(1)')()}}",
          "1; SELECT pg_sleep(5)", "admin'--", "\\u003cscript\\u003e", "<a href=\"javascript:alert(1)\">x</a>",
          "*/alert(1)/*", "`${alert(1)}`", "<style>*{display:none}</style>", "#{7*7}", "<%= 7*7 %>", "{{config}}"]
# substrings of the seeded junk that must never reach a page unescaped (ltlib/browser.py checks every HTML page)
XSS_MARKERS = ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<svg onload=alert(1)>")
DB_KINDS = ("emoji", "rtl", "zero_width", "max_length", "whitespace", "markup", "control", "mixed")


def _visible(text: str) -> bool:
    return any(not ch.isspace() and ch not in ZERO_WIDTH and ch not in BIDI and ord(ch) >= 0x20 for ch in text)


def _emoji(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(EMOJI + ASTRAL) for _ in range(max(1, n)))


def _zalgo(rng: random.Random, word: str, depth: int) -> str:
    return "".join(ch + "".join(rng.choice(COMBINING) for _ in range(rng.randint(0, depth))) for ch in word)


def _word(rng: random.Random) -> str:
    return rng.choice(LATIN + RTL[:3] + ["Kumar", "Iyer", "Fitness", "Club", "Whey", "Plan"])


def text(rng: random.Random, max_len: int, *, kind: str | None = None, attack: float = 1.0) -> str:
    """One junk string of 1..``max_len`` code points with at least one visible character (database-safe: no NUL).
    ``attack`` scales how often the HTML / SQL-looking kind is picked (0 = never)."""
    max_len = max(1, int(max_len))
    if kind is None:
        weights = [14, 13, 12, 10, 12, 16 * attack, 7, 16]
        kind = rng.choices(DB_KINDS, weights=weights, k=1)[0]
    if kind == "emoji":
        s = rng.choice(["", _word(rng) + " "]) + _emoji(rng, rng.randint(1, 8)) + rng.choice(["", " " + _word(rng)])
    elif kind == "rtl":
        s = rng.choice(RTL)
        if rng.random() < 0.6:
            s = f"{_word(rng)} {rng.choice(BIDI)}{s}{rng.choice(BIDI)} {rng.randint(1, 999)}"
        if rng.random() < 0.3:
            s = "‮" + s[::-1]  # RIGHT-TO-LEFT OVERRIDE: "gnp.exe" style
    elif kind == "zero_width":
        w = _word(rng)
        if rng.random() < 0.5:
            s = rng.choice(ZERO_WIDTH).join(w) + rng.choice(ZERO_WIDTH)
        else:
            s = _zalgo(rng, w, rng.randint(2, 12))
        if rng.random() < 0.3:
            s = rng.choice(COMBINING) + s  # a combining mark with nothing to combine with
    elif kind == "max_length":
        unit = rng.choice(["W", "€", "𝕏", "ﷺ", "a b ", "Ꙭ", "🏋️", "é", "漢", "-", "x"])
        s = (unit * (max_len // max(1, len(unit)) + 1))[:max_len]
        if rng.random() < 0.3 and max_len > 20:
            s = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(max_len))  # one endless word
    elif kind == "whitespace":
        s = "".join(rng.choice(SPACES) for _ in range(rng.randint(1, 6))) + _word(rng)
        s += "".join(rng.choice(SPACES) for _ in range(rng.randint(2, 10))) + rng.choice(["", _word(rng)])
        s += "".join(rng.choice(SPACES) for _ in range(rng.randint(0, 6)))
    elif kind == "markup":
        s = rng.choice(MARKUP)
        if rng.random() < 0.4:
            s = f"{_word(rng)} {s} {rng.choice(MARKUP)}"
    elif kind == "control":
        w = _word(rng)
        i = rng.randint(0, len(w))
        s = w[:i] + "".join(rng.choice(CONTROL) for _ in range(rng.randint(1, 3))) + w[i:]
    else:  # mixed: a bit of everything
        parts = [_emoji(rng, 2), rng.choice(RTL), _zalgo(rng, _word(rng), 3), rng.choice(SPACES),
                 rng.choice(MARKUP) if attack else _word(rng), rng.choice(ZERO_WIDTH), rng.choice(BIDI)]
        rng.shuffle(parts)
        s = " ".join(parts[: rng.randint(2, len(parts))])
    s = s[:max_len]
    if not _visible(s):  # cut down to nothing visible (zero-width / spaces only): one visible letter instead
        s = (s[: max_len - 1] + "x") if max_len > 1 else "x"
    if s == "\\N":  # the COPY NULL marker of the seeder
        s = "\\N."[:max_len] if max_len >= 3 else "x"
    return s


# ------------------------------------------------------------------------------------------------ request junk
NUMBERS = ["-1", "0", "-0", "1e309", "-1e309", "NaN", "nan", "Infinity", "-Infinity", "inf", "1e-400", "3.14159",
           "9" * 30, str(2**63), str(2**64), str(-(2**63) - 1), "0x1F", "1_000", " 42 ", "42abc", "١٢٣", "½", "Ⅻ",
           "1,5", "+5", "--5", "1e5", "007", "9.999e999", "-2147483649", "4294967296", "0.0000001", "1" * 400, "²"]
DATES = ["2026-02-30", "0000-00-00", "9999-12-31", "0001-01-01", "-1", "today", "2026-13-01", "2026-11-13T25:61",
         "13/11/2026", "1e9", "29-02-2025", "２０２６-１１-１３", "2026-11-13'--"]
EXTRA_KEYS = ["__proto__", "constructor[prototype]", "a[]", "page[]", "x[y][z]", "%00", "😀", "id", "user_id",
              "role", "is_admin", "gym_id", "next", "callback", "_method", "format", "debug", "csrf_token", ""]


def number(rng: random.Random) -> str:
    return rng.choice(NUMBERS)


def value(rng: random.Random, max_len: int = 300, *, nul: bool = True, attack: float = 1.0) -> str:
    """One junk form / query value: text junk, numbers, dates, empty, oversized, NUL bytes, control characters."""
    r = rng.random()
    if r < 0.45:
        return text(rng, rng.choice([max_len, max_len, min(max_len, 40), max_len + rng.randint(1, 50)]), attack=attack)
    if r < 0.60:
        return number(rng)
    if r < 0.66:
        return rng.choice(DATES)
    if r < 0.74:
        return rng.choice(["", " ", "\t", "null", "undefined", "None", "[]", "{}", "true", "0"])
    if r < 0.84:
        unit = rng.choice(["A", "😀", "ﷺ", "<", "'", "%", "\\", "&", "=", "+", "‮", "é"])
        return unit * rng.randint(1_000, 12_000)  # oversized
    if r < 0.92 and nul:
        w = text(rng, 30, attack=0)
        return w[: len(w) // 2] + "\x00" * rng.randint(1, 3) + w[len(w) // 2:]
    return "".join(rng.choice(CONTROL + ["\x00"] if nul else CONTROL) for _ in range(rng.randint(1, 5))) + \
        text(rng, 20, attack=0)


def qenc(s: str) -> str:
    """Percent-encode (UTF-8) for a URL - every byte of the junk arrives as sent."""
    return quote(s, safe="")


def query(rng: random.Random, keys: list[str], *, attack: float = 1.0, max_len: int = 400) -> str:
    """A junk query string for a route with these known parameters: random subset (some missing), junk values,
    duplicated keys, random extra keys, odd separators."""
    pairs: list[tuple[str, str]] = []
    for k in keys:
        r = rng.random()
        if r < 0.25:
            continue  # missing
        v = value(rng, max_len, nul=rng.random() < 0.15, attack=attack)
        pairs.append((k, v))
        if r > 0.93:
            pairs.append((k, value(rng, 40, nul=False, attack=attack)))  # the same key twice
    for _ in range(rng.choice([0, 0, 1, 2])):
        pairs.append((rng.choice(EXTRA_KEYS), value(rng, 60, nul=False, attack=attack)))
    sep = "&" if rng.random() < 0.9 else rng.choice([";", "&&", "&amp;"])
    out = sep.join(f"{qenc(k)}={qenc(v)}" if rng.random() > 0.05 else qenc(k) for k, v in pairs)
    if rng.random() < 0.05:
        out += rng.choice(["&%C3%28=1", "&q=%FF%FE", "&q=%ED%A0%80", "&%E0%A4=x", "&==", "&&&", "&q=%2"])  # bad UTF-8
    return out


def path_segment(rng: random.Random) -> str:
    """A junk path segment (percent-encoded so the request line stays valid)."""
    r = rng.random()
    if r < 0.30:
        return qenc(text(rng, rng.choice([8, 30, 120]), attack=0.3))
    if r < 0.50:
        return rng.choice(NUMBERS[:20])
    if r < 0.60:
        return qenc("A" * rng.randint(300, 3000))
    if r < 0.70:
        return rng.choice(["%2F", "%2e", ".", "~", "%20", "%E2%80%AE", "%C0%AF", "%EF%BF%BD", ";x=1", "@", "%25", "+"])
    if r < 0.80:
        return qenc(rng.choice(EMOJI + RTL + ASTRAL))
    return "".join(rng.choice(string.ascii_letters + string.digits + "-_.") for _ in range(rng.randint(1, 40)))


def json_body(rng: random.Random, fields: dict[str, str], *, attack: float = 1.0) -> tuple[bytes, str]:
    """(body, what) - a junk JSON request body for an API whose real fields are ``fields`` (name -> kind: text /
    number / id / bool / list). ``what`` names the junk for the failure log."""
    r = rng.random()
    if r < 0.12:  # malformed or not an object at all
        raw = rng.choice(['{', '{"a":}', '[1,2', '{"a": NaN}', '{"a": Infinity}', "{'single': 'quotes'}", "\x00",
                          '﻿{"a": 1}', "null", '"just a string"', "123", "[]", "true", "", " ", '{"a":1}{"b":2}',
                          '{"a": "\\ud800"}', '{"a": 1e999}', '{"' + "k" * 5000 + '": 1}', "{\"a\":\"\x01\x02\"}"])
        return raw.encode("utf-8", "surrogatepass"), "malformed JSON"
    if r < 0.17:  # deep nesting (RecursionError in a hand-written request.json() handler)
        depth = rng.choice([200, 1200, 5000])
        return ("[" * depth + "]" * depth).encode(), f"JSON nested {depth} deep"
    if r < 0.21:  # a number no parser should keep
        return ('{"' + (next(iter(fields), "a")) + '": ' + "9" * rng.choice([400, 5000]) + "}").encode(), "huge integer"
    payload: dict[str, Any] = {}
    for name, kind in fields.items():
        q = rng.random()
        if q < 0.12:
            continue  # missing
        if q < 0.35:
            payload[name] = rng.choice([None, True, False, 0, -1, 2**63, 2**64 + 1, 1e308, -1e308, 3.5, [], {},
                                        [1, "a", None], {"$gt": ""}, "", " "])
        elif kind in ("number", "id"):
            payload[name] = rng.choice([number(rng), -1, 0, 2**63, 1.5, "1", 10**30])
        elif kind == "bool":
            payload[name] = rng.choice(["yes", 1, 0, "true", None, "on", [True]])
        elif kind == "list":
            payload[name] = rng.choice([[text(rng, 60, attack=attack) for _ in range(rng.randint(0, 8))],
                                        ["x"] * rng.randint(50, 500), "not a list", [[[]]], [None, 1, {}]])
        else:
            payload[name] = value(rng, rng.choice([200, 2000, 5000]), attack=attack)
    for _ in range(rng.choice([0, 0, 1, 3])):
        payload[rng.choice(EXTRA_KEYS) or "x"] = rng.choice([value(rng, 50, nul=False, attack=attack), 1, None, [1]])
    return json.dumps(payload, ensure_ascii=rng.random() < 0.5).encode("utf-8", "surrogatepass"), "junk JSON fields"


# ------------------------------------------------------------------------------------------------ junk files
def _png_bomb(width: int, height: int) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


def junk_file(rng: random.Random) -> tuple[str, str, bytes, str]:
    """(filename, content type, bytes, what): junk bytes dressed up as an image (never a decodable picture, so no OCR /
    AI job ever starts from it)."""
    kind = rng.choice(["random", "jpeg_head", "png_head", "gif_head", "svg", "png_bomb", "empty", "text", "html",
                       "zip", "webp_head"])
    n = rng.choice([16, 300, 4_000, 60_000, 250_000])
    noise = rng.randbytes(n)
    data = {"random": noise, "jpeg_head": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + noise,
            "png_head": b"\x89PNG\r\n\x1a\n" + noise, "gif_head": b"GIF89a" + noise[:2000],
            "svg": b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>',
            "png_bomb": _png_bomb(rng.choice([60_000, 100_000]), rng.choice([60_000, 100_000])),
            "empty": b"", "text": "not an image 😀 ‮ gnp.exe".encode(), "html": b"<html><body>junk</body></html>",
            "zip": b"PK\x03\x04" + noise[:5000], "webp_head": b"RIFF\x00\x00\x00\x00WEBPVP8 " + noise[:3000]}[kind]
    name = rng.choice(["photo.jpg", "x.png", "scan.jpeg", "a.gif", "evil.svg", "../../etc/passwd.jpg", "x.php.jpg",
                       "a" * 300 + ".jpg", "émoji😀.png", "", "con.jpg", "noext", "file‮gpj.exe", "photo.JPG ",
                       "x.webp", "id card (1).jpg"])
    ctype = rng.choice(["image/jpeg", "image/png", "image/gif", "image/svg+xml", "application/octet-stream",
                        "text/html", "image/webp", "", "image/jpeg; charset=utf-8"])
    return name, ctype, data, f"{kind} {len(data)} B as {ctype or 'no type'}"


def multipart(fields: list[tuple[str, Any]], rng: random.Random | None = None) -> tuple[bytes, str]:
    """multipart/form-data with text fields and (name, (filename, ctype, bytes)) file parts; byte-exact junk."""
    boundary = "----JunkBoundary" + "".join((rng or random).choice(string.ascii_letters) for _ in range(16))
    out: list[bytes] = []
    for name, val in fields:
        safe_name = str(name).replace('"', "%22").replace("\r", "").replace("\n", "")
        if isinstance(val, tuple):
            fname, ctype, data = val
            fname = str(fname).replace('"', "%22").replace("\r", "").replace("\n", "")
            head = f'--{boundary}\r\nContent-Disposition: form-data; name="{safe_name}"; filename="{fname}"\r\n'
            if ctype:
                head += f"Content-Type: {ctype}\r\n"
            out.append(head.encode("utf-8", "surrogatepass") + b"\r\n" + data + b"\r\n")
        else:
            out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{safe_name}"\r\n\r\n'.encode()
                       + str(val).encode("utf-8", "surrogatepass") + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def urlencode_pairs(pairs: list[tuple[str, Any]]) -> str:
    return "&".join(f"{qenc(str(k))}={qenc(str(v))}" for k, v in pairs)


# ------------------------------------------------------------------------------------------------ forms of a page
class Form:
    __slots__ = ("action", "method", "enctype", "fields", "files", "selects", "checks", "maxlen")

    def __init__(self, action: str, method: str, enctype: str) -> None:
        self.action, self.method, self.enctype = action, method, enctype
        self.fields: list[tuple[str, str]] = []  # (name, value) in document order (hidden, text, textarea ...)
        self.files: list[str] = []
        self.selects: dict[str, list[str]] = {}
        self.checks: list[tuple[str, str, bool]] = []  # checkbox / radio: (name, value, checked)
        self.maxlen: dict[str, int] = {}

    def field(self, name: str) -> str | None:
        return next((v for k, v in self.fields if k == name), None)


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.cur: Form | None = None
        self.select: str | None = None
        self.textarea: str | None = None
        self.text_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self.cur = Form(a.get("action", ""), (a.get("method") or "get").lower(), a.get("enctype", "").lower())
            self.forms.append(self.cur)
            return
        f = self.cur
        if f is not None and tag == "option" and self.select:
            f.selects[self.select].append(a.get("value", ""))
            return
        name = a.get("name")
        if f is None or not name:
            return
        if a.get("maxlength", "").isdigit():
            f.maxlen[name] = int(a["maxlength"])
        if tag == "input":
            kind = a.get("type", "text").lower()
            if kind == "file":
                f.files.append(name)
            elif kind in ("checkbox", "radio"):
                f.checks.append((name, a.get("value", "on"), "checked" in a))
            elif kind not in ("submit", "button", "image", "reset"):
                f.fields.append((name, a.get("value", "")))
        elif tag == "select":
            self.select = name
            f.selects.setdefault(name, [])
        elif tag == "textarea":
            self.textarea = name
            self.text_buf = []
        elif tag == "button" and a.get("type", "submit") == "submit" and name:
            f.fields.append((name, a.get("value", "")))

    def handle_data(self, data: str) -> None:
        if self.textarea is not None:
            self.text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.cur = None
        elif tag == "select":
            self.select = None
        elif tag == "textarea" and self.textarea is not None and self.cur is not None:
            self.cur.fields.append((self.textarea, "".join(self.text_buf)))
            self.textarea = None


def parse_forms(html: str) -> list[Form]:
    p = _Forms()
    try:
        p.feed(html or "")
    except Exception:  # noqa: BLE001 - malformed markup: whatever was parsed
        pass
    return p.forms


def same_origin_path(action: str, page_url: str) -> str | None:
    """The path (+ query) a form posts to, or None for another origin."""
    if not action:
        return page_url
    parts = urlsplit(action)
    if parts.scheme or parts.netloc:
        if parts.hostname not in ("127.0.0.1", "localhost"):
            return None
        return parts.path + (f"?{parts.query}" if parts.query else "")
    if not action.startswith("/"):
        return None
    return action
