"""Perspective correction for photographed (non-flatbed) pages.

Akowe's real workflow strongly prefers flatbed scans over photographs (see
README's schema note: "we will try to only provide flatbed scans, rather
than photographed") - this module exists as a safety net for the
photographed pages that slip through anyway, not as a primary pipeline
stage that runs at full strength on every page.

That shapes the design: a flatbed scan's content fills the frame edge to
edge, with no visible page boundary to find - there's nothing to correct,
and there's nothing there to mistakenly correct either. A photograph, by
contrast, usually shows the page as a distinct rectangle (mildly rotated,
or trapezoidal from a non-perpendicular camera angle) against a
background/desk/edge of frame. So this only acts when it finds a large,
confidently page-shaped quadrilateral that's ALSO meaningfully
non-rectangular - anything murkier (the flatbed-scan case, or a photo
already shot close to head-on) is left alone. Mis-firing on a good flatbed
scan - warping it because some internal contour (a table border, a dense
text block) got mistaken for the page edge - would be worse than doing
nothing, so this errs heavily toward inaction.
"""

import cv2
import numpy as np
from PIL import Image

_MIN_AREA_FRAC = 0.5    # candidate quad must cover at least this much of the frame
_MIN_SKEW_FRAC = 0.015  # corner deviation from a perfect rectangle (as a fraction of
                         # the shorter image dimension) needed to bother correcting
# Opposite sides of the quad must differ in length by at least this fraction
# before it counts as genuine perspective (keystone) distortion. Measured on
# real flatbed scans the detected quad is an INNER RECTANGLE - a page frame or
# content border - with opposite sides within 0.05-2.3% of each other, only
# offset from the image corners and tilted 1-2 degrees. Warping that is not a
# correction, it's a distortion: it fired on ~19% of a whole corpus run
# (1,007 of 5,381 pages) and, on one LASU page, shifted the geometry enough
# to split a name's last given name into a separate record. A photographed
# page seen off-axis is a real trapezoid (a synthetic 15-degree case measures
# ~8%), so a 4% threshold separates the two.
_MIN_KEYSTONE = 0.04


def _order_points(pts: np.ndarray) -> np.ndarray:
    """Orders 4 points as top-left, top-right, bottom-right, bottom-left,
    regardless of the order cv2 found them in."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _find_page_quad(gray: np.ndarray) -> np.ndarray | None:
    """Looks for the largest, sufficiently large 4-sided contour - the
    photographed page's own edge against a background. Returns None if
    nothing plausible is found, which is the expected/common outcome for a
    flatbed scan (content fills the frame - no boundary contour to find)."""
    h, w = gray.shape[:2]
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    frame_area = h * w
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(contour) < frame_area * _MIN_AREA_FRAC:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            return approx.reshape(4, 2).astype("float32")
    return None


def _keystone(quad: np.ndarray) -> float:
    """Largest relative difference between opposite sides: 0 for a
    rectangle (however tilted or offset), growing as the quad becomes a
    trapezoid."""
    tl, tr, br, bl = _order_points(quad)
    n = np.linalg.norm
    wt, wb, hl, hr = n(tr - tl), n(br - bl), n(bl - tl), n(br - tr)
    return float(max(abs(wt - wb) / max(wt, wb, 1.0), abs(hl - hr) / max(hl, hr, 1.0)))


def _needs_correction(quad: np.ndarray, img_w: int, img_h: int) -> bool:
    """Only warp when the quad is BOTH offset from the image's own rectangle
    (otherwise it's just the frame itself, or a photo already shot close to
    head-on) AND genuinely keystoned (see _MIN_KEYSTONE - a merely offset or
    slightly tilted rectangle is an inner page border, not a photographed
    page, and warping it does harm)."""
    rect = _order_points(quad)
    expected_corners = np.array([[0, 0], [img_w, 0], [img_w, img_h], [0, img_h]],
                                 dtype="float32")
    tol = _MIN_SKEW_FRAC * min(img_w, img_h)
    deviations = np.linalg.norm(rect - expected_corners, axis=1)
    return bool(deviations.max() > tol and _keystone(quad) > _MIN_KEYSTONE)


def correct_perspective(img: Image.Image) -> tuple[Image.Image, bool]:
    """Best-effort perspective correction for a photographed page.

    Returns (possibly-corrected image, was_corrected). Leaves the image
    untouched (was_corrected=False) whenever no confidently page-shaped,
    meaningfully skewed quadrilateral is found - see module docstring for
    why that's the deliberately conservative default rather than a
    fallback for a failed detection."""
    arr = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]

    quad = _find_page_quad(gray)
    if quad is None or not _needs_correction(quad, w, h):
        return img, False

    rect = _order_points(quad)
    tl, tr, br, bl = rect
    max_width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    max_height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if max_width <= 0 or max_height <= 0:
        return img, False

    dst = np.array([
        [0, 0],
        [max_width - 1, 0],
        [max_width - 1, max_height - 1],
        [0, max_height - 1],
    ], dtype="float32")

    matrix = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(arr, matrix, (max_width, max_height))
    return Image.fromarray(warped), True
