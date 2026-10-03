"""Fast regression tests for the text/geometry rules that were each found by
tracing a real failure against ground truth. No OCR, no PDFs, no pytest -
run with:

    python -m pdf_extractor.test_units

Every case here corresponds to something that actually broke on real Akowe
data; the comment on each says what.
"""

import sys
from pathlib import Path

import pandas as pd

from .batch import (_filename_sort_key, drop_cross_file_duplicates, drop_overlapping_files,
                    drop_repeats_across_pages)
import numpy as np
from PIL import Image, ImageDraw

from . import pages
from .cli import (_detect_banners, _discard_repeated_readings, _flag_two_number_pages, _hold_two_number_page,
                  _larger_zoom_if_small_text)
from .dewarp import _needs_correction
from .classify import classify_line
from .columns import (Line, _group_words_into_lines, _merge_split_name_bands, _merge_wrapped_names,
                      _name_shape, _number_column_bands)
from .ocr import _wordlike
from .pages import _banner_key, _looks_isolated, resolve_page_numbers
from .confidence import score_record
from .corrections import _char_substitutions, find_corrections, mine_patterns
from .parser import clean_name_text, is_non_name
from .review import build_review_rows

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"FAIL {label}: got {got!r}, want {want!r}")


def test_name_cleaning():
    cases = [
        # list-number / matric prefixes (FUTA prints "N. MATRIC NAME")
        ("72. BAMGBOSE, Tunde Kayode", "BAMGBOSE, Tunde Kayode"),
        ("49 FST/15/2041 ADEKUNLE Bola Samuel", "ADEKUNLE Bola Samuel"),
        ("FWT1/15/2118 OKAFOR Chidi Emeka", "OKAFOR Chidi Emeka"),  # OCR'd digit in faculty code
        # matric AFTER the name, and trailing list numbers
        ("EKANEM Grace Idara-Abasi 14 PMT/14/7302", "EKANEM Grace Idara-Abasi"),
        ("NWACHUKWU Ike Obinna 1.", "NWACHUKWU Ike Obinna"),
        ("ADEGOKE Ruth Folake A.", "ADEGOKE Ruth Folake A."),  # initial's period must survive
        # surname fused onto given name (tight kerning)
        ("BABALOLAKunle Peter", "BABALOLA Kunle Peter"),
        ("50 FST/15/2093 ADEYEMIBolanleOluwa-Seun", "ADEYEMI Bolanle Oluwa-Seun"),
        ("McDonald John", "McDonald John"),  # genuine camel case untouched
        ("MCDONALD John", "MCDONALD John"),
        # I / l / ! / | glyph confusion
        ("ADEYEM! Bisi Grace", "ADEYEMI Bisi Grace"),
        ("AKINYEM| Femi Ayomide", "AKINYEMI Femi Ayomide"),
        ("ADEDEJ|Kunle Paul", "ADEDEJI Kunle Paul"),
        ("OKAFOR-JAMES lfeoma", "OKAFOR-JAMES Ifeoma"),
        ("ASUQUO |bidun Godwin", "ASUQUO Ibidun Godwin"),
        ("SALAKO Samue! Ade", "SALAKO Samuel Ade"),
        # stray characters inside a long surname
        ("ADEBAN4JO Bukola Oreoluwa", "ADEBANJO Bukola Oreoluwa"),
        ("OLADE.JO Oluwabunmi Joshua", "OLADEJO Oluwabunmi Joshua"),
        ("ADEBOY!E Clara Adeola", "ADEBOYE Clara Adeola"),
        ("OKONKWO A.O. Ade", "OKONKWO A.O. Ade"),  # initials keep their dots
        # gender column (COVENANT postgraduate): never part of the name
        ("EFFIONG Utibe Abasi Idara Male", "EFFIONG Utibe Abasi Idara"),
        ("Blessing Chinenye Female", "Blessing Chinenye"),
        ("Male", ""), ("Female", ""),
        ("MALEK Ahmed Female", "MALEK Ahmed"),  # a name that merely starts with "Male" stays
        # gender column, bracketed form (UNILORIN undergraduate lists)
        ("BAMISHE, Kehinde Motunrayo (F)", "BAMISHE, Kehinde Motunrayo"),
        ("SOME NAME (M)", "SOME NAME"),
        ("AGBAJE, Sekinat Omowunmi (F}", "AGBAJE, Sekinat Omowunmi"),  # OCR'd mismatched bracket
        ("LAWAL, Rukayat Adenike {F)", "LAWAL, Rukayat Adenike"),        # OCR'd mismatched bracket, other way
        ("JOHNSON, Michael (JR)", "JOHNSON, Michael (JR)"),  # not a gender marker - left alone
    ]
    for src, want in cases:
        check(f"clean_name_text({src!r})", clean_name_text(src), want)


def test_wordlike():
    # A low-confidence token is kept only if it still looks like a word:
    # "ADEDE4JI" (a surname with one stray digit, confidence 16) was being
    # dropped, taking the surname out of the record.
    for text, want in [("ADEDE4JI", True), ("Emmanuel", True), ("FASHOLA", True),
                       ("O'Brien", True), ("4", False), ("|", False), ("12.5", False),
                       ("a1", False), ("--", False), ("FST/15/1", False), ("ab", False)]:
        check(f"_wordlike({text!r})", _wordlike(text), want)


def _rows(words):
    df = pd.DataFrame(words, columns=["text", "top", "left", "width"])
    df["height"] = 18
    return df


def test_line_grouping():
    # Tightly leaded table (real UNILAG page: row height ~18px, row step
    # ~13px): two rows in the same column must NOT merge into one line.
    tight = _rows([("ADEOYE,", 753, 108, 46), ("Bukunmi", 754, 162, 69), ("Peter", 755, 238, 60),
                   ("BALOGUN,", 778, 109, 72), ("Folake", 779, 187, 56), ("Grace", 780, 250, 44)])
    rows = [" ".join(tight.loc[i].sort_values("left").text) for i in _group_words_into_lines(tight)]
    check("tight rows stay separate", rows, ["ADEOYE, Bukunmi Peter", "BALOGUN, Folake Grace"])

    # Adjacent words in ONE row touch by a pixel (real FUTA scan: surname
    # ending x=468, given name starting x=467) - must stay in one line.
    touching = _rows([("ADEKOLA", 302, 339, 129), ("Ruth", 298, 467, 75), ("Oluwatoyin", 309, 550, 131)])
    rows = [" ".join(touching.loc[i].sort_values("left").text) for i in _group_words_into_lines(touching)]
    check("touching words stay in one row", rows, ["ADEKOLA Ruth Oluwatoyin"])


