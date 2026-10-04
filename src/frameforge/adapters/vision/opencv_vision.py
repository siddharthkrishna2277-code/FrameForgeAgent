"""OpenCV vision adapter.

Everything here is chosen to be cheap on an i5-8250U with integrated graphics:

* Templates are matched in grayscale with ``TM_CCOEFF_NORMED``, which is invariant to
  linear brightness change - the property that lets one authored template survive a
  game's brightness slider, HDR toggle, and time-of-day lighting.
* Regions are converted to grayscale *once* per call and reused, because re-converting
  the same 1920x1080 ROI three times is how a perception step turns into 60 ms.
* Multiscale matching is opt-in per anchor (``scales``), because a pyramid over a large
  ROI costs real time and most HUD elements are authored at a known scale.

No neural network runs here, by design and by hardware constraint.
"""

from __future__ import annotations

import cv2
import numpy as np

from frameforge.ports.geometry import Rect
from frameforge.ports.vision import (
    ChangeScore,
    ColorProbe,
    ColorProbeResult,
    TemplateMatch,
    VisionPort,
)

#: Below this variance a region is considered flat/static. Used to distinguish "the game
#: is idle" from "capture has frozen".
ACTIVITY_EPSILON = 1e-4


class OpenCvVision:
    """Deterministic image measurement via OpenCV."""

    name = "opencv"

    def __init__(self, *, grayscale_templates: bool = True) -> None:
        self._gray_templates = grayscale_templates

    # ------------------------------------------------------------------ conversion

    def to_gray(self, image: np.ndarray) -> np.ndarray:
        if image.ndim == 2:
            return image
        if image.shape[2] == 4:
            image = image[:, :, :3]
        return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    def encode_png(self, image: np.ndarray) -> bytes:
        ok, buf = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not ok:
            msg = "PNG encode failed"
            raise RuntimeError(msg)
        return buf.tobytes()

    # ------------------------------------------------------------------- matching

    def match_template(
        self,
        haystack: np.ndarray,
        template: np.ndarray,
        threshold: float = 0.8,
        method: str = "ccoeff_normed",
    ) -> TemplateMatch:
        """Locate ``template`` in ``haystack``; returns score even when below threshold.

        The score is always returned, never just a boolean, because a tier-0 policy
        frequently wants to *react to the degree* of a match (0.99 = certain menu,
        0.85 = probably menu, 0.6 = noise) rather than to a hard yes/no.
        """
        if haystack.size == 0 or template.size == 0:
            return TemplateMatch(score=0.0, rect=None, template_size=(0, 0), method=method)
        if template.shape[0] > haystack.shape[0] or template.shape[1] > haystack.shape[1]:
            return TemplateMatch(
                score=0.0,
                rect=None,
                template_size=(template.shape[1], template.shape[0]),
                method=method,
            )

        hay = self.to_gray(haystack) if self._gray_templates else haystack
        tpl = self.to_gray(template) if self._gray_templates else template

        cv_method = {
            "ccoeff_normed": cv2.TM_CCOEFF_NORMED,
            "ccorr_normed": cv2.TM_CCORR_NORMED,
            "sqdiff_normed": cv2.TM_SQDIFF_NORMED,
        }.get(method, cv2.TM_CCOEFF_NORMED)

        try:
            result = cv2.matchTemplate(hay, tpl, cv_method)
        except cv2.error:
            # A degenerate template (uniform colour, or larger than the haystack in one
            # axis) makes CCOEFF_NORMED fail. That means "no information", not "crash":
            # perception degradation must never end a run.
            return TemplateMatch(
                score=0.0,
                rect=None,
                template_size=(tpl.shape[1], tpl.shape[0]),
                method=method,
            )

        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
        score = float(max_val)
        if method == "sqdiff_normed":
            score = 1.0 - float(max_val)
        if score < threshold:
            return TemplateMatch(score=score, rect=None, template_size=(tpl.shape[1], tpl.shape[0]), method=method)
        rect = Rect(int(max_loc[0]), int(max_loc[1]), int(tpl.shape[1]), int(tpl.shape[0]))
        return TemplateMatch(
            score=score, rect=rect, template_size=(tpl.shape[1], tpl.shape[0]), method=method
        )

    def match_templates(
        self, haystack: np.ndarray, templates: list[np.ndarray], threshold: float = 0.8
    ) -> list[TemplateMatch]:
        if not templates:
            return []
        hay = self.to_gray(haystack)
        results: list[TemplateMatch] = []
        for tpl in templates:
            t = self.to_gray(tpl)
            if t.shape[0] > hay.shape[0] or t.shape[1] > hay.shape[1]:
                results.append(TemplateMatch(0.0, None, (t.shape[1], t.shape[0])))
                continue
            res = cv2.matchTemplate(hay, t, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _ml, loc = cv2.minMaxLoc(res)
            if mx < threshold:
                results.append(TemplateMatch(float(mx), None, (t.shape[1], t.shape[0])))
            else:
                results.append(
                    TemplateMatch(
                        float(mx), Rect(int(loc[0]), int(loc[1]), int(t.shape[1]), int(t.shape[0]))
                    )
                )
        return results

    # ------------------------------------------------------------------- difference

    def diff(self, a: np.ndarray, b: np.ndarray) -> ChangeScore:
        """Fraction of pixels that differ meaningfully.

        The threshold of 8/255 exists because lossy video and screen scaling produce a
        little per-pixel noise on *identical* content; a strict comparison would report
        "changed" forever and the verifier would never trust a negative assertion.
        """
        if a.size == 0 or b.size == 0:
            return ChangeScore(score=0.0, method="empty")
        if a.shape != b.shape:
            return ChangeScore(score=1.0, method="shape_mismatch",
                               detail={"a": float(a.size), "b": float(b.size)})
        d = cv2.absdiff(a, b)
        gray = self.to_gray(d) if d.ndim == 3 else d
        frac = float((gray > 8).mean())
        return ChangeScore(
            score=frac,
            method="absdiff_fraction",
            detail={"mean_abs": float(gray.mean()), "max_abs": float(gray.max())},
        )

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Normalised similarity in [0, 1].

        Uses ``cv2.matchTemplate`` on self against a same-size haystack rather than
        SSIM proper: it is ~8x faster, and at our use (did this ROI change meaningfully)
        it discriminates equally well.
        """
        if a.size == 0 or b.size == 0 or a.shape != b.shape:
            return 0.0
        hay = self.to_gray(a)
        tpl = self.to_gray(b)
        if tpl.shape[0] > hay.shape[0] or tpl.shape[1] > hay.shape[1]:
            return 0.0
        res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, _loc = cv2.minMaxLoc(res)
        return max(0.0, float(mx))

    # --------------------------------------------------------------------- colour

    def probe_color(
        self, image: np.ndarray, probe: ColorProbe, rect: Rect | None = None
    ) -> ColorProbeResult:
        """Fraction of pixels matching an HSV tolerance window.

        HSV rather than RGB, deliberately: games apply colour grading, gamma and
        post-processing, so an RGB distance threshold is far too brittle for a real
        capture. Hue is compared on the 0..360 circle so red-vs-magenta wrap is correct.
        """
        if image.size == 0:
            return ColorProbeResult(ratio=0.0, matched=False)
        roi = image
        if rect is not None:
            r = rect.clamp_to(Rect(0, 0, image.shape[1], image.shape[0]).size)
            roi = image[r.y : r.bottom, r.x : r.right]
            if roi.size == 0:
                return ColorProbeResult(ratio=0.0, matched=False)
        hsv = cv2.cvtColor(cv2.cvtColor(roi, cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2HSV).astype(np.int16)
        h, s, v = hsv[:, :, 0] * 2.0, hsv[:, :, 1], hsv[:, :, 2]  # OpenCV hue is 0..179 -> 0..358

        dh = np.abs(h - probe.hue)
        dh = np.minimum(dh, 360.0 - dh)  # circular distance
        mask = (
            (dh <= probe.hue_tol)
            & (np.abs(s - probe.saturation) <= probe.sat_tol)
            & (np.abs(v - probe.value) <= probe.val_tol)
        )
        ratio = float(mask.mean())
        return ColorProbeResult(
            ratio=ratio,
            matched=ratio >= probe.min_ratio,
            mean_rgb=tuple(int(x) for x in roi.reshape(-1, 3).mean(axis=0)),  # type: ignore[arg-type]
            mean_hsv=(float(h.mean()), float(s.mean()), float(v.mean())),
        )

    # -------------------------------------------------------------------- activity

    def region_activity(self, image: np.ndarray, rect: Rect | None = None) -> float:
        """Cheap "is anything moving here" signal in [0, 1].

        Gradient energy rather than variance: a loading spinner moves (high gradient
        energy, low pixel variance), while a static menu with text does not. Variance
        alone would call both equally busy.
        """
        if image.size == 0:
            return 0.0
        roi = image
        if rect is not None:
            r = rect.clamp_to(Rect(0, 0, image.shape[1], image.shape[0]).size)
            roi = image[r.y : r.bottom, r.x : r.right]
            if roi.size == 0:
                return 0.0
        gray = self.to_gray(roi)
        if gray.shape[0] < 2 or gray.shape[1] < 2:
            return 0.0
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        magnitude = cv2.magnitude(gx, gy)
        return float(np.clip(magnitude.mean() / 255.0, 0.0, 1.0))

    def is_black(self, image: np.ndarray, threshold: float = 3.0) -> bool:
        if image.size == 0:
            return True
        return float(self.to_gray(image).mean()) < threshold

    def downscale(self, image: np.ndarray, max_dimension: int) -> np.ndarray:
        h, w = image.shape[:2]
        if max(h, w) <= max_dimension:
            return image
        scale = max_dimension / max(h, w)
        return cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)


__all__ = ["ACTIVITY_EPSILON", "OpenCvVision"]
