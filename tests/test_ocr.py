"""WP-H: multi-engine OCR service + deterministic ID parsing (Verhoeff, ICAO 9303 MRZ, PAN/EPIC/DL, dates, names)."""

from __future__ import annotations

import io
import time
from datetime import date

import pytest

from app.ai.client import FakeProvider, ai
from app.services import idparse as p
from app.services import ocr

TODAY = date(2026, 11, 10)
TD3 = ("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
       "L898902C36UTO7408122F1204159ZE184226B<<<<<10")
TD1 = ("I<UTOD231458907<<<<<<<<<<<<<<<\n"
       "7408122F1204159UTO<<<<<<<<<<<6\n"
       "ERIKSSON<<ANNA<MARIA<<<<<<<<<<")


@pytest.fixture(autouse=True)
def _no_network_ai():
    """OCR tests never talk to a real AI provider."""
    ai.use_providers([FakeProvider("gateway", fail=True)])
    yield
    ai.use_providers(None)


def aadhaar(prefix: str = "23456789012") -> str:
    return prefix + p.verhoeff_check_digit(prefix)


# ====================================================================== Verhoeff
def test_verhoeff_valid_and_invalid():
    assert p.verhoeff_valid("2363")  # classic textbook example (236 + check digit 3)
    assert not p.verhoeff_valid("2364")
    n = aadhaar()
    assert p.verhoeff_valid(n) and p.aadhaar_valid(n)
    assert p.aadhaar_valid(f"{n[:4]} {n[4:8]} {n[8:]}")
    # every single-digit substitution and every adjacent transposition is detected
    for i in range(12):
        for d in "0123456789":
            if d != n[i]:
                assert not p.verhoeff_valid(n[:i] + d + n[i + 1:])
    for i in range(11):
        if n[i] != n[i + 1]:
            assert not p.verhoeff_valid(n[:i] + n[i + 1] + n[i] + n[i + 2:])
    assert not p.aadhaar_valid("1" + n[1:])  # Aadhaar never starts with 0 or 1
    assert not p.verhoeff_valid("") and not p.verhoeff_valid("12a4")


# ====================================================================== ICAO 9303 MRZ
def test_mrz_check_digits_icao_specimen():
    assert p.mrz_check_digit("L898902C3") == 6
    assert p.mrz_check_digit("740812") == 2
    assert p.mrz_check_digit("120415") == 9
    assert p.mrz_check_digit("ZE184226B<<<<<") == 1
    assert p.mrz_check_digit("D23145890") == 7
    assert p.mrz_check_digit("<<<<<<") == 0


def test_parse_td3_specimen():
    m = p.find_mrz(TD3, TODAY)
    assert m is not None and m.format == "TD3" and m.valid
    assert (m.surname, m.given_names, m.number, m.nationality, m.sex) == ("ERIKSSON", "ANNA MARIA", "L898902C3", "UTO", "F")
    assert m.dob == date(1974, 8, 12) and m.expiry == date(2012, 4, 15)
    assert m.checks == {"number": True, "dob": True, "expiry": True, "personal": True, "composite": True}


def test_parse_td1_specimen():
    m = p.find_mrz(TD1, TODAY)
    assert m is not None and m.format == "TD1" and m.valid
    assert m.number == "D23145890" and m.surname == "ERIKSSON" and m.given_names == "ANNA MARIA"
    assert m.dob == date(1974, 8, 12) and m.expiry == date(2012, 4, 15)


def test_mrz_ocr_confusions_are_repaired():
    noisy = ("P<UTOERIKSSON«ANNA<MARIA<<<<<<KK<<<<<<<<<<<\n"
             "L8989O2C36UTO74O8122F12O4159ZE184226B<<<<<1O")  # O for 0 everywhere, « for <<, K fillers
    m = p.find_mrz(noisy, TODAY)
    assert m is not None and m.valid and m.corrected
    assert m.number == "L898902C3" and m.given_names == "ANNA MARIA"
    spaced = "P < UTO ERIKSSON << ANNA < MARIA <<<<<<<<<<<<<<<<<<<\nL898902C3 6 UTO 740812 2 F 120415 9 ZE184226B <<<<< 1 0"
    assert p.find_mrz(spaced, TODAY).valid