def test_batch_file_order():
    def order(names):
        return sorted(names, key=lambda n: _filename_sort_key(Path(n)))
    check("PGS ranges in numeric order",
          order(["LASU 2009 (PGS 140 - 180)_x.pdf", "LASU 2009 (PGS 33 - 132)_x.pdf", "LASU 2009 (195 - 202)_x.pdf"]),
          ["LASU 2009 (PGS 33 - 132)_x.pdf", "LASU 2009 (PGS 140 - 180)_x.pdf", "LASU 2009 (195 - 202)_x.pdf"])
    check("unmarked main file before '(from page N)'",
          order(["OOU 2022 (from page 182)_x.pdf", "OOU 2022_x.pdf"]),
          ["OOU 2022_x.pdf", "OOU 2022 (from page 182)_x.pdf"])


def test_classification():
    for text, field in [("B. Sc. (International Relations)", "degree"),
                        ("B. Ch.D (Dentistry)", "degree"),
                        ("(Part Time) - Supplementary", "programme"),
                        ("ARCHBISHOP VINING COLLEGE OF THEOLOGY, AKURE", "affiliated_institution"),
                        ("FACULTY OF ENGINEERING", "faculty"),
                        ("DEPARTMENT OF ARCHITECTURE", "department")]:
        check(f"classify_line({text!r}).field", classify_line(text)["field"], field)
    # an all-caps name with no comma must not be read as a header
    check("all-caps name is a name row", classify_line("BAMGBOSE OLUWATOYIN ADAEZE FOLASADE")["is_header"], False)


def test_split_name_band_merge():
    # shape classifier: ignores list numbers and matric codes
    for text, want in [("CVE/11/3806 SALAMI", "surname"), ("32 AGE/06/8215 BAMGBOSE", "surname"),
                       ("CVEI04/3517 OKORO", "surname"),        # OCR'd matric, still all caps
                       ("KemiSola", "given"), ("Hope Ngozi", "given"),
                       ("BAMGBOSE, Tunde Kayode", "other"),     # a complete name
                       ("FST/15/2064 ADEKOLA Ruth Oluwatoyin", "other")]:
        check(f"_name_shape({text!r})", _name_shape(text), want)

    def table(n=8):  # `No. | Matric SURNAME | Given names | Degree` rows, like FUTA's postgraduate lists
        rows = []
        for i in range(n):
            y = 100 + i * 30
            rows += [(f"{i+1}", y, 60, 20), (f"CVE/11/{4000+i}", y, 100, 90), (f"SURNAME{'ABCDEFGH'[i]}", y, 200, 100),
                     ("Given Names", y, 400, 110), ("M.Eng", y, 700, 50), ("Civil", y, 760, 40), ("Engineering", y, 810, 90)]
        return _rows(rows)
    bands = [(0, 350), (350, 650), (650, 1000)]
    check("surname|given|degree table merges surname+given only", _merge_split_name_bands(table(), bands),
          [(0, 650), (650, 1000)])

    def two_columns(n=8):  # two side-by-side lists of COMPLETE names on the same line grid (LASU-style)
        rows = []
        for i in range(n):
            y = 100 + i * 30
            rows += [(f"OKORO,", y, 60, 80), ("Chidi", y, 150, 70), ("Emeka", y, 230, 80),
                     (f"SALAMI,", y, 600, 80), ("Rilwan", y, 690, 50), ("Kazeem", y, 750, 80)]
        return _rows(rows)
    check("two independent name columns are NOT merged", _merge_split_name_bands(two_columns(), [(0, 500), (500, 1000)]),
          [(0, 500), (500, 1000)])


def test_adaptive_zoom():
    def words(height, n=60):
        return pd.DataFrame({"text": ["Adebayo"] * n, "height": [height] * n})
    # LASU 2012's dense pages: ~11px words at zoom 2.0 -> re-render larger
    z = _larger_zoom_if_small_text(words(11), 2.0)
    check("small text triggers a larger zoom", z is not None and 3.0 < z <= 4.0, True)
    check("normal print (16px) is left alone", _larger_zoom_if_small_text(words(16), 2.0), None)
    check("13px is at the threshold: left alone", _larger_zoom_if_small_text(words(13), 2.0), None)
    check("too few words to judge size", _larger_zoom_if_small_text(words(10, n=10), 2.0), None)
    check("zoom is capped", _larger_zoom_if_small_text(words(5), 2.0), 4.0)


def test_dewarp_only_fires_on_real_keystone():
    W, H = 1191, 1684
    # inner page frame, 1.8 degrees tilted, opposite sides ~equal (the real
    # LASU 2024 false positive: corners (79,130) (1078,162) (1074,1552) (75,1552))
    frame = np.array([[79, 130], [1078, 162], [1074, 1552], [75, 1552]], dtype="float32")
    check("offset+tilted inner rectangle is left alone", _needs_correction(frame, W, H), False)
    # a genuinely photographed page: trapezoid (synthetic 8% side difference)
    photo = np.array([[150, 80], [880, 40], [850, 1150], [100, 1100]], dtype="float32")
    check("real trapezoid is corrected", _needs_correction(photo, W, H), True)
    # the frame of the image itself
    check("full-frame quad is left alone",
          _needs_correction(np.array([[0, 0], [W, 0], [W, H], [0, H]], dtype="float32"), W, H), False)


def test_number_column_bands():
    # LASU 2024: three tight columns of "N. NAME". Each column's numbers are
    # right-aligned; the name-to-next-number gap is as small as the
    # number-to-name gap, which fooled gap-based detection into cutting AFTER
    # the next column's numbers.
    rows = []
    for c, x in enumerate((120, 440, 760)):
        for i in range(10):
            y = 200 + i * 30
            rows += [(f"{i + 10}.", y, x, 30), ("SURNAME", y, x + 50, 100), ("Given", y, x + 160, 60)]
    bands = _number_column_bands(_rows(rows), 1191)
    check("bands start at each number column", [round(b[0]) for b in bands[1:]], [428, 748])
    check("three bands", len(bands), 3)
    check("no numbers -> no anchored bands",
          _number_column_bands(_rows([("SURNAME", 100 + i * 30, 100, 100) for i in range(30)]), 1191), None)


