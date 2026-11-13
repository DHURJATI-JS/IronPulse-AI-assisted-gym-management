"""Signature-based request threat scanner.

Each finding has a category and severity (1 noise .. 5 critical). Severity depends on *where* the
pattern appears: attack syntax in the URL path/query is far more suspicious than the same text in a
chat message body. The gate turns accumulated severities into blocks and bans (the per-address threat score is shared
by every worker through Redis: app/security/bans.py); the AI triage job (app/services/security_triage.py) reviews the
logged events afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

from app.models.enums import ThreatCategory


@dataclass(frozen=True, slots=True)
class Finding:
    category: ThreatCategory
    severity: int
    detail: str


@dataclass(frozen=True, slots=True)
class Signature:
    category: ThreatCategory
    pattern: re.Pattern[str]
    url_severity: int
    body_severity: int
    label: str


def _sig(cat: ThreatCategory, pattern: str, url_sev: int, body_sev: int, label: str) -> Signature:
    return Signature(cat, re.compile(pattern, re.IGNORECASE), url_sev, body_sev, label)


SIGNATURES: tuple[Signature, ...] = (
    # --- SQL injection
    _sig(ThreatCategory.SQLI, r"\bunion\b(\s|/\*.*?\*/)+(all\s+)?select\b", 4, 3, "UNION SELECT"),
    _sig(ThreatCategory.SQLI, r"['\"`]\s*(or|and)\s+['\"`]?\w+['\"`]?\s*(=|like)\s*['\"`]?\w+", 3, 2, "quote OR/AND tautology"),
    _sig(ThreatCategory.SQLI, r"\b(or|and)\s+\d+\s*=\s*\d+\s*(--|#|/\*)", 4, 2, "numeric tautology + comment"),
    _sig(ThreatCategory.SQLI, r"\b(pg_sleep|sleep|benchmark|waitfor\s+delay)\s*\(", 4, 2, "time-based injection"),
    _sig(ThreatCategory.SQLI, r";\s*(drop|truncate|alter|create)\s+(table|database|schema)\b", 4, 2, "stacked DDL"),
    _sig(ThreatCategory.SQLI, r"\binformation_schema\b|\bpg_catalog\b|\bsysobjects\b", 3, 2, "schema enumeration"),
    _sig(ThreatCategory.SQLI, r"\bselect\b.+\bfrom\b.+\b(where|limit)\b", 2, 1, "SQL statement"),
    # --- XSS
    _sig(ThreatCategory.XSS, r"<\s*script\b", 3, 2, "<script> tag"),
    _sig(ThreatCategory.XSS, r"javascript\s*:", 3, 1, "javascript: URL"),
    _sig(ThreatCategory.XSS, r"<[^>]+\bon(error|load|mouseover|focus|click|toggle|animationstart)\s*=", 3, 2, "inline event handler"),
    _sig(ThreatCategory.XSS, r"<\s*(iframe|object|embed|svg|math)\b", 2, 1, "active HTML element"),
    _sig(ThreatCategory.XSS, r"document\.(cookie|domain)|window\.location\s*=", 3, 1, "DOM access"),
    # --- template / expression injection
    _sig(ThreatCategory.COMMAND_INJECTION, r"\$\{\s*jndi\s*:", 5, 4, "Log4Shell JNDI"),
    _sig(ThreatCategory.COMMAND_INJECTION, r"\{\{\s*\d+\s*\*\s*\d+\s*\}\}|\{\{.*__class__", 4, 2, "template injection"),
    # --- command injection
    _sig(ThreatCategory.COMMAND_INJECTION, r"(;|\|\||&&|\|)\s*(cat|ls|id|whoami|uname|wget|curl|nc|ncat|bash|sh|zsh|powershell|cmd(\.exe)?|python[23]?|perl)\b", 4, 2, "shell command chaining"),
    _sig(ThreatCategory.COMMAND_INJECTION, r"\$\((\s*)(cat|id|whoami|curl|wget|sleep)\b", 4, 2, "command substitution"),
    # --- path traversal / sensitive files
    _sig(ThreatCategory.TRAVERSAL, r"(\.\.[/\\]){1,}", 4, 2, "directory traversal"),
    _sig(ThreatCategory.TRAVERSAL, r"/etc/(passwd|shadow|hosts)|c:\\windows\\|boot\.ini|win\.ini", 5, 3, "system file access"),
    _sig(ThreatCategory.TRAVERSAL, r"\x00|%00", 4, 2, "null byte"),
)

PROBE_PATHS = re.compile(
    r"^/(\.env|\.git|\.svn|\.hg|\.DS_Store|\.aws|\.ssh|wp-admin|wp-login\.php|wp-content|wp-includes|xmlrpc\.php|"
    r"phpmyadmin|pma|myadmin|admin\.php|config\.php|setup\.php|install\.php|server-status|server-info|actuator|"
    r"cgi-bin|vendor/phpunit|boaform|HNAP1|owa|ecp|autodiscover|solr|jenkins|manager/html|console|\.well-known/security\.txt\.bak|"
    r"backup|db\.sql|dump\.sql|database\.sql|.*\.(php|asp|aspx|jsp|cgi|bak|old|sql|swp)$)",
    re.IGNORECASE,
)

SCANNER_AGENTS = re.compile(
    r"sqlmap|nikto|nmap|masscan|zgrab|acunetix|nessus|openvas|wpscan|dirbuster|gobuster|ffuf|feroxbuster|"
    r"nuclei|hydra|jaeles|whatweb|arachni|w3af|skipfish|havij|fimap|commix|netsparker|appscan|zap/|"
    r"burpsuite|python-requests/0\.|libwww-perl|morfeus|zmeu",
    re.IGNORECASE,
)


def _decode(value: str) -> str:
    # decode twice to catch double-encoded payloads (%252e%252e)
    once = unquote_plus(value)
    return unquote_plus(once)


def scan_text(text: str, *, location: str) -> list[Finding]:
    """location: 'path' | 'query' | 'header' | 'body'"""
    if not text:
        return []
    decoded = _decode(text) if location in ("path", "query") else text
    findings: list[Finding] = []
    for sig in SIGNATURES:
        if sig.pattern.search(decoded):
            sev = sig.url_severity if location in ("path", "query") else sig.body_severity
            if location == "header":
                sev = max(1, sig.url_severity - 1)
            findings.append(Finding(sig.category, sev, f"{sig.label} in {location}"))
    return findings


def scan_url(path: str, query: str) -> list[Finding]:
    """Findings in what the request asks for (probe paths, attack syntax in the path / query) - which any web page, e-mail
    or chat link can make a visitor's browser send (app/security/net.py ``attributable``)."""
    findings: list[Finding] = []
    if PROBE_PATHS.match(path):
        findings.append(Finding(ThreatCategory.PROBE, 3, f"probe for {path[:80]}"))
    findings += scan_text(path, location="path")
    findings += scan_text(query, location="query")
    return findings


