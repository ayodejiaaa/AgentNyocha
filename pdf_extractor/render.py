"""PDF page -> image rendering. Uses PyMuPDF, which bundles its own PDF
renderer, so unlike the artifact-preview tooling used earlier in this
project's exploration, this needs no external poppler/pdftoppm install.
"""

import fitz  # PyMuPDF
from PIL import Image


def page_count(pdf_path: str) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def render_page(pdf_path: str, page_index: int, zoom: float = 2.0) -> Image.Image:
    """Render a single 0-indexed PDF page to a PIL Image at the given zoom
    factor (2.0 ~= 144 DPI, a good default for OCR on scanned booklets)."""
    with fitz.open(pdf_path) as doc:
        page = doc.load_page(page_index)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        mode = "RGB" if pix.alpha == 0 else "RGBA"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        if mode == "RGBA":
            img = img.convert("RGB")
        return img