def test_wrapped_name_merge():
    def L(text, top, col=0, left=100, right=300):
        return Line(text=text, top=top, bottom=top + 16, left=left, column_index=col, right=right)
    lines = [L("5. BELLO HALIMAT", 100), L("TOLUWANI", 118, left=120, right=190),      # one-word tail
             L("6. OKORO CHIAMAKA", 140), L("NKEIRUKA", 158, left=120, right=190),
             L("7. EKPO MFON", 180, right=300), L("ANNE MARIE", 198, left=120, right=190),   # two words: prev numbered + full
             L("8. SHORT NAME", 220, right=180), L("TWO WORDS", 238, left=120, right=190),        # two words, prev short: NOT a tail
             L("9. LAWAL SIDIKAT", 260),
             L("10. FAR AWAY NAME", 400, right=300), L("STRAY", 500, left=120, right=190)]       # too far below
    got = [l.text for l in _merge_wrapped_names(lines, True)]
    check("wrapped names rejoined, real records kept apart", got,
          ["5. BELLO HALIMAT TOLUWANI", "6. OKORO CHIAMAKA NKEIRUKA", "7. EKPO MFON ANNE MARIE",
           "8. SHORT NAME", "TWO WORDS", "9. LAWAL SIDIKAT", "10. FAR AWAY NAME", "STRAY"])
    check("non-numbered pages are left alone", [l.text for l in _merge_wrapped_names(lines, False)],
          [l.text for l in lines])
    # a header between the name and the tail in the same column breaks the link
    hdr = Line(text="THIRD CLASS", top=118, bottom=134, left=100, column_index=0, is_header=True, right=250)
    got = [l.text for l in _merge_wrapped_names([L("5. BELLO HALIMAT", 100), hdr, L("TOLUWANI", 136)], True)]
    check("header separates name from a lone word", got, ["5. BELLO HALIMAT", "THIRD CLASS", "TOLUWANI"])


def test_cross_file_dedupe():
    def rec(name, src, faculty="ARTS"):
        return {"name": name, "faculty": faculty, "department": "", "course": "", "degree": "B.A",
                "grade": "", "year": "2021", "source_file": src}
    got = drop_cross_file_duplicates([
        rec("ADE BOLA", "a.pdf"), rec("ADE BOLA", "a.pdf"),      # namesakes on one file: both kept
        rec("ADE BOLA", "b.pdf"),                                 # same record again in another file: dropped
        rec("ADE BOLA", "b.pdf", faculty="LAW"),                  # different context: kept
        rec("OKE TUNDE", "b.pdf")])
    check("cross-file duplicates dropped, in-file twins kept", [(r["name"], r["source_file"], r["faculty"]) for r in got],
          [("ADE BOLA", "a.pdf", "ARTS"), ("ADE BOLA", "a.pdf", "ARTS"), ("ADE BOLA", "b.pdf", "LAW"),
           ("OKE TUNDE", "b.pdf", "ARTS")])


def test_repeats_across_pages():
    def rec(name, page, half=None, grade=""):
        return {"name": name, "grade": grade, "_geom": {"page_index": page, "spread_half": half}}

    def names(prefix, n):  # distinct 3-part names that can't collide by chance
        return [f"{prefix}{chr(65 + i // 5)}{chr(65 + i % 5)} Given{chr(65 + i)} Other{chr(65 + i)}" for i in range(n)]

    first = names("ALPHA", 20)
    # page 3 re-prints 14 of page 0's people (with a different grade, as the extractor really did) plus 6 new ones
    page0 = [rec(n, 0, grade="First Class") for n in first]
    page3 = [rec(n, 3, grade="Second Class") for n in first[:14]] + [rec(n, 3) for n in names("BRAVO", 6)]
    got = drop_repeats_across_pages(page0 + page3)
    check("a re-printed page's shared names are dropped, even when the grade differs",
          [r["_geom"]["page_index"] for r in got].count(3), 6)
    check("...and every one of them survives on the first page", [r["_geom"]["page_index"] for r in got].count(0), 20)
    check("the new names on the re-printed page are kept",
          {r["name"] for r in got if r["_geom"]["page_index"] == 3}, set(names("BRAVO", 6)))

    # unrelated pages sharing one common name: two graduates, not a re-print
    other = [rec(n, 7) for n in names("CHARLIE", 19)] + [rec(first[0], 7)]
    check("one shared name between unrelated pages is a namesake and is kept",
          len(drop_repeats_across_pages(page0 + other)), 40)

    # six shared names clears the floor, but on two 60-name pages that's 10%: chance, not a re-print
    big_a = [rec(n, 0) for n in names("INDIA", 30)] + [rec(n, 0) for n in names("JULIET", 30)]
    big_b = [rec(n, 9) for n in names("INDIA", 6)] + [rec(n, 9) for n in names("KILO", 54)]
    check("a few shared names on two long pages is below the share threshold and is kept",
          len(drop_repeats_across_pages(big_a + big_b)), 120)

    small = [rec(n, 0) for n in names("DELTA", 10)]
    small_later = [rec(n, 1) for n in names("DELTA", 10)[:3]] + [rec(n, 1) for n in names("ECHO", 7)]
    check("3 shared names is below the floor even at 30% of a short page",
          len(drop_repeats_across_pages(small + small_later)), 20)

    twins = [rec("ADEBAYO Tunde", 1), rec("ADEBAYO Tunde", 1)] + [rec(n, 1) for n in names("FOXTROT", 8)]
    check("two same-named graduates on one page are both kept", len(drop_repeats_across_pages(twins)), 10)

    half_a = [rec(n, 2, half=0) for n in names("GOLF", 12)]
    half_b = [rec(n, 2, half=1) for n in names("GOLF", 12)]
    check("the two halves of a spread are different pages, so a repeat between them is caught",
          len(drop_repeats_across_pages(half_a + half_b)), 12)

    noinfo = [{"name": n, "grade": ""} for n in names("HOTEL", 8)]
    check("records without page information are never dropped",
          len(drop_repeats_across_pages(noinfo + [dict(r) for r in noinfo])), 16)


def test_overlapping_files():
    def rec(name, src):
        return {"name": name, "source_file": src}
    def word(i, tag):  # letters only: the name key ignores digits
        return tag + "".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))
    people = [f"{word(i, 'sur')} {word(i, 'giv')}" for i in range(100)]
    a = [rec(n, "a.pdf") for n in people]
    # b: a re-scan copy of a (same people, some OCR variants) -> dropped whole
    b = [rec(n, "b.pdf") for n in people[:90]] + [rec(f"{word(i, 'gar')} xx", "b.pdf") for i in range(10)]
    check("near-copy file dropped entirely", {r["source_file"] for r in drop_overlapping_files(a + b)}, {"a.pdf"})
    # c: overlaps a by 30% -> only the shared names go
    c = [rec(n, "c.pdf") for n in people[:30]] + [rec(f"{word(i, 'new')} aa", "c.pdf") for i in range(70)]
    got = drop_overlapping_files(a + c)
    check("partial overlap: shared names removed from the later file", sum(r["source_file"] == "c.pdf" for r in got), 70)
    check("partial overlap: earlier file untouched", sum(r["source_file"] == "a.pdf" for r in got), 100)
    # d: unrelated file sharing 3 common names -> untouched
    d = [rec(n, "d.pdf") for n in people[:3]] + [rec(f"{word(i, 'oth')} bb", "d.pdf") for i in range(97)]
    check("chance overlap is left alone", len(drop_overlapping_files(a + d)), 200)


