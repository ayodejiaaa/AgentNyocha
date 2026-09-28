"""Manual smoke test: render -> orient -> OCR -> reading order, on a page
we've already hand-verified during the earlier PDF exploration (LASU 2005,
PDF page-index 1, printed page 21). Expected result, confirmed against the
ground-truth Excel: left column reads 26-71 top to bottom, then right
column reads 72-118 top to bottom, once the 180-degree rotation is undone.
"""

import sys
from pdf_extractor import render, orientation, ocr, columns

PDF_PATH = "pdf_extractor/samples/lasu2005.pdf"
PAGE_INDEX = 1  # 0-indexed; this is the page we rendered by hand earlier


def main():
    print(f"Rendering page index {PAGE_INDEX}...")
    img = render.render_page(PDF_PATH, PAGE_INDEX, zoom=2.0)
    print(f"Page size: {img.size}")

    print("Detecting orientation...")
    corrected, rotated_by = orientation.correct_orientation(img)
    print(f"Rotated by: {rotated_by} degrees. New size: {corrected.size}")

    print("Running OCR...")
    words = ocr.ocr_words(corrected)
    print(f"Detected {len(words)} words")

    print("Reconstructing reading order...")
    lines = columns.reconstruct_reading_order(words, corrected.size[0])
    print(f"Detected {len(set(l.column_index for l in lines))} column(s), {len(lines)} lines total")

    print("\n--- First 15 lines in reconstructed reading order ---")
    for line in lines[:15]:
        print(f"  [col {line.column_index}] {line.text}")

    print("\n--- Last 15 lines ---")
    for line in lines[-15:]:
        print(f"  [col {line.column_index}] {line.text}")


if __name__ == "__main__":
    main()