def test_mrz_repair_is_conservative():
    # a check digit misread as a letter ("4" -> "I") must not trigger a "repair" of the number itself
    misread = TD3.replace("L898902C36UTO", "L898902C3IUTO")
    m = p.find_mrz(misread, TODAY)
    assert m is not None and m.checks["number"] is False and m.number == "L898902C3" and not m.corrected
    # digits are never turned into letters (8 -> B would "validate" by coincidence far too often)
    assert p._repair_alnum("J8369854<", "4") == ("J8369854<", False)
    assert p._repair_alnum("J8369854<", "7") is None


def test_consensus_survives_one_engine_misreading_the_check_digit():
    cd = lambda s: str(p.mrz_check_digit(s))  # noqa: E731
    l1 = "P<INDMEHTA<<RAHUL".ljust(44, "<")
    l2 = "J8369854<" + cd("J8369854<") + "IND950517" + cd("950517") + "M320516" + cd("320516") + "<" * 15
    l2 += cd(l2[0:10] + l2[13:20] + l2[21:43])
    good = f"{l1}\n{l2}"
    bad = f"{l1}\n{l2[:9]}I{l2[10:]}"  # what the vision model returned in a live run
    c = p.build_consensus({e: _ex(t, "passport") for e, t in (("tesseract", good), ("apple_vision", good), ("vision_llm", bad))},
                          doc_type="passport")
    assert c.accepted and c.number == "J8369854" and c.checksum == "valid" and len(c.agreeing) == 3


def test_mrz_tampered_digit_fails_checks():
    tampered = TD3.replace("7408122F", "7508122F")  # birth year edited -> DOB + composite check digits fail
    m = p.find_mrz(tampered, TODAY)
    assert m is not None and not m.valid
    assert m.checks["dob"] is False and m.checks["composite"] is False and m.checks["number"] is True


def test_mrz_not_found_in_plain_text():
    assert p.find_mrz("Name: RAHUL MEHTA\nDOB: 17/05/1995") is None


# ====================================================================== formats
def test_pan_regex_and_ocr_confusions():
    ex = p.extract_fields("INCOME TAX DEPARTMENT\nPermanent Account Number\nABCPM1234F", doc_type="pan", today=TODAY)
    assert ex.primary.kind == "pan" and ex.primary.number == "ABCPM1234F"
    ex = p.extract_fields("Permanent Account Number\nABCPM12S4F", doc_type="pan", today=TODAY)  # S read for 5? -> 1254
    assert ex.primary.number == "ABCPM1254F"
    ex = p.extract_fields("ABCXM1234F", doc_type="pan", today=TODAY)  # 4th char X is not a valid holder type
    assert all(c.kind != "pan" for c in ex.candidates)


def test_epic_dl_and_passport_numbers():
    ex = p.extract_fields("ELECTION COMMISSION OF INDIA\nEPIC No: ABC1234567", doc_type="voter_id", today=TODAY)
    assert ex.primary.kind == "epic" and ex.primary.number == "ABC1234567"
    ex = p.extract_fields("DL No: MH12 20110012345\nValid Till (NT): 16-05-2035", doc_type="driving_license", today=TODAY)
    assert ex.primary.kind == "dl_in" and ex.primary.number == "MH1220110012345"
    assert ex.expiry == date(2035, 5, 16)
    ex = p.extract_fields("REPUBLIC OF INDIA PASSPORT\nPassport No. J8369854", doc_type="passport", today=TODAY)
    assert ex.primary.kind == "passport" and ex.primary.number == "J8369854"