def test_non_name_lines():
    for text, want in [("Supervisor:", True), ("Bervisor:", True), ("Thesis:", True), ("nil", True), ("NIL", True),
                       ("Prof.D.A. Bello & Dr Segun Ojo", True), ("Dr. Ade Bola", True),
                       ("Institutional factors as correlates of:", True),
                       ("DRAKE, Emeka Paul", False), ("PROFIT Adeyemi Tunde", False), ("NILE, Sarah Jane", False),
                       ("OKONKWO, Chidi Samuel", False), ("Tunde Kayode", False)]:
        check(f"is_non_name({text!r})", is_non_name(text), want)


def test_confidence_scoring():
    def geom(conf=90.0, repaired=False, page_status="detected"):
        return {"conf": conf, "repaired": repaired, "page_number_status": page_status}
    # unambiguous structural red flags, regardless of OCR confidence
    check("gender marker flagged low", score_record("BALOGUN Tunde Ruth (F)", geom())[0], "low")
    check("matric leftover flagged low", score_record("CVE/5/3391 ADEGOKE Taiwo", geom())[0], "low")
    check("single word flagged low", score_record("Adebayo", geom())[0], "low")
    # a long compound name NOT in vocabulary is unfamiliar, not proven fused -
    # medium, not low (see confidence.py: a same-shaped rule miscalibrated badly)
    check("long unrecognized token flagged medium, not low",
          score_record("OMOIJUIADEJOSHUA Okafor", geom())[0], "medium")
    # a clean, recognized, high-confidence name: high, no reason
    check("clean known name is high confidence", score_record("ADEYEMI Olufemi Samuel", geom(95.0)), ("high", ""))
    # low OCR confidence alone is enough, even for a real-looking name
    check("low OCR confidence flagged low", score_record("ADEYEMI Olufemi Samuel", geom(20.0))[0], "low")
    # a repaired line (wrap-merge) that isn't in the name vocabulary: flagged, not silently trusted
    check("repaired + unrecognized flagged medium", score_record("XQPLMZ Vwbnrk", geom(95.0, repaired=True))[0], "medium")
    # repaired but a recognized name: still flagged (medium), never silently trusted just because it's known
    check("repaired known name still flagged", score_record("ADEYEMI Olufemi", geom(95.0, repaired=True))[0], "medium")
    # missing geometry (e.g. a header-adjacent record) doesn't crash
    check("no geom handled", score_record("ADEYEMI Olufemi Samuel", None)[0] in ("high", "medium", "low"), True)
    # an unresolved page number flags the record even with a clean name -
    # Akowe's report assembly matches pages by this number, so it's exactly
    # as load-bearing as the name itself
    check("uncertain page number flagged despite a clean name",
          score_record("ADEYEMI Olufemi Samuel", geom(95.0, page_status="uncertain")),
          ("medium", "page_number_uncertain"))
    check("interpolated page number is trusted, not flagged - it's arithmetic from two confirmed anchors",
          score_record("ADEYEMI Olufemi Samuel", geom(95.0, page_status="interpolated")),
          ("high", ""))


def test_corrections_diff_and_mining():
    original = [
        {"name": "OKUNOL, Bisola", "faculty": "ARTS", "confidence": "low", "flag_reason": "low_ocr_confidence"},
        {"name": "QKAFOR Emeka", "faculty": "LAW", "confidence": "medium", "flag_reason": "name_not_in_vocabulary"},
        {"name": "ADEYEMI Olufemi", "faculty": "LAW", "confidence": "high", "flag_reason": ""},
    ]
    corrected = [
        {"name": "OKUNOLI, Bisola", "faculty": "ARTS"},
        {"name": "OKAFOR Emeka", "faculty": "LAW"},
        {"name": "ADEYEMI Olufemi", "faculty": "LAW"},   # untouched
    ]
    diffs = find_corrections(original, corrected)
    check("only the two edited rows produce diffs", len(diffs), 2)
    check("unchanged row produces no diff", any(d["row_index"] == 2 for d in diffs), False)

    check("char substitution extraction", sorted(_char_substitutions("QKAFOR", "OKAFOR")), [("Q", "O")])
    check("insertion extraction", _char_substitutions("OKUNOL", "OKUNOLI"), [("", "I")])

    mined = mine_patterns(diffs)
    check("substitution pattern mined", ("Q", "O") in [s for s, _ in mined["substitutions"]], True)
    check("a substitution comes with a token-pair example, not a whole name",
          mined["substitution_examples"][("Q", "O")], "QKAFOR -> OKAFOR")

    # 35 different one-letter confusions: the on-screen default stops at 30, but anything
    # that stores the patterns needs all of them (a cut-off would make the rest look gone)
    many = [{"field": "name", "before": f"K{a}Z", "after": f"K{b}Z"}
            for a, b in [(x, y) for x in "ABCDEFGH" for y in "ABCDEFGH" if x != y][:35]]
    check("report default keeps the top 30", len(mine_patterns(many)["substitutions"]), 30)

    # how many DIFFERENT booklets a pattern came from (a single bad scan repeats one correction endlessly)
    def fix(before, after, booklet):
        return {"field": "name", "before": before, "after": after, "booklet": booklet}
    spread = mine_patterns([fix("QKAFOR Emeka", "OKAFOR Emeka", "u|2020"), fix("QKAFOR Chidi", "OKAFOR Chidi", "u|2020"),
                            fix("QKAFOR Ngozi", "OKAFOR Ngozi", "v|2021"),
                            fix("ADEYEMI ZQXVTB", "ADEYEMI ZQXVTA", "u|2020"),
                            fix("ADEYEMI ZQXVTB", "ADEYEMI ZQXVTA", "v|2021")])
    check("three corrections across two booklets: counted 3 times, 2 booklets",
          (dict(spread["substitutions"])[("Q", "O")], spread["substitution_booklets"][("Q", "O")]), (3, 2))
    check("a name confirmed in two booklets says so", spread["vocabulary_booklets"]["ZQXVTA"], 2)
    check("corrections with no booklet information produce no booklet counts",
          (mine_patterns(diffs)["substitution_booklets"], mine_patterns(diffs)["vocabulary_booklets"]), ({}, {}))
    check("None lifts the limit", len(mine_patterns(many, limit_substitutions=None)["substitutions"]), 35)


def test_review_rows_flag_and_carry_image_path():
    records = [
        {"name": "ADEYEMI Olufemi Samuel", "faculty": "LAW", "department": "", "course": "", "degree": "",
         "grade": "", "distinction_notes": "", "year": "", "year_note": "", "page": "1", "programme": "",
         "affiliated_institution": "", "country": "", "_geom": {"conf": 95.0, "repaired": False}},
        {"name": "BALOGUN Tunde (F)", "faculty": "LAW", "department": "", "course": "", "degree": "",
         "grade": "", "distinction_notes": "", "year": "", "year_note": "", "page": "1", "programme": "",
         "affiliated_institution": "", "country": "", "_geom": {"conf": 95.0, "repaired": False}},
    ]
    rows, flagged = build_review_rows(records)
    check("high-confidence row not flagged for a snapshot", len(flagged), 1)
    check("flagged row references the right record", flagged[0][0], 1)
    check("review_image starts empty before snapshot generation", rows[1]["review_image"], "")


