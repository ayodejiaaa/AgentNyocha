"""Two-page-spread splitting.

Some of Akowe's real source scans capture two physical book pages in a
single image (UNILORIN's booklets are the case that surfaced this during
the manual PDF exploration) - two portrait pages side by side, doubling
the image width. Every downstream stage (column detection, reading order,
page-number detection) assumes one image is one physical page, so a spread
has to be split before any of that runs - otherwise its two independent
name lists get read as if they were two columns of one continuous list,
interleaving unrelated departments/rows the same way a page with several
genuine side-by-side sections would if reconstruct_reading_order didn't
treat them separately.

This must run on an already-orientation-corrected image (0/90/180/270 via
orientation.py), not before. A two-page spread is landscape-shaped (wider
than tall) in ANY of its 4 possible raw scan rotations - exactly the same
silhouette a single page rotated 90 degrees has before correction - so
checking aspect ratio ahead of orientation correction would misfire on a
plain sideways single page and try to split it in half. After correction,
a genuine spread stays landscape-shaped (it's two portrait pages side by
side; rotation can't change that), while a genuine single page becomes
properly portrait - that's what makes the aspect-ratio check reliable
here and not earlier in the pipeline.
"""

import numpy as np
from PIL import Image

_MIN_ASPECT = 1.15  # width/height beyond which an upright image is treated
                     # as a candidate spread rather than a normal
                     # taller-than-wide single page


def is_spread(img: Image.Image, min_aspect: float = _MIN_ASPECT) -> bool:
    """True if an (already orientation-corrected) image is wide enough to
    plausibly be two side-by-side pages rather than one."""
    w, h = img.size
    if h == 0:
        return False
    return (w / h) >= min_aspect


def _find_split_x(gray: np.ndarray, search_frac: float = 0.15, ink_threshold: int = 200) -> int:
    """Finds the best vertical split line near the horizontal center: the
    column with the least ink (dark pixels) in a window around the middle
    - normally the blank gutter/margin between two physical pages, since
    real printed content sits within each page's own margins on either
    side of it, not spanning across the binding."""
    h, w = gray.shape[:2]
    center = w // 2
    window = max(1, int(w * search_frac))
    lo, hi = max(0, center - window), min(w, center + window)
    dark = gray < ink_threshold
    col_ink = dark.sum(axis=0)
    search_region = col_ink[lo:hi]
    if search_region.size == 0:
        return center
    return lo + int(np.argmin(search_region))


def split_spread(img: Image.Image) -> list[Image.Image]:
    """Returns [left_half, right_half] if `img` looks like a two-page
    spread (see is_spread), else [img] unchanged - a single normal page,
    the common case, must be a true no-op."""
    if not is_spread(img):
        return [img]
    gray = np.array(img.convert("L"))
    split_x = _find_split_x(gray)
    if split_x <= 0 or split_x >= img.width:
        return [img]
    left = img.crop((0, 0, split_x, img.height))
    right = img.crop((split_x, 0, img.width, img.height))
    return [left, right]