def test_aadhaar_card_extraction_and_vid_is_ignored():
    n = aadhaar()
    vid = "9134 5678 9012 3456"
    text = f"GOVERNMENT OF INDIA\nRahul Mehta\nDOB: 17/05/1995\nMALE\n{n[:4]} {n[4:8]} {n[8:]}\nVID : {vid}"
    ex = p.extract_fields(text, doc_type="aadhaar", today=TODAY)
    assert ex.primary.kind == "aadhaar" and ex.primary.number == n and ex.primary.checksum == "valid"
    assert ex.name == "RAHUL MEHTA" and ex.name_source == "heuristic"
    assert ex.dob == date(1995, 5, 17)
    assert all(c.number != "567890123456" for c in ex.candidates)  # last 12 digits of the VID are not an Aadhaar


def test_date_parsing_formats():
    for raw in ("17/05/1995", "17-05-1995", "17.05.1995", "1995-05-17", "17 May 1995", "17-MAY-1995", "May 17, 1995"):
        assert p.parse_date(raw) == date(1995, 5, 17), raw
    assert p.parse_date("05/17/1995", country="US") == date(1995, 5, 17)
    assert p.parse_date("31/02/1995") is None
    assert p.parse_date("no date here") is None
    ex = p.extract_fields("Name: A B\nDate of Birth\n17/05/1995\nDate of Issue: 01/01/2020\nDate of Expiry: 31/12/2030", today=TODAY)
    assert ex.dob == date(1995, 5, 17) and ex.expiry == date(2030, 12, 31)
    ex = p.extract_fields("Year of Birth : 1995", today=TODAY)
    assert ex.yob == 1995 and ex.dob is None


def test_name_similarity_and_homoglyphs():
    assert p.name_similarity("Rahul Mehta", "RAHUL KUMAR MEHTA") >= 0.95  # middle name
    assert p.name_similarity("Rahul Mehta", "MEHTA RAHUL") == 1.0  # order
    assert p.name_similarity("Rahul Mehta", "RAHUL MEHIA") >= 0.75  # OCR noise
    assert p.name_similarity("Rahul Mehta", "PRIYA SHARMA") < 0.5
    assert p.name_similarity("Mr. Rahul Mehta", "Rahul Mehta") == 1.0
    assert p.name_similarity("", "Rahul") == 0.0
    # Apple Vision sometimes returns Cyrillic look-alikes ("МЕНТА")
    assert p.normalize_text("Name: RAHUL МЕНТА") == "Name: RAHUL MEHTA"
    assert p.name_in_text("Rahul Mehta", "GOVT OF INDIA\nRAHUL МЕНТА\nDOB 17/05/1995") == 1.0
    assert p.name_in_text("Rahul Mehta", "PRIYA SHARMA") < 0.5


def test_levenshtein_and_closeness():
    assert p.levenshtein("ABCPM1234F", "ABCPM1234F") == 0
    assert p.levenshtein("ABCPM1234F", "ABCPM1284F") == 1
    assert p.levenshtein("kitten", "sitting") == 3
    assert p.numbers_close("234567890124", "23456789O124".replace("O", "0"))
    assert p.numbers_close("ABCPM1234F", "ABCPM124F")  # one deletion
    assert not p.numbers_close("ABCPM1234F", "ABCPM9934F")


# ====================================================================== consensus (pure)
def _ex(text: str, doc_type: str = "aadhaar") -> p.Extraction:
    return p.extract_fields(text, doc_type=doc_type, today=TODAY)