def scan_agent(method: str, user_agent: str | None) -> list[Finding]:
    """Findings in what the client says it is (its User-Agent) - a web page can't set that for a visitor's browser."""
    findings: list[Finding] = []
    if user_agent and SCANNER_AGENTS.search(user_agent):
        findings.append(Finding(ThreatCategory.SCANNER, 4, f"scanner user-agent {user_agent[:60]}"))
    if not user_agent and method != "OPTIONS":
        findings.append(Finding(ThreatCategory.SCANNER, 1, "missing user-agent"))
    if user_agent:
        findings += [f for f in scan_text(user_agent, location="header") if f.severity >= 3]
    return findings


def scan_request(method: str, path: str, query: str, user_agent: str | None) -> list[Finding]:
    findings: list[Finding] = []
    if PROBE_PATHS.match(path):
        findings.append(Finding(ThreatCategory.PROBE, 3, f"probe for {path[:80]}"))
    if user_agent and SCANNER_AGENTS.search(user_agent):
        findings.append(Finding(ThreatCategory.SCANNER, 4, f"scanner user-agent {user_agent[:60]}"))
    if not user_agent and method != "OPTIONS":
        findings.append(Finding(ThreatCategory.SCANNER, 1, "missing user-agent"))
    findings += scan_text(path, location="path")
    findings += scan_text(query, location="query")
    if user_agent:
        findings += [f for f in scan_text(user_agent, location="header") if f.severity >= 3]
    return findings


def scan_body(body: bytes) -> list[Finding]:
    try:
        text = body.decode("utf-8", errors="ignore")
    except Exception:
        return []
    if not text:
        return []
    # urlencoded bodies: decode once so "%3Cscript" is seen as "<script"
    if "=" in text and "&" in text or "%" in text:
        text = unquote_plus(text)
    return scan_text(text, location="body")
