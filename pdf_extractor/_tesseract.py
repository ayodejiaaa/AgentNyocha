"""Locates the Tesseract executable for pytesseract. Imported for its side
effect by every module that calls Tesseract (ocr.py, orientation.py), so
that no module depends on some other one having been imported first.

Fresh Tesseract installs on Windows aren't always on PATH within this
process; point pytesseract at the known install location if the bare
command isn't resolvable.
"""

import shutil

import pytesseract

if shutil.which("tesseract") is None:
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