def test_consensus_rules():
    n = aadhaar()
    good = f"Rahul Mehta\nDOB: 17/05/1995\n{n[:4]} {n[4:8]} {n[8:]}"
    off_by_one = good.replace(n[8:], n[8:11] + str((int(n[11]) + 1) % 10))
    c = p.build_consensus({"tesseract": _ex(good), "apple_vision": _ex(good)}, doc_type="aadhaar")
    assert c.accepted and c.number == n and c.outcome == "agreement" and sorted(c.agreeing) == ["apple_vision", "tesseract"]
    assert c.score == 1.0
    # Levenshtein <= 1 tolerated when the checksum validates on the consensus number
    c = p.build_consensus({"tesseract": _ex(good), "apple_vision": _ex(off_by_one)}, doc_type="aadhaar")
    assert c.accepted and c.number == n and len(c.agreeing) == 2
    # one engine + valid checksum: accepted, but only one engine agrees
    c = p.build_consensus({"tesseract": _ex(good), "apple_vision": _ex("blurry nothing useful here")}, doc_type="aadhaar")
    assert c.accepted and c.outcome == "checksum" and c.agreeing == ["tesseract"] and c.score == 0.5
    # the same invalid number read by two engines -> checksum_invalid
    c = p.build_consensus({"tesseract": _ex(off_by_one), "apple_vision": _ex(off_by_one)}, doc_type="aadhaar")
    assert not c.accepted and c.outcome == "checksum_invalid"
    # formats without checksum need two engines
    pan = "Permanent Account Number\nABCPM1234F\nName: RAHUL MEHTA"
    c = p.build_consensus({"tesseract": _ex(pan, "pan")}, doc_type="pan")
    assert not c.accepted and c.outcome == "single" and c.best_guess == "ABCPM1234F"
    c = p.build_consensus({"tesseract": _ex(pan, "pan"), "apple_vision": _ex(pan.replace("1234", "9876"), "pan")}, doc_type="pan")
    assert not c.accepted and c.outcome == "disagreement"
    c = p.build_consensus({"tesseract": _ex(pan, "pan"), "vision_llm": _ex(pan.replace("1234", "1284"), "pan"),
                           "apple_vision": _ex(pan, "pan")}, doc_type="pan")
    assert c.accepted and c.number == "ABCPM1234F" and len(c.agreeing) == 3  # majority variant stored
    c = p.build_consensus({"tesseract": _ex("")}, doc_type="pan")
    assert not c.accepted and c.outcome == "none" and c.checksum == "missing"


