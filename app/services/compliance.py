"""Country-specific compliance rules for physical gym branches (WP-B).

* ``requirements_for(country)`` -> :class:`CountryRules` (required documents, registration-number label,
  authority names). Unknown countries fall back to ``DEFAULT``.
* ``validate_registration_number(country, value)`` -> ``(ok, message)`` with real format/checksum rules:
  IN GSTIN (state code + PAN + checksum character), US EIN (valid IRS prefix), GB Companies House,
  AE trade licence, AU ABN (mod-89), CA BN, SG UEN, DE HRB/HRA, FR SIRET (Luhn), ZA CIPC, NZ NZBN (GS1),
  SA CR.
* ``missing_requirements(country, doc_types)`` -> requirement groups not yet satisfied by uploads.

Pure functions only (no I/O) so they are cheap to call from templates, forms and the verification job.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from app.models.enums import BranchDocType

Validator = Callable[[str], tuple[bool, str]]

# ------------------------------------------------------------------ labels
DOC_TYPE_LABELS: dict[BranchDocType, str] = {
    BranchDocType.BUSINESS_REGISTRATION: "Business registration",
    BranchDocType.TAX_REGISTRATION: "Tax registration",
    BranchDocType.TRADE_LICENSE: "Trade licence",
    BranchDocType.FIRE_SAFETY_CERTIFICATE: "Fire safety certificate",
    BranchDocType.SECURITY_CERTIFICATE: "Security certificate",
    BranchDocType.OCCUPANCY_CERTIFICATE: "Occupancy certificate",
    BranchDocType.LIABILITY_INSURANCE: "Liability insurance",
    BranchDocType.HEALTH_SAFETY_CERTIFICATE: "Health & safety certificate",
    BranchDocType.OTHER: "Other document",
}

COUNTRY_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates", "AT": "Austria", "AU": "Australia", "BD": "Bangladesh", "BE": "Belgium",
    "BH": "Bahrain", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CN": "China", "DE": "Germany",
    "DK": "Denmark", "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "ID": "Indonesia", "IE": "Ireland", "IN": "India", "IT": "Italy", "JP": "Japan", "KE": "Kenya",
    "KR": "South Korea", "KW": "Kuwait", "LK": "Sri Lanka", "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria",
    "NL": "Netherlands", "NO": "Norway", "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman", "PH": "Philippines",
    "PK": "Pakistan", "PL": "Poland", "PT": "Portugal", "QA": "Qatar", "SA": "Saudi Arabia", "SE": "Sweden",
    "SG": "Singapore", "TH": "Thailand", "TR": "Turkey", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}

# Sensible default time zone per country (owners can change it; multi-zone countries get a longitude guess).
COUNTRY_TIMEZONES: dict[str, str] = {
    "IN": "Asia/Kolkata", "US": "America/New_York", "GB": "Europe/London", "AE": "Asia/Dubai",
    "AU": "Australia/Sydney", "CA": "America/Toronto", "SG": "Asia/Singapore", "DE": "Europe/Berlin",
    "FR": "Europe/Paris", "ZA": "Africa/Johannesburg", "NZ": "Pacific/Auckland", "SA": "Asia/Riyadh",
    "IT": "Europe/Rome", "ES": "Europe/Madrid", "NL": "Europe/Amsterdam", "IE": "Europe/Dublin",
    "PT": "Europe/Lisbon", "BE": "Europe/Brussels", "AT": "Europe/Vienna", "FI": "Europe/Helsinki",
    "CH": "Europe/Zurich", "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "DK": "Europe/Copenhagen",
    "PL": "Europe/Warsaw", "QA": "Asia/Qatar", "KW": "Asia/Kuwait", "OM": "Asia/Muscat", "BH": "Asia/Bahrain",
    "PK": "Asia/Karachi", "BD": "Asia/Dhaka", "LK": "Asia/Colombo", "NP": "Asia/Kathmandu",
    "MY": "Asia/Kuala_Lumpur", "PH": "Asia/Manila", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "ID": "Asia/Jakarta", "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "CN": "Asia/Shanghai", "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City", "KE": "Africa/Nairobi", "NG": "Africa/Lagos", "EG": "Africa/Cairo",
    "TR": "Europe/Istanbul",
}


# Map centre + zoom used when a branch has no pin yet.
COUNTRY_CENTROIDS: dict[str, tuple[float, float, int]] = {
    "IN": (21.0, 78.5, 5), "US": (39.5, -98.35, 4), "GB": (54.0, -2.5, 5), "AE": (24.4, 54.4, 7),
    "AU": (-25.3, 133.8, 4), "CA": (56.1, -106.3, 3), "SG": (1.352, 103.82, 11), "DE": (51.17, 10.45, 5),
    "FR": (46.6, 2.2, 5), "ZA": (-29.0, 24.7, 5), "NZ": (-41.3, 174.8, 5), "SA": (23.9, 45.1, 5),
}


def map_center(country_code: str | None) -> list[float]:
    lat, lon, zoom = COUNTRY_CENTROIDS.get((country_code or "").upper(), (20.0, 10.0, 2))
    return [lat, lon, zoom]


def country_name(code: str | None) -> str:
    code = (code or "").upper()
    return COUNTRY_NAMES.get(code, code or "Unknown country")


def guess_timezone(country_code: str | None, lon: float | None = None) -> str | None:
    """Best-effort IANA time zone for a country (longitude refines the multi-zone ones)."""
    cc = (country_code or "").upper()
    if lon is not None:
        if cc == "US":
            return ("America/New_York" if lon > -87 else "America/Chicago" if lon > -101
                    else "America/Denver" if lon > -115 else "America/Los_Angeles" if lon > -140 else "Pacific/Honolulu")
        if cc == "CA":
            return ("America/Halifax" if lon > -64 else "America/Toronto" if lon > -90 else "America/Winnipeg"
                    if lon > -102 else "America/Edmonton" if lon > -120 else "America/Vancouver")
        if cc == "AU":
            return "Australia/Perth" if lon < 129 else "Australia/Adelaide" if lon < 141 else "Australia/Sydney"
    return COUNTRY_TIMEZONES.get(cc)


def doc_type_label(value: BranchDocType | str) -> str:
    try:
        return DOC_TYPE_LABELS[BranchDocType(value)]
    except ValueError:
        return str(value).replace("_", " ").capitalize()


# ------------------------------------------------------------------ validators
_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _compact(value: str) -> str:
    return re.sub(r"[\s\-./]", "", (value or "").upper())


def gstin_check_char(first14: str) -> str:
    """GSTIN checksum character (GSTN algorithm: base-36 code points, factors 1/2 alternating)."""
    total = 0
    for i, ch in enumerate(first14):
        product = _ALNUM.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _ALNUM[(36 - total % 36) % 36]


_GSTIN_RE = re.compile(r"^(0[1-9]|[12][0-9]|3[0-8]|97|99)[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][A-Z][0-9A-Z]$")


def validate_gstin(value: str) -> tuple[bool, str]:
    v = _compact(value)
    if len(v) != 15:
        return False, "A GSTIN has exactly 15 characters (e.g. 27AAPFU0939F1ZV)."
    if not _GSTIN_RE.match(v):
        return False, "That is not a valid GSTIN: 2-digit state code + 10-character PAN + entity code + Z + check character."
    if gstin_check_char(v[:14]) != v[14]:
        return False, "The GSTIN checksum character does not match - please re-check the number."
    return True, "Valid GSTIN."


_EIN_BAD_PREFIXES = {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70", "78", "79", "89", "96", "97"}


def validate_ein(value: str) -> tuple[bool, str]:
    v = (value or "").strip()
    if not re.fullmatch(r"\d{2}-?\d{7}", v):
        return False, "An EIN looks like NN-NNNNNNN (9 digits)."
    if v[:2] in _EIN_BAD_PREFIXES:
        return False, f"EINs never start with {v[:2]} - please re-check the number."
    return True, "Valid EIN."


def validate_gb_company(value: str) -> tuple[bool, str]:
    v = _compact(value)
    if v.isdigit() and 6 <= len(v) <= 8:
        v = v.zfill(8)
    if re.fullmatch(r"\d{8}|[A-Z]{2}\d{6}|R\d{7}", v):
        return True, "Valid Companies House number."
    return False, "A Companies House number has 8 characters, e.g. 01234567 or SC123456."


def validate_ae_trade_licence(value: str) -> tuple[bool, str]:
    v = _compact(value)
    if re.fullmatch(r"[A-Z0-9]{4,20}", v) and re.search(r"\d", v):
        return True, "Valid trade licence number."
    return False, "A UAE trade licence number has 4-20 letters/digits (at least one digit)."


_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


def validate_abn(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", value or "")
    if not re.fullmatch(r"\d{11}", v):
        return False, "An ABN has 11 digits (e.g. 51 824 753 556)."
    digits = [int(c) for c in v]
    digits[0] -= 1
    if sum(d * w for d, w in zip(digits, _ABN_WEIGHTS, strict=True)) % 89 != 0:
        return False, "The ABN checksum is invalid - please re-check the number."
    return True, "Valid ABN."


def validate_ca_bn(value: str) -> tuple[bool, str]:
    v = _compact(value)
    if re.fullmatch(r"\d{9}([A-Z]{2}\d{4})?", v):
        return True, "Valid Business Number."
    return False, "A CRA Business Number has 9 digits (optionally followed by a program account like RC0001)."


def validate_sg_uen(value: str) -> tuple[bool, str]:
    v = _compact(value)
    if re.fullmatch(r"\d{8}[A-Z]|(18|19|20)\d{7}[A-Z]|[TSR]\d{2}[A-Z]{2}\d{4}[A-Z]", v):
        return True, "Valid UEN."
    return False, "A UEN looks like 53312345X, 201912345K or T09LL0001B."


def validate_de_hr(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s+", " ", (value or "").upper()).strip()
    if re.fullmatch(r"HR[AB] ?\d{1,6}( ?[A-Z]{1,2})?", v):
        return True, "Valid Handelsregister number."
    return False, "Enter the commercial register number, e.g. HRB 123456 or HRA 12345."


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def validate_siret(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", value or "")
    if not re.fullmatch(r"\d{14}", v):
        return False, "A SIRET has 14 digits (SIREN + NIC)."
    if v.startswith("356000000"):  # La Poste establishments use a sum-of-digits rule
        ok = sum(int(c) for c in v) % 5 == 0
    else:
        ok = _luhn_ok(v)
    return (True, "Valid SIRET.") if ok else (False, "The SIRET checksum (Luhn) is invalid - please re-check the number.")


def validate_za_cipc(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", value or "")
    m = re.fullmatch(r"(\d{4})/(\d{6})/(\d{2})", v)
    if not m:
        return False, "A CIPC registration number looks like YYYY/NNNNNN/NN (e.g. 2015/123456/07)."
    year = int(m.group(1))
    if not 1900 <= year <= date.today().year:
        return False, "The registration year in the CIPC number is not plausible."
    return True, "Valid CIPC registration number."


def validate_nzbn(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", value or "")
    if not re.fullmatch(r"\d{13}", v):
        return False, "An NZBN has 13 digits (starting with 94)."
    if not v.startswith("94"):
        return False, "New Zealand Business Numbers start with 94."
    weighted = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(v[:12]))
    if (10 - weighted % 10) % 10 != int(v[12]):
        return False, "The NZBN check digit is invalid - please re-check the number."
    return True, "Valid NZBN."


def validate_sa_cr(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", value or "")
    if re.fullmatch(r"\d{10}", v):
        return True, "Valid Commercial Registration number."
    return False, "A Saudi Commercial Registration (CR) number has 10 digits."


def validate_generic(value: str) -> tuple[bool, str]:
    v = (value or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9][A-Z0-9\-/. ]{2,38}[A-Z0-9]", v) and re.search(r"\d", v):
        return True, "Registration number format accepted."
    return False, "Enter the official registration number (4-40 letters/digits, at least one digit)."


# ------------------------------------------------------------------ rules
@dataclass(frozen=True, slots=True)
class DocRequirement:
    key: str
    label: str
    any_of: tuple[BranchDocType, ...]
    authority: str
    help: str = ""

    def satisfied_by(self, doc_types: Iterable[BranchDocType | str]) -> bool:
        have = {BranchDocType(d) for d in doc_types}
        return any(t in have for t in self.any_of)

    @property
    def accepted_labels(self) -> list[str]:
        return [DOC_TYPE_LABELS[t] for t in self.any_of]


@dataclass(frozen=True, slots=True)
class CountryRules:
    code: str
    name: str
    registration_label: str
    registration_example: str
    registration_authority: str
    validator: Validator
    requirements: tuple[DocRequirement, ...] = field(default_factory=tuple)

    @property
    def required_doc_types(self) -> set[BranchDocType]:
        return {t for r in self.requirements for t in r.any_of}


_B = BranchDocType

COUNTRY_RULES: dict[str, CountryRules] = {
    "IN": CountryRules(
        "IN", "India", "GSTIN", "27AAPFU0939F1ZV", "GST Network (GSTN) / State Labour Department", validate_gstin,
        (
            DocRequirement("registration", "GST registration or Shops & Establishments certificate",
                           (_B.TAX_REGISTRATION, _B.BUSINESS_REGISTRATION), "GSTN / State Labour Department",
                           "GST REG-06 certificate or the Shops & Establishments registration of this branch."),
            DocRequirement("fire", "Fire NOC", (_B.FIRE_SAFETY_CERTIFICATE,), "State Fire & Emergency Services",
                           "No-objection certificate from the fire department for the premises."),
            DocRequirement("security", "Police / security clearance or public liability insurance",
                           (_B.SECURITY_CERTIFICATE, _B.LIABILITY_INSURANCE), "Local police / insurer"),
        ),
    ),
    "US": CountryRules(
        "US", "United States", "EIN", "12-3456789", "Internal Revenue Service (IRS) / Secretary of State", validate_ein,
        (
            DocRequirement("registration", "State business registration or IRS EIN letter (CP 575)",
                           (_B.BUSINESS_REGISTRATION, _B.TAX_REGISTRATION), "Secretary of State / IRS"),
            DocRequirement("fire", "Fire inspection certificate or Certificate of Occupancy",
                           (_B.FIRE_SAFETY_CERTIFICATE, _B.OCCUPANCY_CERTIFICATE), "Local fire marshal / building department"),
            DocRequirement("insurance", "General liability insurance (ACORD 25 certificate)",
                           (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "GB": CountryRules(
        "GB", "United Kingdom", "Companies House number", "01234567", "Companies House", validate_gb_company,
        (
            DocRequirement("registration", "Certificate of incorporation", (_B.BUSINESS_REGISTRATION,), "Companies House"),
            DocRequirement("fire", "Fire risk assessment", (_B.FIRE_SAFETY_CERTIFICATE,),
                           "Regulatory Reform (Fire Safety) Order 2005"),
            DocRequirement("insurance", "Public liability insurance", (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "AE": CountryRules(
        "AE", "United Arab Emirates", "Trade licence number", "CN-1234567", "Department of Economy and Tourism (DET)",
        validate_ae_trade_licence,
        (
            DocRequirement("registration", "Trade licence", (_B.TRADE_LICENSE, _B.BUSINESS_REGISTRATION), "DET / DED"),
            DocRequirement("fire", "Civil Defence safety certificate", (_B.FIRE_SAFETY_CERTIFICATE,), "Civil Defence"),
            DocRequirement("security", "Security approval or liability insurance",
                           (_B.SECURITY_CERTIFICATE, _B.LIABILITY_INSURANCE), "SIRA / licensed insurer"),
        ),
    ),
    "AU": CountryRules(
        "AU", "Australia", "ABN", "51 824 753 556", "Australian Business Register (ABR)", validate_abn,
        (
            DocRequirement("registration", "ABN registration or ASIC certificate",
                           (_B.BUSINESS_REGISTRATION, _B.TAX_REGISTRATION), "ABR / ASIC"),
            DocRequirement("fire", "Annual fire safety statement", (_B.FIRE_SAFETY_CERTIFICATE,), "Local council"),
            DocRequirement("insurance", "Public liability insurance", (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "CA": CountryRules(
        "CA", "Canada", "Business Number (BN)", "123456789", "Canada Revenue Agency (CRA)", validate_ca_bn,
        (
            DocRequirement("registration", "Business registration or CRA BN confirmation",
                           (_B.BUSINESS_REGISTRATION, _B.TAX_REGISTRATION), "Provincial registry / CRA"),
            DocRequirement("fire", "Fire inspection or occupancy permit",
                           (_B.FIRE_SAFETY_CERTIFICATE, _B.OCCUPANCY_CERTIFICATE), "Municipal fire services"),
            DocRequirement("insurance", "Commercial general liability insurance", (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "SG": CountryRules(
        "SG", "Singapore", "UEN", "201912345K", "Accounting and Corporate Regulatory Authority (ACRA)", validate_sg_uen,
        (
            DocRequirement("registration", "ACRA business profile", (_B.BUSINESS_REGISTRATION,), "ACRA"),
            DocRequirement("fire", "SCDF fire safety certificate", (_B.FIRE_SAFETY_CERTIFICATE,), "Singapore Civil Defence Force"),
            DocRequirement("insurance", "Public liability insurance", (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "DE": CountryRules(
        "DE", "Germany", "Handelsregister number", "HRB 123456", "Amtsgericht (Handelsregister)", validate_de_hr,
        (
            DocRequirement("registration", "Handelsregisterauszug or Gewerbeanmeldung",
                           (_B.BUSINESS_REGISTRATION, _B.TRADE_LICENSE), "Amtsgericht / Gewerbeamt"),
            DocRequirement("fire", "Brandschutznachweis (fire protection certificate)", (_B.FIRE_SAFETY_CERTIFICATE,),
                           "Bauaufsichtsbehörde"),
            DocRequirement("insurance", "Betriebshaftpflichtversicherung (business liability insurance)",
                           (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "FR": CountryRules(
        "FR", "France", "SIRET", "73282932000074", "INSEE / Registre du commerce et des sociétés", validate_siret,
        (
            DocRequirement("registration", "Extrait Kbis", (_B.BUSINESS_REGISTRATION,), "Greffe du tribunal de commerce"),
            DocRequirement("fire", "ERP safety approval (commission de sécurité)",
                           (_B.FIRE_SAFETY_CERTIFICATE, _B.OCCUPANCY_CERTIFICATE), "Commission de sécurité"),
            DocRequirement("insurance", "Responsabilité civile professionnelle insurance", (_B.LIABILITY_INSURANCE,),
                           "Licensed insurer"),
        ),
    ),
    "ZA": CountryRules(
        "ZA", "South Africa", "CIPC registration number", "2015/123456/07",
        "Companies and Intellectual Property Commission (CIPC)", validate_za_cipc,
        (
            DocRequirement("registration", "CIPC registration certificate (CoR 14.3)", (_B.BUSINESS_REGISTRATION,), "CIPC"),
            DocRequirement("fire", "Municipal fire compliance certificate", (_B.FIRE_SAFETY_CERTIFICATE,), "Municipal fire department"),
            DocRequirement("safety", "OHS compliance or public liability insurance",
                           (_B.HEALTH_SAFETY_CERTIFICATE, _B.LIABILITY_INSURANCE), "Department of Employment and Labour / insurer"),
        ),
    ),
    "NZ": CountryRules(
        "NZ", "New Zealand", "NZBN", "9429041925676", "New Zealand Companies Office", validate_nzbn,
        (
            DocRequirement("registration", "NZBN / Companies Office extract", (_B.BUSINESS_REGISTRATION,), "Companies Office"),
            DocRequirement("fire", "Building warrant of fitness or fire evacuation scheme",
                           (_B.FIRE_SAFETY_CERTIFICATE, _B.OCCUPANCY_CERTIFICATE), "Fire and Emergency New Zealand"),
            DocRequirement("insurance", "Public liability insurance", (_B.LIABILITY_INSURANCE,), "Licensed insurer"),
        ),
    ),
    "SA": CountryRules(
        "SA", "Saudi Arabia", "Commercial Registration (CR) number", "1010123456", "Ministry of Commerce", validate_sa_cr,
        (
            DocRequirement("registration", "Commercial Registration certificate", (_B.BUSINESS_REGISTRATION,), "Ministry of Commerce"),
            DocRequirement("fire", "Civil Defense safety licence", (_B.FIRE_SAFETY_CERTIFICATE,), "General Directorate of Civil Defense"),
            DocRequirement("municipal", "Baladiya municipal licence or security certificate",
                           (_B.TRADE_LICENSE, _B.SECURITY_CERTIFICATE), "Municipality / security authority"),
        ),
    ),
}

DEFAULT = CountryRules(
    "XX", "Other countries", "Business registration number", "REG-123456", "National business registry", validate_generic,
    (
        DocRequirement("registration", "Business registration or trade licence",
                       (_B.BUSINESS_REGISTRATION, _B.TRADE_LICENSE, _B.TAX_REGISTRATION), "National business registry"),
        DocRequirement("fire", "Fire or building safety certificate",
                       (_B.FIRE_SAFETY_CERTIFICATE, _B.OCCUPANCY_CERTIFICATE, _B.HEALTH_SAFETY_CERTIFICATE),
                       "Local fire / building authority"),
        DocRequirement("insurance", "Liability insurance or security certificate",
                       (_B.LIABILITY_INSURANCE, _B.SECURITY_CERTIFICATE), "Licensed insurer / security authority"),
    ),
)


def requirements_for(country: str | None) -> CountryRules:
    return COUNTRY_RULES.get((country or "").upper(), DEFAULT)


def validate_registration_number(country: str | None, value: str | None) -> tuple[bool, str]:
    value = (value or "").strip()
    rules = requirements_for(country)
    if not value:
        return False, f"Enter the {rules.registration_label}."
    if len(value) > 80:
        return False, "That registration number is too long."
    return rules.validator(value)


def canonical_registration(country: str | None, value: str | None) -> str:
    """Display/storage form: trimmed, upper-case, single spaces (EIN keeps its hyphen)."""
    v = re.sub(r"\s+", " ", (value or "").strip().upper())
    cc = (country or "").upper()
    if cc == "US" and re.fullmatch(r"\d{9}", v):
        return f"{v[:2]}-{v[2:]}"
    if cc in ("IN", "GB", "SG", "FR", "NZ", "SA", "CA"):
        v = v.replace(" ", "")
        if cc == "GB" and v.isdigit() and 6 <= len(v) < 8:
            v = v.zfill(8)
    return v


def registration_key(value: str | None) -> str:
    """Alphanumeric-only form used to find the number inside OCR text."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def missing_requirements(country: str | None, doc_types: Iterable[BranchDocType | str]) -> list[DocRequirement]:
    have = list(doc_types)
    return [r for r in requirements_for(country).requirements if not r.satisfied_by(have)]


def requirement_status(country: str | None, doc_types: Iterable[BranchDocType | str]) -> list[tuple[DocRequirement, bool]]:
    have = list(doc_types)
    return [(r, r.satisfied_by(have)) for r in requirements_for(country).requirements]


def country_options() -> list[tuple[str, str]]:
    return sorted(COUNTRY_NAMES.items(), key=lambda kv: kv[1])


def doc_type_options() -> list[tuple[str, str]]:
    return [(t.value, DOC_TYPE_LABELS[t]) for t in BranchDocType]


def registration_labels() -> dict[str, dict[str, str]]:
    """Per-country registration label/example (the branch form updates its label when the country changes)."""
    out = {cc: {"label": r.registration_label, "example": r.registration_example, "authority": r.registration_authority}
           for cc, r in COUNTRY_RULES.items()}
    out["*"] = {"label": DEFAULT.registration_label, "example": DEFAULT.registration_example,
                "authority": DEFAULT.registration_authority}
    return out