def test_looks_isolated():
    def words(rows):
        df = pd.DataFrame(rows, columns=["text", "top", "left", "width"])
        df["height"] = 16
        return df
    # candidate digit box: left=20, top=100, width=18, height=16
    # a list-item number ("12. ADEGBITE, Tolulope Kayode") - a real
    # name sits tight beside it, on the same row: not isolated
    row = words([("ADEGBITE,", 100, 60, 90), ("Tolulope", 100, 155, 80)])
    check("a list-item number is not isolated", _looks_isolated(row, 20, 100, 18, 16), False)

    # a genuine page number, alone in its corner - nothing anywhere near it
    far_row = words([("ADEGBITE,", 100, 500, 90)])
    check("a standalone page number with distant content is isolated",
          _looks_isolated(far_row, 20, 100, 18, 16), True)

    # a real word exists, but on a different row entirely - still isolated
    other_row = words([("SOMENAME", 400, 60, 90)])
    check("content on a different row doesn't count", _looks_isolated(other_row, 20, 100, 18, 16), True)

    # noise (stray punctuation) sitting right beside it doesn't disqualify -
    # only actual content counts as "not alone"
    noisy = words([("|", 100, 40, 5), (".", 100, 45, 4)])
    check("adjacent noise (not real content) still counts as isolated", _looks_isolated(noisy, 20, 100, 18, 16), True)

    # a matric code ("43 AEC/17/2237 OYEBANJI Grace Kehinde") - real content
    # even though it isn't a name-shaped word; a real failure (FUTA 2024)
    # slipped through an earlier version of this check because the matric
    # code was invisible to it, leaving only the more distant name to check
    # against, which cleared the gap
    matric_row = words([("AEC/17/2237", 100, 45, 100), ("OYEBANJI", 100, 200, 70)])
    check("a matric code counts as real content, not just name-shaped words",
          _looks_isolated(matric_row, 20, 100, 18, 16), False)

    check("no word data at all defaults to isolated (can't disprove it)", _looks_isolated(None, 20, 100, 18, 16), True)

    # a real, confirmed failure: FUTA 2024 tags its page number onto a
    # repeating slogan ("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA ---
    # [84]") - close enough to trip the plain proximity check, but it's the
    # exact same text on every page, which is what a banner already means
    banner_keys = {_banner_key("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA")}
    slogan_row = words([("THE", 100, 40, 30), ("BEST", 100, 74, 35), ("UNIVERSITY", 100, 113, 80),
                        ("OF", 100, 197, 20), ("TECHNOLOGY", 100, 221, 85), ("IN", 100, 310, 20),
                        ("NIGERIA", 100, 334, 65)])
    check("a recurring slogan doesn't disqualify a real page number, without knowing banners",
          _looks_isolated(slogan_row, 400, 100, 18, 16), False)
    check("...but does once the document's banner text is known",
          _looks_isolated(slogan_row, 400, 100, 18, 16, banner_keys), True)
    # a genuine list-item number must still be rejected even with banner_keys set - it's not the banner
    check("a real list-item name is never mistaken for the banner",
          _looks_isolated(row, 20, 100, 18, 16, banner_keys), False)


def test_resolve_page_numbers():
    # a single gap, cleanly bracketed - the only correct answer
    got = resolve_page_numbers([148, None, 150])
    check("single gap interpolated", [v for v, _ in got], [148, 149, 150])
    check("interpolated slot tagged", got[1][1], "interpolated")
    check("detected slots tagged", (got[0][1], got[2][1]), ("detected", "detected"))

    # LASU's real shape: long stretches where direct OCR finds nothing at
    # all, bracketed by just two readable anchors far apart - still exact
    check("long gap bridged by two distant anchors",
          [v for v, _ in resolve_page_numbers([5, None, None, None, None, 10])],
          [5, 6, 7, 8, 9, 10])

    # adjacent anchors: nothing to fill in, no interpolation entries created
    check("adjacent anchors untouched", resolve_page_numbers([5, 6]), [(5, "detected"), (6, "detected")])

    # two anchors that disagree with each other and with nothing else in
    # the document: neither is corroborated, so neither is trusted, and the
    # gap between them can't be bridged either - this is the same shape as
    # a real failure (several early LASU pages each reading a different,
    # equally uncorroborated wrong number)
    got = resolve_page_numbers([5, None, 40])
    check("mutually disagreeing anchors are all left uncertain",
          [s for _, s in got], ["uncertain", "uncertain", "uncertain"])

    # a cluster of readings that all disagree with EACH OTHER, elsewhere in
    # the same document a real scheme has enough agreement to be trusted -
    # the cluster must not leak into "detected" just because nothing sits
    # directly next to each one to contradict it individually
    got = resolve_page_numbers([1, 8, 4, 7, None, 143, 144, None, None, 147])
    check("uncorroborated early cluster rejected even with a confirmed scheme later",
          [s for _, s in got[:4]], ["uncertain"] * 4)
    check("the actually-confirmed scheme later in the document is still trusted",
          [v for v, _ in got[5:]], [143, 144, 145, 146, 147])

    # a misread anchor sandwiched between two that agree with each other:
    # dropped as a trusted reading, but its true value is still recovered by
    # interpolating straight through it from the two anchors that do agree
    # (indices 0 and 4 are 4 apart, so 148 -> 152 is the consistent pair)
    got = resolve_page_numbers([148, None, 999, None, 152])
    check("outlier's own OCR reading is no longer trusted", got[2][1], "interpolated")
    check("but its true value is still recovered by interpolation",
          [v for v, _ in got], [148, 149, 150, 151, 152])

    # no anchors at all: nothing can be known, all uncertain (never a guess)
    check("no anchors -> all uncertain", [s for _, s in resolve_page_numbers([None, None, None])],
          ["uncertain", "uncertain", "uncertain"])

    # an anchor at a document boundary with nothing beyond it to bound the
    # far side - stays uncertain rather than extrapolated
    got = resolve_page_numbers([None, None, 20])
    check("nothing before the only anchor stays uncertain", [s for _, s in got], ["uncertain", "uncertain", "detected"])


def _line(text, top, height=20, left=100):
    return Line(text=text, top=top, bottom=top + height, left=left, column_index=0)


