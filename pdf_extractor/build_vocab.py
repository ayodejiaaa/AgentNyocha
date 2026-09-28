"""Builds two word-frequency vocabularies straight from the verified
Canonical Training Data (535,288 real records) rather than guessing at
patterns:

  - institutional vocabulary: words that occur in faculty/department/
    course/degree/grade/programme text
  - name vocabulary: words that occur in the name field

The point isn't fuzzy-matching whole phrases against a lookup table (OCR
noise makes exact phrases unreliable, and new courses/spellings keep
appearing) - it's this: a word like DEPARTMENT, FACULTY, ENGINEERING,
ADMINISTRATION essentially never appears inside a person's name, and a
word like FAVOUR, PEACE, MERCY, OLUWASEUN essentially never appears inside
a department name. That asymmetry, measured empirically across half a
million real records instead of assumed, is a much sturdier signal than
comma-presence or line shape.

Run once (or whenever the training data changes):
    python -m pdf_extractor.build_vocab
"""

import csv
import json
import re
from collections import Counter
from pathlib import Path

MASTER_CSV = Path(__file__).parent.parent / "Canonical Training Data" / "akowe_canonical_master.csv"
OUT_PATH = Path(__file__).parent / "vocab.json"

INSTITUTIONAL_FIELDS = ["faculty", "department", "course", "degree", "grade", "programme"]
NAME_FIELDS = ["name"]

TOKEN_RE = re.compile(r"[A-Z]{2,}")


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.upper())


def main():
    institutional_counts = Counter()
    name_counts = Counter()

    with open(MASTER_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            for field in INSTITUTIONAL_FIELDS:
                institutional_counts.update(tokenize(row.get(field, "") or ""))
            for field in NAME_FIELDS:
                name_counts.update(tokenize(row.get(field, "") or ""))
            if i % 100000 == 0:
                print(f"  ...{i} rows")

    print(f"Distinct institutional tokens: {len(institutional_counts)}")
    print(f"Distinct name tokens: {len(name_counts)}")

    # A word is a strong institutional signal if it appears meaningfully
    # often in institutional fields and rarely (or never) in names, and
    # vice versa. Words that show up a lot on both sides (e.g. common
    # connector words, or a genuine coincidence like a surname that's also
    # a place name) are left out rather than forced into either bucket.
    institutional_words = {}
    name_words = {}
    all_tokens = set(institutional_counts) | set(name_counts)
    for tok in all_tokens:
        i_count = institutional_counts.get(tok, 0)
        n_count = name_counts.get(tok, 0)
        total = i_count + n_count
        if total < 5:
            continue
        ratio_institutional = i_count / total
        if ratio_institutional >= 0.95 and i_count >= 5:
            institutional_words[tok] = i_count
        elif ratio_institutional <= 0.02 and n_count >= 5:
            name_words[tok] = n_count

    print(f"Strong institutional-only words: {len(institutional_words)}")
    print(f"Strong name-only words: {len(name_words)}")
    print("\nSample institutional words:", sorted(institutional_words, key=institutional_words.get, reverse=True)[:30])
    print("\nSample name words:", sorted(name_words, key=name_words.get, reverse=True)[:30])

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "institutional_words": institutional_words,
            "name_words": name_words,
        }, f, indent=1)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
