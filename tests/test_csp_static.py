"""Static guards for the strict Content-Security-Policy: templates must not contain inline event
handlers, inline style attributes, or <script> tags without a nonce. (E-mail templates are exempt:
mail clients need inline styles and are not served under our CSP.)"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"
JS_SRC = ROOT / "app" / "static" / "src" / "js"

STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*["']""", re.IGNORECASE)
HANDLER_ATTR = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.IGNORECASE)


def _templates():
    for f in sorted(TEMPLATES.rglob("*.html")):
        if "emails" in f.relative_to(TEMPLATES).parts:
            continue
        yield f


_COMMENTS = re.compile(r"\{#.*?#\}|<!--.*?-->", re.DOTALL)


def _code(text: str) -> str:
    """Template source with Jinja/HTML comments blanked (line numbers preserved)."""
    return _COMMENTS.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def test_no_inline_styles_or_event_handlers():
    problems = []
    for f in _templates():
        for no, line in enumerate(_code(f.read_text(encoding="utf-8")).splitlines(), 1):
            if STYLE_ATTR.search(line):
                problems.append(f"{f.relative_to(ROOT)}:{no}: inline style attribute")
            if HANDLER_ATTR.search(line):
                problems.append(f"{f.relative_to(ROOT)}:{no}: inline event handler")
    assert not problems, "CSP violations:\n" + "\n".join(problems)


def test_every_script_tag_has_a_nonce():
    problems = []
    for f in _templates():
        text = _code(f.read_text(encoding="utf-8"))
        for m in SCRIPT_TAG.finditer(text):
            if "nonce=" not in m.group(0):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{f.relative_to(ROOT)}:{line}: <script> without nonce")
    assert not problems, "\n".join(problems)


def test_inline_script_bodies_only_in_base_theme_init():
    """Only base.html may contain an inline script body (the nonce'd theme initialiser)."""
    problems = []
    inline = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
    for f in _templates():
        for m in inline.finditer(f.read_text(encoding="utf-8")):
            body = m.group(1).strip()
            if body and f.name != "base.html":
                problems.append(f"{f.relative_to(ROOT)}: inline <script> body")
    assert not problems, "\n".join(problems)


def test_js_never_uses_eval_or_document_write():
    problems = []
    for f in sorted(JS_SRC.glob("*.js")):
        text = f.read_text(encoding="utf-8")
        for token in ("eval(", "new Function(", "document.write("):
            if token in text:
                problems.append(f"{f.name}: uses {token}")
    assert not problems, "\n".join(problems)