def test_plain_page_number():
    H = 1000  # bottom edge band starts at y=850, top edge band ends at y=150

    check("bare digits near the bottom edge", pages._plain_page_number([_line("45", 900)], H), 45)
    check("a stray closing paren is tolerated (UI 2004 read '109)')",
          pages._plain_page_number([_line("109)", 900)], H), 109)
    check("a fully parenthesized number is tolerated", pages._plain_page_number([_line("(555)", 900)], H), 555)
    check("a digit line mid-page is not a page number", pages._plain_page_number([_line("45", 500)], H), None)
    check("a list-item line is not a page number", pages._plain_page_number([_line("12. AJAYI OLU", 900)], H), None)
    check("no lines at all", pages._plain_page_number([], H), None)

    # These booklets never approach 1,000 pages: a page number is 1-3 digits,
    # so a printed year (COVENANT's "2022", OOU's footer "2020") is refused
    # outright, even when it's the only candidate on the page
    check("a lone year is never a page number", pages._plain_page_number([_line("2022", 900)], H), None)
    check("a year beside a real number doesn't displace it",
          pages._plain_page_number([_line("2022", 900), _line("30", 930)], H), 30)
    check("999 is still a page number", pages._plain_page_number([_line("999", 900)], H), 999)
    check("any 4-digit number is refused, not just years", pages._plain_page_number([_line("1000", 900)], H), None)
    check("a 4-digit number isn't truncated into a plausible 3-digit one",
          pages._plain_page_number([_line("(1099)", 900)], H), None)

    check("bottom edge preferred over top edge",
          pages._plain_page_number([_line("7", 40), _line("88", 920)], H), 88)


def test_banner_fused_candidate_line():
    H = 1000
    futa = {_banner_key("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA")}
    covenant = {_banner_key("Covenant University Convocation Ceremony The Release of Eagles")}
    find = pages._banner_fused_candidate_line

    got = find([_line("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA (95)", 920)], futa, H)
    check("digit fused after the slogan is found, marked trailing", got and got[1], "trailing")

    got = find([_line("ih THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA (28)", 920)], futa, H)
    check("stray noise glued to the front doesn't hide the slogan", got and got[1], "trailing")

    got = find([_line("30 Covenant University 17 Convocation Ceremony -The Release of Eagles 2022", 920)], covenant, H)
    check("digit fused before the slogan is found, marked leading", got and got[1], "leading")

    check("a clean slogan with no digit is not a candidate",
          find([_line("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA", 920)], futa, H), None)
    check("a numbered name row is not mistaken for a fused slogan",
          find([_line("12 ADEGBITE Tolulope Kayode", 920)], futa, H), None)
    check("a fused slogan in the middle of the page is ignored",
          find([_line("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA (95)", 500)], futa, H), None)
    check("without known banners nothing is a candidate",
          find([_line("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA (95)", 920)], set(), H), None)


def test_reread_banner_fused_digit():
    words = pd.DataFrame({"text": ["THE", "BEST"], "top": [920.0, 920.0], "left": [100.0, 150.0],
                          "width": [40.0, 50.0], "height": [20.0, 20.0]})
    img = Image.new("RGB", (400, 1000), "white")
    line = _line("THE BEST", 920)
    real = pages.pytesseract.image_to_string

    def reread(ocr_text, side, w=words):
        pages.pytesseract.image_to_string = lambda *a, **k: ocr_text
        try:
            return pages._reread_banner_fused_digit(img, w, line, side)
        finally:
            pages.pytesseract.image_to_string = real

    # FUTA: a stray leading "1" from a flourish must not beat the real number at the end
    check("trailing side takes the last run, not a stray leading digit",
          reread("1 THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA 88 |", "trailing"), 88)
    # COVENANT: the slogan's own "17" and "2022" sit before/after the page number
    check("leading side takes the first run, skipping the slogan's own numbers",
          reread("30 Covenant University 17 Convocation Ceremony Eagles 2022", "leading"), 30)
    check("trailing side skips the slogan's own year and the ordinal in the middle",
          reread("Covenant University 17 Convocation Ceremony Eagles 2022 36", "trailing"), 36)
    check("a slogan ending in its own year with no page number is refused, not read as 2022",
          reread("Covenant University 17 Convocation Ceremony Eagles 2022", "trailing"), None)
    check("a 4-digit run is refused whole, never truncated to its first 3 digits",
          reread("30 Covenant University 2022", "trailing"), None)
    check("no digits at all", reread("THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA", "trailing"), None)
    check("no word data", reread("88", "trailing", w=words.iloc[0:0]), None)