# ====================================================================== OCR service
def _png(text: str = "HELLO 12345", size=(900, 240)) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    font = None
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(path, 44)
            break
        except OSError:
            continue
    d.text((30, 80), text, fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def fake_engines(monkeypatch):
    """Replace the local engines with deterministic fakes and mark them available."""
    calls: list[str] = []

    def make(name, text):
        def run(img: bytes):
            calls.append(name)
            if isinstance(text, Exception):
                raise text
            return (text(img) if callable(text) else text), 0.9
        return run

    def install(**engines):
        status = {n: ("not installed" if n not in engines else None) for n in ocr.LOCAL_ENGINES}
        monkeypatch.setattr(ocr, "_local_status", lambda: dict(status))
        for name, text in engines.items():
            monkeypatch.setitem(ocr._SYNC_ENGINES, name, make(name, text))
        return calls

    return install


async def test_run_ocr_runs_engines_concurrently_and_never_raises(fake_engines):
    fake_engines(tesseract="Name: RAHUL MEHTA", rapidocr=RuntimeError("model missing"))
    res = await ocr.run_ocr(_png(), engines=["tesseract", "rapidocr", "apple_vision", "nonsense"])
    by = {r.engine: r for r in res.results}
    assert by["tesseract"].ok and by["tesseract"].text == "Name: RAHUL MEHTA" and by["tesseract"].confidence == 0.9
    assert not by["rapidocr"].ok and "RuntimeError" in by["rapidocr"].error
    assert not by["apple_vision"].ok and "Not available" in by["apple_vision"].error
    assert not by["nonsense"].ok
    assert res.engines_used == ["tesseract"] and res.best_text == "Name: RAHUL MEHTA"
    garbage = await ocr.run_ocr(b"definitely not an image", engines=["tesseract"])
    assert garbage.results and not garbage.results[0].ok and "decoded" in garbage.results[0].error


async def test_engine_timeout_is_reported(fake_engines, monkeypatch):
    monkeypatch.setattr(ocr, "ENGINE_TIMEOUT", 0.2)
    fake_engines(tesseract=lambda img: time.sleep(1.0) or "late")
    res = await ocr.run_ocr(_png(), engines=["tesseract"])
    assert not res.results[0].ok and "Timed out" in res.results[0].error


async def test_available_engines_honours_settings(fake_engines, monkeypatch):
    fake_engines(tesseract="x", apple_vision="y")
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ocr_engines", ["tesseract"])
    assert ocr.available_engines() == ["tesseract"]
    res = await ocr.run_ocr(_png(), engines=["apple_vision"])
    assert not res.results[0].ok and "Disabled" in res.results[0].error
    rows = ocr.engine_catalog(include_unsupported=True)
    assert [r["name"] for r in rows] == ["tesseract"]


async def test_vision_llm_engine_with_fake_ai(fake_engines, fake_ai):
    fake_engines()
    provider = fake_ai(responses=["```\nREPUBLIC OF INDIA\nName: RAHUL MEHTA\n```"])
    assert "vision_llm" in ocr.available_engines()
    res = await ocr.run_ocr(_png(), engines=["vision_llm"], purpose="id_card")
    r = res.results[0]
    assert r.ok and r.text == "REPUBLIC OF INDIA\nName: RAHUL MEHTA" and r.confidence is None
    sent = provider.prompts[0]
    assert sent["images"] and sent["max_tokens"] == 700 and "MRZ" in sent["prompt"]


async def test_vision_llm_engine_ai_down(fake_engines, ai_down):
    fake_engines()
    res = await ocr.run_ocr(_png(), engines=["vision_llm"])
    assert not res.results[0].ok and res.results[0].error == "AI unavailable"


async def test_ocr_document_renders_pdf_pages_and_concatenates(fake_engines):
    from PIL import Image

    pages = [Image.new("RGB", (600, 800), "white"), Image.new("RGB", (600, 800), "white")]
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:])
    counter = iter(range(1, 10))
    fake_engines(tesseract=lambda img: f"page {next(counter)}")
    res = await ocr.ocr_document(buf.getvalue(), "application/pdf", max_pages=3)
    r = res.results[0]
    assert r.ok and r.text == "page 1\n\npage 2"
    single = await ocr.ocr_document(buf.getvalue(), "application/pdf", max_pages=1)
    assert single.results[0].text.count("page") == 1
    broken = await ocr.ocr_document(b"%PDF-1.4 broken", "application/pdf")
    assert broken.results and not broken.results[0].ok
    image = await ocr.ocr_document(_png(), "image/png")
    assert image.results[0].ok


def test_layout_lines_orders_vision_boxes():
    # (text, conf, x, y, w, h) in Vision's bottom-left coordinates: two boxes on the top line, one below
    items = [("MEHTA", 1.0, 0.5, 0.8, 0.2, 0.1), ("RAHUL", 0.5, 0.1, 0.8, 0.2, 0.1), ("DOB 17/05/1995", 1.0, 0.1, 0.5, 0.5, 0.1)]
    text, conf = ocr._layout_lines(items)
    assert text == "RAHUL MEHTA\nDOB 17/05/1995" and conf == pytest.approx(2.5 / 3)


@pytest.mark.skipif("tesseract" not in ocr.available_engines(), reason="Tesseract is not installed")
async def test_real_tesseract_reads_clear_text():
    ocr.reset_engine_cache()
    res = await ocr.run_ocr(_png("ABCPM1234F 17/05/1995"), engines=["tesseract"])
    r = res.results[0]
    assert r.ok and "ABCPM1234F" in r.text.replace(" ", "") and 0 < (r.confidence or 0) <= 1