def _badge_page(box, shape="ellipse", size=(1191, 1684)):
    """A blank page with one dark outlined badge drawn at `box` (x0, y0, x1, y1)."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    (draw.ellipse if shape == "ellipse" else draw.rectangle)(box, outline="black", width=3)
    return img


def test_oval_badge_page_number():
    real = pages.pytesseract.image_to_string

    def detect(img, ocr_text="(45)"):
        pages.pytesseract.image_to_string = lambda *a, **k: ocr_text
        try:
            return pages._oval_badge_page_number(img)
        finally:
            pages.pytesseract.image_to_string = real

    check("LASU-style elongated oval, bottom centre", detect(_badge_page((580, 1535, 627, 1570))), 45)
    check("OAU-style circle, bottom centre", detect(_badge_page((589, 1550, 647, 1608))), 45)
    check("a badge whose OCR reads nothing yields no page number",
          detect(_badge_page((580, 1535, 627, 1570)), ocr_text=""), None)
    check("an off-centre shape (a crest, say) is ignored", detect(_badge_page((60, 1535, 107, 1570))), None)
    check("a shape that's too small is ignored", detect(_badge_page((600, 1540, 612, 1550))), None)
    check("a shape that's too large is ignored", detect(_badge_page((540, 1500, 660, 1620))), None)
    check("a wide bar is not a badge", detect(_badge_page((540, 1540, 620, 1560), shape="rect")), None)
    check("a badge near the top of the page is ignored (bottom band only)",
          detect(_badge_page((580, 40, 627, 75))), None)
    check("a badge reading as a year is refused", detect(_badge_page((580, 1535, 627, 1570)), ocr_text="(2022)"), None)

    # wiring: when word-level OCR proposes nothing (as it does for these
    # badges), _boxed_page_number must fall through to the shape detector
    real_data = pages.pytesseract.image_to_data
    pages.pytesseract.image_to_data = lambda *a, **k: pd.DataFrame(
        {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []})
    pages.pytesseract.image_to_string = lambda *a, **k: "(31)"
    try:
        got = pages._boxed_page_number(_badge_page((589, 1550, 647, 1608)))
    finally:
        pages.pytesseract.image_to_data = real_data
        pages.pytesseract.image_to_string = real
    check("_boxed_page_number falls back to the badge detector", got, 31)

    # word-level OCR reading a footer year ("2020") must not become a page number
    pages.pytesseract.image_to_data = lambda *a, **k: pd.DataFrame(
        {"text": ["2020", "94"], "conf": [95.0, 95.0], "left": [400.0, 100.0], "top": [40.0, 40.0],
         "width": [80.0, 40.0], "height": [30.0, 30.0]})
    pages.pytesseract.image_to_string = lambda *a, **k: ""
    try:
        got = pages._boxed_page_number(Image.new("RGB", (1191, 1684), "white"))
    finally:
        pages.pytesseract.image_to_data = real_data
        pages.pytesseract.image_to_string = real
    check("_boxed_page_number takes the page number and skips the year beside it", got, 94)


def test_drop_repeated_readings():
    drop = pages.drop_repeated_readings
    # OOU-shaped: a fixed number read on most pages, a few genuine readings
    got = drop([28, 28, 92, 28, 94, 28, 28, 28, 98, None, 28, 28])
    check("a value repeated across most pages is fixed print and is blanked",
          got, [None, None, 92, None, 94, None, None, None, 98, None, None, None])
    check("genuine, distinct readings are untouched", drop([90, None, 92, 93, 94]), [90, None, 92, 93, 94])
    check("two coincidental matches are below the floor", drop([5, 5, 7, 8, 9, 10]), [5, 5, 7, 8, 9, 10])
    # numbering that restarts per section repeats each value a few times among many distinct ones
    restarts = list(range(1, 21)) * 3
    check("per-section restarts aren't mistaken for fixed print", drop(restarts), restarts)
    check("nothing read", drop([None, None]), [None, None])
    check("empty", drop([]), [])


def test_discard_repeated_readings_updates_page_meta():
    page_data = [(str(i), (800, 1000), [], 0, {"raw_page_number": v})
                 for i, v in enumerate([28, 28, 92, 28, 94, 28, None, 28, 98])]
    _discard_repeated_readings(page_data, verbose=False)
    check("repeated readings are cleared in each page's own metadata",
          [pm["raw_page_number"] for *_, pm in page_data], [None, None, 92, None, 94, None, None, None, 98])


def _corner(left=None, right=None):
    return {"left": left, "right": right}


def test_audit_corner_numbers():
    audit = pages.audit_corner_numbers
    # A booklet that keeps its numbers in the bottom-right corner, 40-47; the scan of
    # page index 3 runs into the neighbour (42 on the left), and on index 5 the page's own
    # number wasn't read and only the neighbour's (44) is showing.
    raw = [40, 41, 42, 43, 44, None, 46, 47]
    corners = [_corner(None, 40), _corner(None, 41), _corner(None, 42), _corner(42, 43),
               _corner(None, 44), _corner(44, None), _corner(None, 46), _corner(None, 47)]
    got = audit(raw, corners)
    check("clean pages are not flagged", [g for i, g in enumerate(got) if i not in (3, 5)], [None] * 6)
    check("two different numbers on a scan: flagged, naming both and the one in the booklet's own corner",
          got[3], {"note": "two_numbers", "suggested": 43,
                   "seen": [{"side": "left", "value": 42}, {"side": "right", "value": 43}]})
    check("only the neighbour's number showing: flagged, and the sequence suggests the page's own",
          got[5], {"note": "neighbour_number", "suggested": 45, "seen": [{"side": "left", "value": 44}]})

    # a reading is never trusted just because the detector produced it
    check("a page the detector read as the neighbour's number is still flagged",
          audit([40, 41, 42, 42, 44, 45, 46, 47], corners[:3] + [_corner(42, 43)] + corners[4:5]
                + [_corner(None, 45), _corner(None, 46), _corner(None, 47)])[3]["note"], "two_numbers")

    # the detector itself took the neighbour's number (39, left corner) on the first page: that
    # reading doesn't fit the sequence, so it must not teach the booklet's corner as "left"
    lead = audit([39, None, 42, 43],
                 [_corner(39, None), _corner(None, 41), _corner(None, 42), _corner(None, 43)])
    check("a wrongly read neighbour number is caught, and the sequence gives the page's own",
          lead[0], {"note": "neighbour_number", "suggested": 40, "seen": [{"side": "left", "value": 39}]})

    # nothing to learn the booklet's corner from: two different numbers are still flagged,
    # but one number on its own is not guessed at
    check("one number in a corner, booklet's side unknown: not flagged",
          audit([None, None], [_corner(7, None), _corner(None, None)]), [None, None])
    check("two numbers, booklet's side unknown: flagged with nothing suggested",
          audit([None, None], [_corner(7, 8), _corner(None, None)])[0],
          {"note": "two_numbers", "suggested": None,
           "seen": [{"side": "left", "value": 7}, {"side": "right", "value": 8}]})

    # a book numbered right on odd pages and left on even ones is normal, not a flaw
    alt_raw = [10, 11, 12, 13, 14, 15]
    alt = [_corner(10, None), _corner(None, 11), _corner(12, None), _corner(None, 13), _corner(14, None), _corner(None, 15)]
    check("numbers alternating sides by odd/even page are not flagged", audit(alt_raw, alt), [None] * 6)
    # ...and each parity's own corner is what a page is held to: index 6 is an even page, so its
    # number belongs on the left, and one showing only on the right is the neighbour's
    alt8 = alt + [_corner(12, None), _corner(None, 17)]
    alt8[6] = _corner(None, 15)
    check("the corner is learned separately for odd and even pages",
          [g["note"] if g else None for g in audit(alt_raw + [None, 17], alt8)], [None] * 6 + ["neighbour_number", None])

    # fixed print in a corner (a title year / ordinal on every page) is not a second number
    fixed_raw = list(range(60, 70))
    fixed = [_corner(28, 60 + i) for i in range(10)]
    check("a number repeated in a corner on most pages is fixed print", audit(fixed_raw, fixed), [None] * 10)

    # the suggestion must agree with what the page shows
    odd = audit([40, 41, 42, 43, 44, 45], [_corner(None, 40), _corner(None, 41), _corner(None, 42),
                                         _corner(99, 77), _corner(None, 44), _corner(None, 45)])
    check("a suggestion the page doesn't show is not offered for two numbers", odd[3]["suggested"], None)
    check("empty document", audit([], []), [])


def test_two_number_pages_are_held_for_review():
    def page(raw, corner):
        return ("0", (800, 1000), [], 0, {"raw_page_number": raw, "corner_numbers": corner})

    data = [page(40 + i, _corner(None, 40 + i)) for i in range(5)] + [page(45, _corner(44, 45))]         + [page(46 + i, _corner(None, 46 + i)) for i in range(2)]
    _flag_two_number_pages(data, verbose=False)
    flagged = data[5][4]
    check("a flagged page's reading stays, as one of the anchors for the booklet's sequence", flagged["raw_page_number"], 45)
    check("...and what it showed is kept for the reviewer",
          (flagged["page_number_note"], flagged["suggested_page_number"], [s["value"] for s in flagged["seen_numbers"]]),
          ("two_numbers", 45, [44, 45]))
    check("other pages are untouched", [pm["raw_page_number"] for *_, pm in data[:5]], [40, 41, 42, 43, 44])
    check("...and carry no note", [pm.get("page_number_note") for *_, pm in data[:5]], [None] * 5)

    hold = _hold_two_number_page
    check("a flagged page stays uncertain even when interpolation filled it in",
          hold({"page_number_note": "two_numbers"}, 45, "interpolated"), (None, "uncertain"))
    meta = {"page_number_note": "two_numbers"}
    hold(meta, 45, "interpolated")
    check("...and what interpolation said becomes the suggestion", meta["suggested_page_number"], 45)
    meta = {"page_number_note": "two_numbers", "suggested_page_number": 46}
    hold(meta, 45, "interpolated")
    check("...without replacing a suggestion already made", meta["suggested_page_number"], 46)
    check("an unflagged page is left as it was", hold({}, 45, "detected"), (45, "detected"))


def test_corner_numbers_reads_both_corners():
    def frame(*cells):   # (text, conf, left, top) in the upscaled corner crop's own coordinates
        return pd.DataFrame({"text": [c[0] for c in cells], "conf": [float(c[1]) for c in cells],
                             "left": [float(c[2]) for c in cells], "top": [float(c[3]) for c in cells],
                             "width": [60.0] * len(cells), "height": [60.0] * len(cells)})

    real = pages.pytesseract.image_to_data

    def read(left_cells, right_cells, words=None):
        replies = iter([frame(*left_cells), frame(*right_cells)])   # left corner is read first
        pages.pytesseract.image_to_data = lambda *a, **k: next(replies)
        try:
            return pages.corner_numbers(Image.new("RGB", (1191, 1684), "white"), words)
        finally:
            pages.pytesseract.image_to_data = real

    check("both corner numbers are read", read([("32", 95, 40, 100)], [("33", 95, 40, 100)]), {"left": 32, "right": 33})
    check("one number only", read([], [("33", 95, 40, 100)]), {"left": None, "right": 33})
    check("a blank page", read([], []), {"left": None, "right": None})
    check("a year in a corner is not a page number", read([("2022", 95, 40, 100)], []), {"left": None, "right": None})
    check("a shaky reading is ignored", read([("32", 40, 40, 100)], []), {"left": None, "right": None})
    check("zero is not a page number", read([("0", 95, 40, 100)], []), {"left": None, "right": None})
    check("the most confident reading in a corner wins over a speck",
          read([], [("5", 72, 10, 100), ("34", 96, 200, 100)]), {"left": None, "right": 34})
    # a number sitting beside a name in the corner is a list number, not a page number
    words = pd.DataFrame({"text": ["ADEYEMI"], "conf": [95.0], "left": [60.0], "top": [1620.0],
                          "width": [120.0], "height": [24.0]})
    # the page's own OCR usually read the number too: it is in `words`, and must not make itself non-isolated
    own = pd.DataFrame({"text": ["33"], "conf": [95.0], "left": [1120.0], "top": [1625.0], "width": [25.0], "height": [15.0]})
    check("the number's own OCR word does not count against it",
          read([], [("33", 95, (1120 - 893) * 4, (1625 - 1431) * 4)], own), {"left": None, "right": 33})
    covered = pd.DataFrame({"text": ["ADEYEMI"], "conf": [95.0], "left": [1100.0], "top": [1620.0], "width": [60.0], "height": [24.0]})
    check("...but a name sitting over it still does",
          read([], [("33", 95, (1120 - 893) * 4, (1625 - 1431) * 4)], covered), {"left": None, "right": None})
    check("a number beside a name is not a page number",
          read([("7", 95, 0, (1620 - 1431) * 4)], [], words), {"left": None, "right": None})
    sizes = []
    pages.pytesseract.image_to_data = lambda big, **k: (sizes.append(big.size), frame())[1]
    try:
        pages.corner_numbers(Image.new("RGB", (1000, 2000), "white"))
    finally:
        pages.pytesseract.image_to_data = real
    check("each corner is the outer quarter of the page, bottom 15%", sizes, [(1000, 1200)] * 2)
    check("a failing OCR call leaves the corner empty",
          (lambda: (setattr(pages.pytesseract, "image_to_data",
                            lambda *a, **k: (_ for _ in ()).throw(pages.pytesseract.TesseractError(1, "boom"))),
                    pages.corner_numbers(Image.new("RGB", (1191, 1684), "white")))[1])(),
          {"left": None, "right": None})
    pages.pytesseract.image_to_data = real


def test_detect_banners_footer_guard_and_noise_clustering():
    slogan = "THE BEST UNIVERSITY OF TECHNOLOGY IN NIGERIA"
    H = 1000

    def doc(texts, top):
        return [((800, H), [Line(text=t, top=top, bottom=top + 20, left=100, column_index=0)]) for t in texts]

    key = _banner_key(slogan)
    # classify_line reads this slogan as a department/course header, which
    # _detect_banners normally exempts as real content - but at 91% down the
    # page it's a footer, and a header field never sits there
    check("a header-looking slogan in the footer band is still a banner",
          _detect_banners(doc([slogan] * 10, top=910)), {key})
    check("the same text near the top of the page keeps the header exemption",
          _detect_banners(doc([slogan] * 10, top=100)), set())

    # OCR glued a different stray letter or two onto the same footer on each
    # page, so no single exact key reaches the frequency threshold alone
    noisy = [slogan, "ih " + slogan, "il " + slogan, "a " + slogan, "ok " + slogan, "xg " + slogan] * 2
    check("noisy variants of one footer are clustered into a single banner",
          _detect_banners(doc(noisy, top=910)), {key})

    check("text that varies on every page is never a banner",
          _detect_banners(doc([f"UNRELATED LINE NUMBER {chr(65 + i) * 7}" for i in range(10)], top=910)), set())


def main():
    for t in (test_name_cleaning, test_wordlike, test_line_grouping,
              test_batch_file_order, test_classification, test_split_name_band_merge, test_adaptive_zoom,
              test_dewarp_only_fires_on_real_keystone, test_number_column_bands, test_wrapped_name_merge, test_cross_file_dedupe, test_repeats_across_pages, test_overlapping_files, test_non_name_lines, test_confidence_scoring, test_corrections_diff_and_mining,
              test_review_rows_flag_and_carry_image_path, test_looks_isolated, test_resolve_page_numbers,
              test_plain_page_number, test_banner_fused_candidate_line, test_reread_banner_fused_digit,
              test_oval_badge_page_number, test_drop_repeated_readings, test_discard_repeated_readings_updates_page_meta,
              test_detect_banners_footer_guard_and_noise_clustering,
              test_audit_corner_numbers, test_two_number_pages_are_held_for_review,
              test_corner_numbers_reads_both_corners):
        t()
    if _failures:
        print("\n".join(_failures))
        print(f"{len(_failures)} FAILED")
        sys.exit(1)
    print("all unit tests passed")


if __name__ == "__main__":
    main()
