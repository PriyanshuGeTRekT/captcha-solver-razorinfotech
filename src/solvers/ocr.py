from __future__ import annotations

import ast
import asyncio
import logging
import re
import time
from collections import Counter
from urllib.parse import urlparse

import numpy as np
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
from scipy import ndimage
import cv2

from src.models import CaptchaChallenge, CaptchaSolution, CaptchaType
from src.solvers.base import BaseSolver, SolverRegistry
from src.utils.image import decode_image
from src.utils.model_manager import ModelManager

logger = logging.getLogger("captcha_solver")

_TESSERACT_AVAILABLE = False
_TESSERACT_PATH = ""


def _init_tesseract():
    global _TESSERACT_AVAILABLE, _TESSERACT_PATH
    if _TESSERACT_AVAILABLE:
        return True
    try:
        import pytesseract
        from pathlib import Path as _Path
        paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for p in paths:
            if _Path(p).exists():
                pytesseract.pytesseract.tesseract_cmd = p
                _TESSERACT_PATH = p
                _TESSERACT_AVAILABLE = True
                logger.info(f"Tesseract found at {p}")
                return True
    except ImportError:
        pass
    return False


def _safe_eval_math(expr: str) -> int | None:
    if not expr or not expr.strip():
        return None
    expr = expr.strip()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    _ALLOWED_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)

    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp,)):
            if not isinstance(node.op, _ALLOWED_OPS):
                return None
        elif isinstance(node, (ast.UnaryOp,)):
            if not isinstance(node.op, (ast.UAdd, ast.USub)):
                return None
        elif isinstance(node, (ast.Constant, ast.Num, ast.Expression)):
            continue
        elif isinstance(node, _ALLOWED_OPS + (ast.UAdd, ast.USub)):
            continue
        else:
            return None

    try:
        result = eval(compile(tree, "<math>", "eval"))
        if isinstance(result, (int, float)):
            return int(result)
    except (ZeroDivisionError, TypeError, ValueError):
        return None
    return None


def _vote_math_answers(texts, min_result=0, max_result=200):
    """Vote on the evaluated INTEGER answer across many noisy OCR reads.

    For each raw OCR string: normalize common confusables, tokenize into
    numbers/operators, then evaluate every 'num op num' and 'num op num op num'
    sub-expression through the sandboxed _safe_eval_math. Each in-range result
    casts one vote. Consensus on the evaluated integer is far more robust to
    per-read digit/operator noise than committing to any single OCR string.

    Returns [(result_int, vote_count, example_expr), ...] sorted by votes desc.
    """
    from collections import Counter
    votes: Counter = Counter()
    examples: dict[int, str] = {}

    def _cast(expr: str):
        r = _safe_eval_math(expr)
        if r is not None and min_result <= r <= max_result:
            votes[r] += 1
            if r not in examples or len(expr) < len(examples[r]):
                examples[r] = expr

    for raw in texts:
        if not raw:
            continue
        s = str(raw).replace("·", "*").replace("×", "*").replace("÷", "/")
        s = re.sub(r"(\d)\s*[xX]\s*(\d)", r"\1*\2", s)
        for a, b in (("l", "1"), ("I", "1"), ("O", "0"), ("o", "0"),
                     ("S", "5"), ("s", "5"), ("B", "8")):
            s = s.replace(a, b)
        s = re.sub(r"[^0-9+\-*/ ]", " ", s)
        tokens = re.findall(r"\d+|[+\-*/]", s)
        n = len(tokens)
        for i in range(n):
            if not tokens[i].isdigit():
                continue
            if i + 2 < n and tokens[i + 1] in "+-*/" and tokens[i + 2].isdigit():
                _cast(tokens[i] + tokens[i + 1] + tokens[i + 2])
                if i + 4 < n and tokens[i + 3] in "+-*/" and tokens[i + 4].isdigit():
                    _cast(tokens[i] + tokens[i + 1] + tokens[i + 2]
                          + tokens[i + 3] + tokens[i + 4])

    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    return [(val, cnt, examples.get(val, "")) for val, cnt in ranked]


# ---------------------------------------------------------------------------
# Preprocessing variants
# ---------------------------------------------------------------------------

def _binarize_simple(img: Image.Image) -> Image.Image:
    gray = np.array(img.convert("L"))
    flat = sorted(gray.flatten())
    thresh = flat[int(len(flat) * 0.25)]
    binary = ((gray < thresh) * 255).astype(np.uint8)
    labeled, n_labels = ndimage.label(binary == 255)
    if n_labels > 0:
        sizes = ndimage.sum(binary == 255, labeled, range(n_labels + 1))
        mask = sizes > 2
        binary = (mask[labeled] * 255).astype(np.uint8)
    return Image.fromarray(binary)


def _binarize_adaptive(img: Image.Image) -> Image.Image:
    img_np = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    thresh = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    kernel = np.ones((2, 2), np.uint8)
    processed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    return Image.fromarray(processed)


def _binarize_denoise(img: Image.Image) -> Image.Image:
    img_np = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)

    denoised = cv2.bilateralFilter(gray, 9, 75, 75)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 1))
    h_lines = cv2.morphologyEx(cv2.bitwise_not(binary), cv2.MORPH_OPEN, h_kernel)
    binary = cv2.bitwise_or(binary, h_lines)

    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 15))
    v_lines = cv2.morphologyEx(cv2.bitwise_not(binary), cv2.MORPH_OPEN, v_kernel)
    binary = cv2.bitwise_or(binary, v_lines)

    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    return Image.fromarray(binary)


def _binarize_dark_text(img: Image.Image) -> Image.Image:
    """Isolate dark text from lighter/colored backgrounds using LAB color space."""
    img_np = np.array(img.convert("RGB"))
    lab = cv2.cvtColor(img_np, cv2.COLOR_RGB2LAB)
    l_channel = lab[:, :, 0]

    _, binary = cv2.threshold(l_channel, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    return Image.fromarray(binary)


def _binarize_clahe(img: Image.Image) -> Image.Image:
    """CLAHE + Otsu — good for noisy grid backgrounds (Klub-style captchas)."""
    gray = np.array(img.convert("L"))
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
    enhanced = clahe.apply(gray)
    _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return Image.fromarray(binary)


def _binarize_sauvola(img: Image.Image) -> Image.Image:
    """Sauvola-style local thresholding — good for uneven backgrounds."""
    gray = np.array(img.convert("L")).astype(np.float64)
    window = 15
    pad = window // 2

    padded = np.pad(gray, pad, mode='reflect')
    integral = padded.cumsum(axis=0).cumsum(axis=1)
    integral_sq = (padded ** 2).cumsum(axis=0).cumsum(axis=1)

    h, w = gray.shape
    y1, x1 = 0, 0
    y2, x2 = y1 + window, x1 + window

    rows = np.arange(h)
    cols = np.arange(w)
    Y1 = rows[:, None]
    X1 = cols[None, :]
    Y2 = Y1 + window
    X2 = X1 + window

    area = window * window
    s = integral[Y2, X2] - integral[Y1, X2] - integral[Y2, X1] + integral[Y1, X1]
    sq = integral_sq[Y2, X2] - integral_sq[Y1, X2] - integral_sq[Y2, X1] + integral_sq[Y1, X1]
    mean = s / area
    std = np.sqrt(np.clip(sq / area - mean ** 2, 0, None))

    k = 0.2
    R = 128.0
    thresh = mean * (1.0 + k * (std / R - 1.0))
    binary = ((gray < thresh) * 255).astype(np.uint8)

    return Image.fromarray(binary)


def _binarize_color_channel(img: Image.Image, channel: int = 0) -> Image.Image:
    """Extract a specific color channel and binarize. Channel: 0=R, 1=G, 2=B."""
    img_np = np.array(img.convert("RGB"))
    ch = img_np[:, :, channel]
    _, binary = cv2.threshold(ch, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return Image.fromarray(binary)


def _preprocess_high_contrast(img: Image.Image) -> Image.Image:
    """Heavy contrast + sharpening for faint text."""
    enhanced = ImageEnhance.Contrast(img).enhance(3.0)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(2.0)
    gray = enhanced.convert("L")
    return ImageOps.autocontrast(gray, cutoff=10)


def _remove_grid_lines(binary_np: np.ndarray) -> np.ndarray:
    """Remove horizontal and vertical grid lines from a binary image."""
    result = binary_np.copy()

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1))
    h_lines = cv2.morphologyEx(cv2.bitwise_not(result), cv2.MORPH_OPEN, h_kernel)
    result = cv2.bitwise_or(result, h_lines)

    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 20))
    v_lines = cv2.morphologyEx(cv2.bitwise_not(result), cv2.MORPH_OPEN, v_kernel)
    result = cv2.bitwise_or(result, v_lines)

    d1_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 1))
    np_diag = np.eye(15, dtype=np.uint8)
    d_lines = cv2.morphologyEx(cv2.bitwise_not(result), cv2.MORPH_OPEN, np_diag)
    result = cv2.bitwise_or(result, d_lines)

    return result


def _scale_up(img: Image.Image, factor: int) -> Image.Image:
    return img.resize((img.width * factor, img.height * factor), Image.LANCZOS)


def _binarize_blue_text(img: Image.Image) -> Image.Image:
    """Isolate blue text using HSV hue filtering — best for kazan captchas."""
    img_np = np.array(img.convert("RGB"))
    hsv = cv2.cvtColor(img_np, cv2.COLOR_RGB2HSV)
    lower = np.array([85, 40, 30])
    upper = np.array([150, 255, 255])
    mask = cv2.inRange(hsv, lower, upper)
    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return Image.fromarray(mask)


def _remove_colored_noise(img: Image.Image) -> np.ndarray:
    """Remove colored noise lines from math captchas, keeping dark text.
    Colored lines are dark in only 1-2 channels; real text is dark in ALL.
    Returns binary: text=0 (black), background=255 (white)."""
    img_np = np.array(img.convert("RGB"))
    r = img_np[:, :, 0].astype(np.int16)
    g = img_np[:, :, 1].astype(np.int16)
    b = img_np[:, :, 2].astype(np.int16)
    # Text is dark in ALL channels; colored noise is bright in at least one
    max_ch = np.maximum(np.maximum(r, g), b)
    min_ch = np.minimum(np.minimum(r, g), b)
    # Truly dark pixels: max channel < threshold (not bright in any channel)
    # Also filter out pixels where channels differ a lot (colored, not gray)
    text_mask = (max_ch < 150) & ((max_ch - min_ch) < 60)
    result = np.full(img_np.shape[:2], 255, dtype=np.uint8)
    result[text_mask] = 0
    kernel = np.ones((2, 2), np.uint8)
    result = cv2.morphologyEx(result, cv2.MORPH_CLOSE, kernel)
    return result


# ---------------------------------------------------------------------------
# Site hint extraction
# ---------------------------------------------------------------------------

def _get_site_hint(page_url: str) -> str:
    """Return a short site key for site-specific OCR tuning."""
    if not page_url:
        return ""
    try:
        host = urlparse(page_url).hostname or ""
    except Exception:
        host = ""
    if "kazan" in host or "top100lingua" in host:
        return "kazan"
    if "klubabstynenta" in host or "piekary" in host:
        return "klub"
    if "starwars" in host or "freakz" in host:
        return "starwars"
    if "ersterzug" in host:
        return "ersterzug"
    if "geini" in host:
        return "geini"
    return ""


# ---------------------------------------------------------------------------
# Character disambiguation
# ---------------------------------------------------------------------------

_CHAR_CONFUSIONS = {
    ("m", "n"): "m",
    ("rn", "m"): "m",
    ("cl", "d"): "d",
    ("vv", "w"): "w",
}


def _disambiguate_chars(text: str, candidates: list[str]) -> str:
    """Fix common OCR character confusions using cross-candidate voting."""
    if len(text) < 2 or not candidates:
        return text

    result = list(text)
    for i, ch in enumerate(result):
        votes = Counter()
        for cand in candidates:
            if i < len(cand):
                votes[cand[i]] += 1
        if votes:
            most_common_char, count = votes.most_common(1)[0]
            if count >= 2 and most_common_char != ch:
                result[i] = most_common_char

    return "".join(result)


class OCRSolver(BaseSolver):
    name = "ocr"

    def __init__(self, model_manager: ModelManager | None = None):
        self.model_manager = model_manager or ModelManager()

    def _try_math_eval(self, text: str) -> str | None:
        if not text:
            return None

        cleaned = text
        cleaned = cleaned.replace("×", "*")
        cleaned = cleaned.replace("÷", "/")
        cleaned = re.sub(r"(\d)[xX](\d)", r"\1*\2", cleaned)
        cleaned = cleaned.replace("=", "").replace(" ", "")

        has_digit = any(c.isdigit() for c in cleaned)
        has_operator = any(c in cleaned for c in "+-*/")
        if not (has_digit and has_operator):
            return None

        ocr_fixes = str.maketrans({
            "O": "0", "o": "0", "l": "1", "I": "1",
            "S": "5", "s": "5", "B": "8",
        })
        cleaned = cleaned.translate(ocr_fixes)

        if not re.match(r"^[\d+\-*/().]+$", cleaned):
            return None

        result = _safe_eval_math(cleaned)
        if result is not None:
            return str(result)
        return None

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return challenge.type == CaptchaType.IMAGE_CAPTCHA

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        start = time.time()
        try:
            img_data = challenge.extra.get("image_data", "")
            if not img_data:
                return CaptchaSolution(
                    type=CaptchaType.IMAGE_CAPTCHA,
                    success=False,
                    error="no image_data in challenge.extra",
                )

            img = decode_image(img_data)
            site_hint = _get_site_hint(challenge.page_url)
            captcha_hash = challenge.extra.get("captcha_hash")

            # Save captcha image for debugging
            try:
                from pathlib import Path as _P
                debug_dir = _P("screenshots/captcha_debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                ts = int(time.time())
                img.save(debug_dir / f"{site_hint or 'unknown'}_{ts}.png")
                logger.info(f"Captcha image saved: {site_hint}_{ts}.png ({img.width}x{img.height})")
            except Exception:
                pass

            loop = asyncio.get_event_loop()
            token, engine, conf = await loop.run_in_executor(
                None, self._solve_captcha, img, site_hint, captcha_hash
            )

            elapsed = (time.time() - start) * 1000
            logger.info(f"ocr solved via {engine}: '{token}' (conf={conf:.3f}, site={site_hint})")
            return CaptchaSolution(
                type=CaptchaType.IMAGE_CAPTCHA,
                token=token,
                solved_via=engine,
                attempts=1,
                elapsed_ms=elapsed,
                confidence=conf,
                success=bool(token),
            )
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            return CaptchaSolution(
                type=CaptchaType.IMAGE_CAPTCHA,
                success=False,
                error=str(e),
                attempts=1,
                elapsed_ms=elapsed,
            )

    def _get_site_allowlist(self, site_hint: str) -> str | None:
        """Return EasyOCR allowlist tuned for each site's captcha charset."""
        if site_hint == "klub":
            return "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if site_hint == "kazan":
            return "abcdefghijklmnopqrstuvwxyz"
        return None

    def _get_preprocessors(self, site_hint: str) -> list[tuple[str, Image.Image]]:
        """Return site-tuned preprocessing variants. Called per-image."""
        return []

    def _solve_captcha(self, img: Image.Image, site_hint: str = "", captcha_hash: int | None = None) -> tuple[str, str, float]:
        # Adaptive scale factors based on image size
        # Small images (<200px) need more upscaling; larger ones less
        if img.width < 150:
            scales = [4, 3, 2]
        elif img.width < 400:
            scales = [3, 2, 1]
        else:
            scales = [2, 1]

        if site_hint == "kazan":
            return self._solve_kazan(img, captcha_hash=captcha_hash)
        if site_hint == "klub":
            return self._solve_klub(img)

        simple_bin = _binarize_simple(img)
        adaptive_bin = _binarize_adaptive(img)
        denoise_bin = _binarize_denoise(img)
        dark_bin = _binarize_dark_text(img)
        high_contrast = _preprocess_high_contrast(img)

        candidates = []
        allowlist = self._get_site_allowlist(site_hint)

        preprocessed = [
            ("simple", simple_bin),
            ("adaptive", adaptive_bin),
            ("denoise", denoise_bin),
            ("dark", dark_bin),
        ]

        if site_hint == "klub":
            for label, bin_img in [("simple", simple_bin), ("denoise", denoise_bin)]:
                arr = np.array(bin_img)
                cleaned = _remove_grid_lines(arr)
                preprocessed.append((f"nogrid_{label}", Image.fromarray(cleaned)))
            try:
                sauvola = _binarize_sauvola(img)
                preprocessed.append(("sauvola", sauvola))
            except Exception:
                pass
            try:
                clahe_bin = _binarize_clahe(img)
                preprocessed.insert(0, ("clahe", clahe_bin))
            except Exception:
                pass

        if site_hint == "kazan":
            try:
                blue_text_bin = _binarize_blue_text(img)
                preprocessed.insert(0, ("blue_text", blue_text_bin))
            except Exception:
                pass

        if site_hint in ("kazan", "starwars", ""):
            for ch_idx, ch_name in [(0, "red"), (1, "green"), (2, "blue")]:
                try:
                    ch_bin = _binarize_color_channel(img, ch_idx)
                    preprocessed.append((f"ch_{ch_name}", ch_bin))
                except Exception:
                    pass

        for scale in scales:
            for label, binary in preprocessed:
                text, conf = self._try_easyocr(binary, scale, allowlist=allowlist)
                if text:
                    clean = self._clean_token(text)
                    if clean and len(clean) >= 2:
                        candidates.append((clean, conf + 0.01, f"easyocr_{label}_{scale}x"))

                t_text = self._try_tesseract(binary, scale, label, text, site_hint=site_hint)
                if t_text:
                    candidates.append((t_text, 0.55, f"tesseract_{label}_{scale}x"))

        for scale in scales[:2]:
            text, conf = self._try_easyocr(high_contrast, scale, allowlist=allowlist)
            if text:
                clean = self._clean_token(text)
                if clean and len(clean) >= 2:
                    candidates.append((clean, conf, f"easyocr_hicon_{scale}x"))

        for scale in scales:
            text, conf = self._try_easyocr(img, scale, allowlist=allowlist)
            if text:
                clean = self._clean_token(text)
                if clean and len(clean) >= 2:
                    candidates.append((clean, conf, f"easyocr_rgb_{scale}x"))

        per_char_text = self._try_perchar_ocr(img, allowlist=allowlist)
        if per_char_text and len(per_char_text) >= 3:
            candidates.append((per_char_text, 0.65, "perchar"))

        # ddddocr: OCR model purpose-built for distorted-text captchas.
        if site_hint in ("ersterzug", "geini", ""):
            dd_clean = self._clean_token(self._try_ddddocr(img))
            if dd_clean and len(dd_clean) >= 2:
                candidates.append((dd_clean, 0.72, "ddddocr"))
                # ersterzug/geini are Ilch CMS distorted-text captchas with NO
                # server hash to validate against. ddddocr is by far the most
                # accurate engine on them; our EasyOCR/consensus pool otherwise
                # OVERRIDES ddddocr's correct reads with a wrong blend (verified
                # live: consensus turned 'dobaoae'->'doloae', 'gajah'->'gajan',
                # 'wuue6'->'myea'). Trust ddddocr as the primary answer; the 4x
                # captcha-refresh retry loop covers the cases it misses.
                if site_hint in ("ersterzug", "geini") and len(dd_clean) >= 4:
                    logger.info(f"ddddocr primary for {site_hint}: '{dd_clean}'")
                    return dd_clean, "ddddocr_primary", 0.85

        # Klub: CLAHE gives correct chars but with edge noise — extract 5-char core
        if site_hint == "klub":
            try:
                clahe_bin = _binarize_clahe(img)
                for cl_scale in [6, 4]:
                    text, conf = self._try_easyocr(clahe_bin, cl_scale, allowlist=allowlist)
                    if text:
                        clean = self._clean_token(text)
                        if clean and len(clean) == 5:
                            candidates.append((clean, conf + 0.15, f"clahe_exact_{cl_scale}x"))
                        elif clean and len(clean) > 5:
                            for start in range(len(clean) - 4):
                                sub = clean[start:start + 5]
                                candidates.append((sub, conf + 0.05, f"clahe_trim{start}_{cl_scale}x"))
            except Exception:
                pass

        # Math OCR: only for sites that use math captchas
        if site_hint == "starwars" or site_hint == "":
            math_text = self._try_math_ocr(img)
            if math_text:
                candidates.append((math_text, 0.6, "math_ocr"))

        if site_hint == "starwars":
            # Cross-validation dispatch: run BOTH component analysis and denoised
            # full-image OCR, then vote on the evaluated INTEGER answer. Return
            # high confidence only when the two paths AGREE; otherwise prefer the
            # better-supported reading and lower confidence (<0.85) so the caller
            # can re-roll the captcha instead of submitting a likely-wrong answer.

            # (a) Component-based analysis — collect candidate, do NOT early-return
            comp_answer = None
            for m_scale in [6, 5, 4, 3]:
                m_result = self._try_math_ocr_enhanced(img, m_scale)
                if m_result:
                    comp_answer = m_result
                    break

            # (b) Whole-image OCR over several binarizations — collect raw reads to
            # build a vote pool. The denoised image often reads nothing, so we also
            # add fixed-threshold binarizations + inverses; different binarizations
            # misread different glyphs, but the CORRECT arithmetic answer recurs
            # across them and wins the vote. Escalate lazily: read the denoised
            # source first and stop early once the component answer already agrees
            # with a 2+ vote consensus (fast path for easy captchas); only hard
            # captchas pay for the extra binarizations + Tesseract.
            full_texts: list[str] = []
            # ddddocr reads the whole arithmetic expression well ("19+14=") — seed
            # the vote pool with it so _vote_math_answers evaluates + tallies it.
            _dd_expr = self._try_ddddocr(img)
            if _dd_expr:
                full_texts.append(_dd_expr)
            comp_int_early = None
            if comp_answer is not None:
                try:
                    comp_int_early = int(comp_answer)
                except (ValueError, TypeError):
                    comp_int_early = None

            def _early_agree() -> bool:
                if comp_int_early is None:
                    return False
                vs = _vote_math_answers(full_texts, 0, 200)
                return bool(vs) and vs[0][0] == comp_int_early and vs[0][1] >= 2

            try:
                denoised_sw = _remove_colored_noise(img)
                reader_sw = self.model_manager.get_easyocr()
                math_chars_sw = "0123456789+-*/= "
                gray_sw = np.array(img.convert("L"))

                def _read_source(base, scales):
                    for dn_scale in scales:
                        h_dn, w_dn = base.shape
                        scaled_dn = cv2.resize(base, (w_dn * dn_scale, h_dn * dn_scale),
                                               interpolation=cv2.INTER_LANCZOS4)
                        for variant in (scaled_dn, 255 - scaled_dn):
                            results_dn = reader_sw.readtext(variant, detail=1, allowlist=math_chars_sw)
                            if results_dn:
                                sorted_dn = sorted(results_dn, key=lambda r: r[0][0][0])
                                full_texts.append(" ".join(r[1] for r in sorted_dn))

                _read_source(denoised_sw, [6, 4])
                if not _early_agree():
                    for thr in (50, 60):
                        _, bt = cv2.threshold(gray_sw, thr, 255, cv2.THRESH_BINARY_INV)
                        _read_source(bt, [6, 4])
                        if _early_agree():
                            break
                    # Tesseract only if still no confident consensus
                    if not _early_agree() and _init_tesseract():
                        import pytesseract
                        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH
                        scaled_dn = cv2.resize(denoised_sw,
                                               (denoised_sw.shape[1] * 6, denoised_sw.shape[0] * 6),
                                               interpolation=cv2.INTER_LANCZOS4)
                        pil_dn = Image.fromarray(255 - scaled_dn)
                        for psm in ["7", "8"]:
                            try:
                                text_dn = pytesseract.image_to_string(
                                    pil_dn,
                                    config=f"--psm {psm} -c tessedit_char_whitelist=0123456789+-*/="
                                ).strip()
                                if text_dn:
                                    full_texts.append(text_dn)
                            except Exception:
                                continue
            except Exception as _e:
                logger.debug(f"starwars denoised full-image OCR failed: {_e}")

            # (c) Vote on the evaluated integer across all full-image reads
            votes = _vote_math_answers(full_texts, 0, 200)
            top = votes[0] if votes else None
            runner = votes[1] if len(votes) > 1 else None
            comp_int = None
            if comp_answer is not None:
                try:
                    comp_int = int(comp_answer)
                except (ValueError, TypeError):
                    comp_int = None
            logger.info(f"math vote: comp={comp_answer} votes={votes[:4]}")

            # (d) Decision by agreement / consensus strength. NOTE: starwars has no
            # pre-submit refresh, so the token VALUE is what matters; confidence is
            # mostly diagnostic. Selection priority maximizes correctness:
            #   1. component & vote agree            -> strongest
            #   2. strong vote consensus overrides   -> fixes operator misreads (5*4*4->9)
            #   3. component alone, range-gated      -> best single guess
            #   4. any in-range vote                 -> last usable signal
            #   5. nothing usable                    -> "" so the retry re-rolls a fresh captcha
            if comp_int is not None and top is not None and comp_int == top[0]:
                logger.info(f"math agree: comp+vote={comp_int} -> 0.95")
                return str(comp_int), "math_eval", 0.95
            if top is not None and top[1] >= 3 and (runner is None or top[1] - runner[1] >= 2):
                logger.info(f"math consensus override: {top[0]} ({top[1]} votes, ex '{top[2]}') comp={comp_answer} -> 0.90")
                return str(top[0]), "math_eval", 0.90
            # A 2-vote consensus (same evaluated answer from two independent reads)
            # is more trustworthy than the component analysis, which mis-segments the
            # strike-through noise. Prefer it when the component DISAGREES.
            if top is not None and top[1] >= 2 and 0 <= top[0] <= 99 and (comp_int is None or comp_int != top[0]):
                logger.info(f"math 2-vote consensus over comp: {top[0]} ({top[1]} votes) comp={comp_answer} -> 0.80")
                return str(top[0]), "math_eval", 0.80
            # component-only, range-gated (starwars answers are small integers) —
            # rejects absurd component misreads like 108/291/6279/-7 which fall through
            if comp_int is not None and 0 <= comp_int <= 99:
                logger.info(f"math comp-only (in range): {comp_int} -> 0.85")
                return str(comp_int), "math_eval", 0.85
            if top is not None and 0 <= top[0] <= 99:
                logger.info(f"math weak vote: {top[0]} ({top[1]} votes) -> 0.72")
                return str(top[0]), "math_eval", 0.72

            # Try math eval on any candidate text (numeric-only)
            for candidate_text, _conf, _engine in candidates:
                math_answer = self._try_math_eval(candidate_text)
                if math_answer is not None and math_answer.lstrip("-").isdigit() \
                        and 0 <= int(math_answer) <= 99:
                    logger.info(f"math eval candidate: '{candidate_text}' -> {math_answer} -> 0.70")
                    return math_answer, "math_eval", 0.70

            # No confident reading. starwars has NO pre-submit refresh and an empty
            # token makes web_server mark the site failed WITHOUT retrying, so we must
            # always submit a numeric best-guess — a wrong guess triggers the
            # post-submit retry loop which re-rolls a fresh (hopefully easier) captcha.
            raw_guess = comp_int if comp_int is not None else (top[0] if top is not None else 0)
            fallback_int = raw_guess if 0 <= raw_guess <= 99 else abs(raw_guess) % 100
            logger.info(f"math: no confident reading — best-guess {fallback_int} -> 0.55 (submit+retry safety net)")
            return str(fallback_int), "math_guess", 0.55

        if not candidates:
            return "", "none", 0.0

        # Generic Yii2 hash validation: when the page exposed a captcha hash and this
        # is NOT one of the hand-tuned sites, use the hash (sum of lowercase char
        # codes — the Yii2 CaptchaAction default) to confirm or lightly repair the
        # OCR candidates. Built-in sites never reach here WITH a hash — kazan takes
        # its own early-return path, and the others are non-Yii2 (hash is None) — so
        # this can't regress them, it only adds accuracy for user-added Yii2 sites.
        if site_hint == "" and captcha_hash is not None:
            hashed = self._hash_rerank(candidates, captcha_hash)
            if hashed:
                logger.info(f"generic hash-validated answer: '{hashed[0]}' via {hashed[1]}")
                return hashed

        for c_text, c_conf, c_engine in sorted(candidates, key=lambda x: -x[1])[:10]:
            logger.debug(f"  candidate: '{c_text}' conf={c_conf:.3f} via {c_engine}")

        if site_hint == "klub":
            candidates = [(t.upper(), c, e) for t, c, e in candidates]
        elif site_hint == "kazan":
            candidates = [(t.lower(), c, e) for t, c, e in candidates]
            # Kazan's bold italic blue font reads best from raw RGB/high-contrast;
            # binary preprocessors produce wrong readings with inflated confidence.
            # Boost RGB/hicon candidates and demote binary+tesseract ones.
            boosted = []
            for t, c, e in candidates:
                if "rgb" in e or "hicon" in e:
                    boosted.append((t, c + 0.3, e))
                elif "tesseract" in e:
                    boosted.append((t, max(c - 0.2, 0.05), e))
                else:
                    boosted.append((t, c, e))
            candidates = boosted

        # Site-specific length constraints
        if site_hint == "klub":
            len5 = [(t, c, e) for t, c, e in candidates if len(t) == 5]
            if len5:
                candidates = len5
            else:
                close = [(t, c, e) for t, c, e in candidates if 4 <= len(t) <= 6]
                if close:
                    candidates = close

        # Length-aware filtering: group by length, find consensus per group,
        # then prefer the longest group with consensus.
        # This prevents truncated readings from dominating.
        lengths = [len(c[0]) for c in candidates]
        if lengths:
            max_len = max(lengths)
            # Find the most common "long" length (within 1 of max)
            long_lengths = [l for l in lengths if l >= max_len - 1]
            if long_lengths:
                target_len = Counter(long_lengths).most_common(1)[0][0]
            else:
                target_len = Counter(lengths).most_common(1)[0][0]
            min_acceptable = max(3, target_len - 1)
            filtered = [(t, c, e) for t, c, e in candidates if len(t) >= min_acceptable]
            if len(filtered) >= 3:
                candidates = filtered

        consensus = self._find_consensus(candidates, site_hint)
        if consensus:
            return consensus, "consensus", 0.75

        best = max(candidates, key=lambda r: r[1] + (0.02 * len(r[0])))

        all_texts = [c[0] for c in candidates]
        best_text = _disambiguate_chars(best[0], all_texts)
        best_text = self._clean_token(best_text)

        return best_text, best[2], best[1]

    def _try_easyocr(
        self, img: Image.Image, scale: int,
        allowlist: str | None = None,
    ) -> tuple[str, float]:
        reader = self.model_manager.get_easyocr()
        scaled = _scale_up(img, scale) if scale > 1 else img.copy()

        if scaled.mode != "L":
            enhanced = ImageOps.autocontrast(scaled.convert("L"), cutoff=5)
        else:
            enhanced = ImageOps.autocontrast(scaled, cutoff=3)

        arr = np.array(enhanced)

        kwargs = {"detail": 1}
        if allowlist:
            kwargs["allowlist"] = allowlist

        results = reader.readtext(arr, **kwargs)
        texts = [(r[1], float(r[2])) for r in results if r[2] > 0.005]
        if texts:
            best = max(texts, key=lambda t: t[1])
            return best[0], best[1]
        return "", 0.0

    def _try_ddddocr(self, img: Image.Image) -> str:
        """Run ddddocr (an OCR model purpose-built for distorted-text captchas) on
        the raw image. Returns the raw string, or '' if ddddocr is unavailable or
        errors. Near-instant, so it is cheap to add as an extra voting engine."""
        try:
            reader = self.model_manager.get_ddddocr()
            if not reader:
                return ""
            import io as _io
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return reader.classification(buf.getvalue()) or ""
        except Exception as e:
            logger.debug(f"ddddocr failed: {e}")
            return ""

    def _try_math_ocr(self, img: Image.Image) -> str:
        reader = self.model_manager.get_easyocr()
        scaled = _scale_up(img, 4)

        if scaled.mode != "L":
            enhanced = ImageOps.autocontrast(scaled.convert("L"), cutoff=5)
        else:
            enhanced = ImageOps.autocontrast(scaled, cutoff=3)

        arr = np.array(enhanced)
        results = reader.readtext(
            arr, detail=1,
            allowlist="0123456789+-×*/=xX ",
        )
        texts = [(r[1], float(r[2])) for r in results if r[2] > 0.005]
        if texts:
            best_text = max(texts, key=lambda t: t[1])[0]
            math_answer = self._try_math_eval(best_text)
            if math_answer is not None:
                logger.info(f"math_ocr pass: '{best_text}' -> {math_answer}")
                return best_text
        return ""

    def _try_math_ocr_enhanced(self, img: Image.Image, scale: int) -> str | None:
        """Component-based math captcha solver for 'digit op digit =' expressions."""
        img_np = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        reader = self.model_manager.get_easyocr()
        img_w = gray.shape[1]
        img_h = gray.shape[0]

        # Step 1: Find components — try denoised + multiple thresholds
        equals_x = img_w * 0.85
        best_components = []
        best_tall_count = 0
        binary = None

        # Build list of binary images to try: denoised first, then thresholds
        binaries_to_try = []
        try:
            denoised = _remove_colored_noise(img)
            denoised_fg = 255 - denoised  # text=255 foreground
            binaries_to_try.append(denoised_fg)
        except Exception:
            denoised_fg = None

        for thresh in [40, 50, 60]:
            _, bin_t = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY_INV)
            binaries_to_try.append(bin_t)

        for bin_t in binaries_to_try:
            n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(bin_t)

            components = []
            for i in range(1, n_labels):
                x = int(stats[i, cv2.CC_STAT_LEFT])
                y = int(stats[i, cv2.CC_STAT_TOP])
                w = int(stats[i, cv2.CC_STAT_WIDTH])
                h = int(stats[i, cv2.CC_STAT_HEIGHT])
                area = int(stats[i, cv2.CC_STAT_AREA])
                if w >= img_w * 0.8:
                    continue
                if area < 5:
                    continue
                if x >= equals_x:
                    continue
                fill_ratio = area / max(w * h, 1)
                components.append((x, y, w, h, area, fill_ratio))

            tall_count = sum(1 for _, _, _, h, a, _ in components if h >= 5 and a >= 12)
            if tall_count > best_tall_count:
                best_tall_count = tall_count
                best_components = components
                binary = bin_t

            if tall_count >= 3:
                break

        if binary is None:
            _, binary = cv2.threshold(gray, 40, 255, cv2.THRESH_BINARY_INV)
        components = sorted(best_components)
        ocr_binary = denoised_fg if denoised_fg is not None else binary

        # Step 2: Two-pass classification
        # First pass: collect all tall components as potential digits
        tall_comps = []
        short_comps = []
        for x, y, w, h, area, fill in components:
            if h <= 3 and w >= 4 and area >= 10:
                short_comps.append((x, y, w, h, area, fill))
            elif h >= 5 and area >= 12 and w / max(h, 1) < 3.0:
                tall_comps.append((x, y, w, h, area, fill))
        logger.debug(f"math_comp: {len(tall_comps)} tall, {len(short_comps)} short, equals_x={equals_x:.0f}")

        # Pre-detect '+' signs: vertical bar (tall) + horizontal bar (short) at same position
        plus_sign_positions = []
        tall_to_remove = set()
        short_to_remove = set()
        for ti, (tx, ty, tw, th, ta, tf) in enumerate(tall_comps):
            if th < 1.5 * max(tw, 1) or tf < 0.55:
                continue
            tx_cx = tx + tw / 2
            tx_cy = ty + th / 2
            for si, (sx, sy, sw, sh, sa, sf) in enumerate(short_comps):
                if si in short_to_remove:
                    continue
                sx_cx = sx + sw / 2
                sx_cy = sy + sh / 2
                if abs(tx_cx - sx_cx) <= max(tw, sw) and abs(tx_cy - sx_cy) <= th * 0.5:
                    plus_sign_positions.append((min(tx, sx), min(ty, sy),
                                                max(tx + tw, sx + sw) - min(tx, sx),
                                                max(ty + th, sy + sh) - min(ty, sy)))
                    tall_to_remove.add(ti)
                    short_to_remove.add(si)
                    logger.debug(f"math_comp: split PLUS pre-detected at x≈{tx_cx:.0f}")
                    break
        if tall_to_remove:
            tall_comps = [c for i, c in enumerate(tall_comps) if i not in tall_to_remove]
        if short_to_remove:
            short_comps = [c for i, c in enumerate(short_comps) if i not in short_to_remove]

        # Second pass: classify — PLUS detection and digit identification
        digits = []
        operator = None

        if len(tall_comps) >= 3:
            # Try treating ALL tall comps as digits first (3-operand expressions).
            # Only classify as PLUS if fill is very low and aspect is nearly square.
            plus_indices = []
            for i, (x, y, w, h, area, fill) in enumerate(tall_comps):
                has_left = any(c[0] + c[2] < x for c in tall_comps if c != (x, y, w, h, area, fill))
                has_right = any(c[0] > x + w for c in tall_comps if c != (x, y, w, h, area, fill))
                if has_left and has_right and fill < 0.32 and 0.7 < (h / w) < 1.4:
                    plus_indices.append(i)
                    logger.debug(f"math_comp: PLUS candidate at x={x} w={w} h={h} fill={fill:.2f}")
            # Only remove PLUS candidates if we'd still have >= 2 digits
            if plus_indices and len(tall_comps) - len(plus_indices) >= 2:
                for i, (x, y, w, h, area, fill) in enumerate(tall_comps):
                    if i in plus_indices:
                        operator = "+"
                    else:
                        digits.append((x, y, w, h, area))
            else:
                digits = [(x, y, w, h, area) for x, y, w, h, area, fill in tall_comps]
        elif len(tall_comps) == 2:
            # With only 2 tall components, check if one is a PLUS
            # A PLUS has very low fill ratio (<0.36) and near-square aspect
            for i, (x, y, w, h, area, fill) in enumerate(tall_comps):
                if fill < 0.36 and 0.7 < (h / w) < 1.5 and w >= 8:
                    operator = "+"
                    logger.debug(f"math_comp: PLUS(2) at x={x} w={w} h={h} fill={fill:.2f}")
                else:
                    digits.append((x, y, w, h, area))
            if len(digits) < 2:
                digits = [(x, y, w, h, area) for x, y, w, h, area, fill in tall_comps]
                operator = None
        else:
            digits = [(x, y, w, h, area) for x, y, w, h, area, fill in tall_comps]

        # Check short components for MINUS or split PLUS (between two digits)
        op_confident = False
        if operator is None and len(digits) >= 2 and short_comps:
            d1_end = digits[0][0] + digits[0][2]
            d2_start = digits[-1][0] if len(digits) > 1 else img_w
            for x, y, w, h, area, fill in short_comps:
                if digits[0][0] < x < d2_start:
                    sc_cx = x + w / 2
                    sc_cy = y + h / 2
                    is_plus = False
                    for cx, cy, cw, ch, ca, cf in components:
                        if ch > cw and ch >= 3 and (cx, cy, cw, ch) != (x, y, w, h):
                            c_cx = cx + cw / 2
                            c_cy = cy + ch / 2
                            if abs(c_cx - sc_cx) <= max(cw, w) and abs(c_cy - sc_cy) <= max(ch, h) * 0.7:
                                is_plus = True
                                break
                    operator = "+" if is_plus else "-"
                    gap_to_d1 = x - d1_end
                    gap_to_d2 = d2_start - (x + w)
                    op_confident = gap_to_d1 <= 5 or gap_to_d2 <= 5
                    logger.debug(f"math_comp: {'split-PLUS' if is_plus else 'MINUS'} at x={x}")
                    break

        # Check for pre-detected split '+' signs between digits
        if operator is None and len(digits) >= 2 and plus_sign_positions:
            d1_end = digits[0][0] + digits[0][2]
            d2_start = digits[-1][0]
            for px, py, pw, ph in plus_sign_positions:
                p_center = px + pw / 2
                if d1_end - 3 <= p_center <= d2_start + 3:
                    operator = "+"
                    op_confident = True
                    logger.debug(f"math_comp: pre-detected PLUS between digits")
                    break

        logger.debug(f"math_comp: {len(digits)} digits, op={operator} confident={op_confident}")

        # Step 3: If we have exactly 2 digits, OCR each one and try combinations
        # For 3+ digits, skip to full-image OCR (multi-operand expression)
        if len(digits) == 2:
            d1_box = digits[0]
            d2_box = digits[1]

            d1_candidates = self._ocr_single_digit(gray, binary, d1_box, scale)
            d2_candidates = self._ocr_single_digit(gray, binary, d2_box, scale)
            if ocr_binary is not binary:
                d1_den = self._ocr_single_digit(gray, ocr_binary, d1_box, scale)
                d2_den = self._ocr_single_digit(gray, ocr_binary, d2_box, scale)
                for d in d1_den:
                    if d not in d1_candidates:
                        d1_candidates.append(d)
                for d in d2_den:
                    if d not in d2_candidates:
                        d2_candidates.append(d)

            # If operator is confident, try it first; otherwise try all
            if operator and op_confident:
                ops_to_try = [operator]
            elif operator:
                ops_to_try = [operator, "+", "*", "-"]
            else:
                ops_to_try = ["-", "+", "*"]
            # Deduplicate
            ops_to_try = list(dict.fromkeys(ops_to_try))

            # Build combos: primary first, then single-digit swaps
            combos = []
            d2_primary = d2_candidates[0] if d2_candidates else None
            d1_primary = d1_candidates[0] if d1_candidates else None

            if d1_primary is not None and d2_primary is not None:
                combos.append((d1_primary, d2_primary))
            for d1 in d1_candidates[1:4]:
                if d2_primary is not None:
                    combos.append((d1, d2_primary))
            for d2 in d2_candidates[1:3]:
                if d1_primary is not None:
                    combos.append((d1_primary, d2))

            for op in ops_to_try:
                for d1_val, d2_val in combos:
                    expr = f"{d1_val}{op}{d2_val}"
                    result = _safe_eval_math(expr)
                    if result is not None and result >= 0:
                        logger.info(f"math_components: {expr} = {result}")
                        return str(result)
        multi_fallback = None
        if len(digits) >= 3:
            # Multi-operand expression: d1 op1 d2 op2 d3
            d_boxes = digits[:3]
            d_vals = [self._ocr_single_digit(gray, binary, db, scale) for db in d_boxes]
            if ocr_binary is not binary:
                d_den = [self._ocr_single_digit(gray, ocr_binary, db, scale) for db in d_boxes]
                for i in range(len(d_vals)):
                    for d in d_den[i]:
                        if d not in d_vals[i]:
                            d_vals[i].append(d)

            # Detect operators between each pair of adjacent digits
            detected_ops = []
            for di in range(len(d_boxes) - 1):
                d_end = d_boxes[di][0] + d_boxes[di][2]
                d_next = d_boxes[di + 1][0]
                op_here = None
                for tc in tall_comps:
                    tx, ty, tw, th, ta, tf = tc
                    if d_end <= tx and tx + tw <= d_next and tf < 0.42:
                        op_here = "+"
                        break
                if op_here is None:
                    for pp in plus_sign_positions:
                        px, py, pw, ph = pp
                        p_center = px + pw / 2
                        if d_end - 3 <= p_center <= d_next + 3:
                            op_here = "+"
                            break
                if op_here is None:
                    for sc in short_comps:
                        sx, sy, sw, sh, sa, sf = sc
                        if d_end - 2 <= sx and sx + sw <= d_next + 2:
                            sc_cx = sx + sw / 2
                            sc_cy = sy + sh / 2
                            is_plus = False
                            for cx, cy, cw, ch, ca, cf in components:
                                if ch > cw and ch >= 3 and (cx, cy, cw, ch) != (sx, sy, sw, sh):
                                    c_cx = cx + cw / 2
                                    c_cy = cy + ch / 2
                                    if abs(c_cx - sc_cx) <= max(cw, sw) and abs(c_cy - sc_cy) <= max(ch, sh) * 0.7:
                                        is_plus = True
                                        break
                            op_here = "+" if is_plus else "-"
                            break
                if op_here is None:
                    op_here = "*"
                detected_ops.append(op_here)

            any_op_detected = any(op != "*" for op in detected_ops)
            logger.debug(f"math_multi: digits={[v[0] if v else '?' for v in d_vals]} ops={detected_ops}")

            op_combos = [tuple(detected_ops)]
            for alt_ops in [("+", "*"), ("*", "+"), ("+", "-"), ("-", "+"),
                            ("+", "+"), ("*", "-"), ("-", "*"), ("*", "*")]:
                if alt_ops not in op_combos:
                    op_combos.append(alt_ops)

            if any_op_detected:
                for ops in op_combos:
                    for d0 in d_vals[0][:3]:
                        for d1 in d_vals[1][:2]:
                            for d2 in d_vals[2][:2]:
                                expr = f"{d0}{ops[0]}{d1}{ops[1]}{d2}"
                                result = _safe_eval_math(expr)
                                if result is not None and result >= 0:
                                    logger.info(f"math_multi: {expr} = {result}")
                                    return str(result)
            else:
                all_results = []
                for ops in op_combos:
                    for d0 in d_vals[0][:3]:
                        for d1 in d_vals[1][:2]:
                            for d2 in d_vals[2][:2]:
                                expr = f"{d0}{ops[0]}{d1}{ops[1]}{d2}"
                                result = _safe_eval_math(expr)
                                if result is not None and result >= 0:
                                    all_results.append((result, expr, ops))
                if all_results:
                    best = all_results[0]
                    multi_fallback = str(best[0])
                    logger.info(f"math_multi tentative: {best[1]} = {best[0]} (best of {len(all_results)}, deferring to full-image OCR)")

        # Step 4: Fallback — try OCR on the whole image with various preprocessings
        math_chars = "0123456789+-×·*xX/=() "

        # Try denoised image first (colored noise removed)
        fallback_bins = []
        if denoised_fg is not None:
            fallback_bins.append(denoised_fg)
        for thresh in [40, 60, 80]:
            _, bin_t = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY_INV)
            fallback_bins.append(bin_t)

        for fb_bin in fallback_bins:
            h_i, w_i = fb_bin.shape
            scaled_np = cv2.resize(fb_bin, (w_i * scale, h_i * scale),
                                   interpolation=cv2.INTER_LANCZOS4)

            results = reader.readtext(scaled_np, detail=1, allowlist=math_chars)
            if results:
                sorted_r = sorted(results, key=lambda r: r[0][0][0])
                full = " ".join(r[1] for r in sorted_r)
                cleaned = self._clean_math_text(full)
                answer = self._try_math_eval(cleaned)
                if answer is not None:
                    logger.info(f"math_fallback easyocr: '{full}' -> {answer}")
                    return answer

            # Tesseract fallback
            if _init_tesseract():
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH
                inverted = 255 - scaled_np
                pil_inv = Image.fromarray(inverted)
                for psm in ["7", "8"]:
                    try:
                        text = pytesseract.image_to_string(
                            pil_inv,
                            config=f"--psm {psm} -c tessedit_char_whitelist=0123456789+-*/=x."
                        ).strip()
                        if text:
                            cleaned = self._clean_math_text(text)
                            answer = self._try_math_eval(cleaned)
                            if answer is not None:
                                logger.info(f"math_fallback tess: '{text}' -> {answer}")
                                return answer
                    except Exception:
                        continue

        if multi_fallback is not None:
            logger.info(f"math_multi fallback (full-image OCR failed): {multi_fallback}")
            return multi_fallback

        return None

    def _ocr_single_digit(self, gray: np.ndarray, binary: np.ndarray,
                          box: tuple, scale: int) -> list[int]:
        """OCR a single digit — returns candidate digits ordered by confidence."""
        x, y, w, h, area = box
        pad = 3
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(gray.shape[1], x + w + pad)
        y1 = min(gray.shape[0], y + h + pad)

        crop = binary[y0:y1, x0:x1]
        crop_h, crop_w = crop.shape
        s = max(scale, 6)
        scaled = cv2.resize(crop, (crop_w * s, crop_h * s), interpolation=cv2.INTER_NEAREST)

        candidates = []
        reader = self.model_manager.get_easyocr()

        for s_factor in [s, s + 2]:
            sc = cv2.resize(crop, (crop_w * s_factor, crop_h * s_factor),
                            interpolation=cv2.INTER_NEAREST)
            results = reader.readtext(sc, detail=1, allowlist="0123456789")
            for r in results:
                digit_text = r[1].strip()
                if digit_text.isdigit() and len(digit_text) == 1:
                    candidates.append((int(digit_text), float(r[2])))

        if _init_tesseract():
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH
            inverted = 255 - scaled
            pil_inv = Image.fromarray(inverted)
            for psm in ["10", "8"]:
                try:
                    text = pytesseract.image_to_string(
                        pil_inv,
                        config=f"--psm {psm} -c tessedit_char_whitelist=0123456789"
                    ).strip()
                    if text.isdigit() and len(text) == 1:
                        candidates.append((int(text), 0.5))
                except Exception:
                    pass

        # Deduplicate and sort by confidence
        seen = {}
        for digit, conf in candidates:
            if digit not in seen or conf > seen[digit]:
                seen[digit] = conf
        result = sorted(seen.keys(), key=lambda d: -seen[d])

        # Add confusable alternatives
        confusables = {0: [9, 6], 9: [0, 4], 6: [5, 0, 8], 5: [6, 3], 1: [7], 7: [1, 2], 3: [8, 5], 8: [3, 6], 4: [9], 2: [7]}
        if result:
            primary = result[0]
            for alt in confusables.get(primary, []):
                if alt not in result:
                    result.append(alt)

        # Return [] (not a fabricated 1-9 range) when no engine read a digit, so
        # operator/equals/noise boxes defer to voting instead of inventing operands.
        return result

    def _clean_math_text(self, text: str) -> str:
        """Normalize OCR output into a math expression."""
        text = text.replace("·", "*").replace("×", "*").replace("÷", "/")
        text = re.sub(r"(\d)\s*[xX]\s*(\d)", r"\1*\2", text)
        text = text.replace("=", "").replace(" ", "")
        text = text.replace("l", "1").replace("I", "1")
        text = text.replace("O", "0").replace("o", "0")
        text = text.replace("S", "5").replace("s", "5")
        text = text.replace("B", "8")
        text = re.sub(r"[^0-9+\-*/]", "", text)
        return text

    def _extract_math_from_noisy(self, text: str) -> str | None:
        """Extract a math expression from noisy OCR text.

        Looks for patterns like 'digit op digit' scattered in noise.
        Handles: 9 ... - ... 6 ... = or 1 + 4 = etc.
        """
        # First try WITHOUT replacing middle dots (they're often just noise)
        text_clean = text.replace("×", "*").replace("÷", "/")
        text_clean = re.sub(r"[xX](?=\s*\d)", "*", text_clean)

        tokens = re.findall(r"(\d+|[+\-*/])", text_clean)
        if len(tokens) >= 3:
            for i in range(len(tokens) - 2):
                if tokens[i].isdigit() and tokens[i + 1] in "+-*/" and tokens[i + 2].isdigit():
                    expr = f"{tokens[i]}{tokens[i+1]}{tokens[i+2]}"
                    result = _safe_eval_math(expr)
                    if result is not None:
                        logger.info(f"math extracted: '{expr}' = {result} from '{text[:50]}'")
                        return str(result)

        # Now try WITH middle dot replacement (only between digits)
        text_dot = re.sub(r"(\d)\s*·\s*(\d)", r"\1*\2", text)
        text_dot = text_dot.replace("×", "*").replace("÷", "/")
        text_dot = re.sub(r"[xX](?=\s*\d)", "*", text_dot)

        tokens = re.findall(r"(\d+|[+\-*/])", text_dot)
        if len(tokens) >= 3:
            for i in range(len(tokens) - 2):
                if tokens[i].isdigit() and tokens[i + 1] in "+-*/" and tokens[i + 2].isdigit():
                    expr = f"{tokens[i]}{tokens[i+1]}{tokens[i+2]}"
                    result = _safe_eval_math(expr)
                    if result is not None:
                        logger.info(f"math extracted (dot): '{expr}' = {result}")
                        return str(result)

        return None

    def _try_tesseract(
        self, img: Image.Image, scale: int, label: str, easy_text: str,
        site_hint: str = "",
    ) -> str:
        if not _init_tesseract():
            return ""
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH

        scaled = _scale_up(img, scale) if scale > 1 else img.copy()
        inverted = ImageOps.invert(scaled.convert("L"))

        configs = []
        if site_hint == "klub":
            configs = [
                f"--psm 7 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                f"--psm 8 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            ]
        elif site_hint == "kazan":
            configs = [
                f"--psm 7 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz",
                f"--psm 8 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz",
                f"--psm 13 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz",
            ]
        else:
            configs = ["--psm 7", "--psm 8"]

        for cfg in configs:
            try:
                text = pytesseract.image_to_string(inverted, config=cfg).strip()
                clean = self._clean_token(text)
                if clean and len(clean) >= 2:
                    return clean
            except Exception:
                continue
        return ""

    def _try_perchar_ocr(self, img: Image.Image, allowlist: str | None = None) -> str:
        """Segment into individual characters and OCR each one."""
        reader = self.model_manager.get_easyocr()
        arr = np.array(img)
        if len(arr.shape) == 3 and arr.shape[2] >= 2:
            if arr.shape[0] > 1 and arr[0, 0, 0] == 42 and arr[0, 0, 1] == 29:
                img = img.crop((0, 1, img.width, img.height))

        # Try multiple binarization thresholds for segmentation
        best_result = ""
        for thresh_pct in [0.30, 0.40, 0.20]:
            gray = np.array(img.convert("L"))
            flat = sorted(gray.flatten())
            thresh = flat[int(len(flat) * thresh_pct)]
            binary = (gray < thresh).astype(int)
            proj = binary.sum(axis=0)
            min_px = max(2, int(gray.shape[0] * 0.12))

            in_char = False
            chars = []
            start = 0
            for x, count in enumerate(proj):
                if count >= min_px and not in_char:
                    start = x
                    in_char = True
                elif count < min_px and in_char:
                    if x - start >= 3:
                        chars.append((start, x))
                    in_char = False
            if in_char and len(proj) - start >= 3:
                chars.append((start, len(proj)))

            if len(chars) < 3:
                continue

            # Split wide segments that likely contain 2+ chars
            split_chars = []
            for s, e in chars:
                width = e - s
                avg_char_w = img.height * 0.6
                if width > avg_char_w * 1.8 and len(chars) < 6:
                    mid = s + width // 2
                    split_chars.append((s, mid))
                    split_chars.append((mid, e))
                else:
                    split_chars.append((s, e))

            result = ""
            for s, e in split_chars:
                left = max(0, s - 1)
                right = min(img.width, e + 2)
                slot = img.crop((left, 0, right, img.height))
                scale_f = max(3, min(6, 40 // max(1, slot.width)))
                scaled = slot.resize((slot.width * scale_f, slot.height * scale_f), Image.LANCZOS)
                arr_slot = np.array(scaled.convert("L"))

                kwargs = {"detail": 1}
                if allowlist:
                    kwargs["allowlist"] = allowlist

                ez = reader.readtext(arr_slot, **kwargs)
                ez_texts = [(r[1], float(r[2])) for r in ez if r[2] > 0.01]
                if ez_texts:
                    char = max(ez_texts, key=lambda t: t[1])[0][:1]
                    result += self._clean_token(char)
                else:
                    result += "?"

            if "?" not in result and len(result) >= 3:
                if len(result) > len(best_result):
                    best_result = result

        return best_result

    def _solve_klub(self, img: Image.Image) -> tuple[str, str, float]:
        """Klub-specific OCR: bold italic on noisy grid background.
        Uses hard threshold ~155 to isolate text from grid, then per-char OCR."""
        allowlist = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        reader = self.model_manager.get_easyocr()
        gray = np.array(img.convert("L"))
        candidates = []

        for thresh in [155, 160, 165, 150]:
            binary = (gray < thresh).astype(np.uint8) * 255
            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
            comps = []
            for i in range(1, num_labels):
                x, y, w, h, area = stats[i]
                if area > 15 and h > 5 and w > 2:
                    comps.append((x, y, w, h, area))
            comps.sort(key=lambda c: c[0])

            if not (4 <= len(comps) <= 6):
                continue

            # Per-character OCR on isolated components
            perchar = ""
            for x, y, w, h, area in comps:
                crop_arr = 255 - binary[max(0, y - 1):y + h + 1, max(0, x - 1):x + w + 1]
                crop = Image.fromarray(crop_arr)
                cw, ch = crop.size
                best_char = "?"
                best_conf = 0.0
                for s in [6, 8, 4, 12]:
                    crop_s = crop.resize((cw * s, ch * s), Image.LANCZOS)
                    arr_c = np.array(ImageOps.autocontrast(crop_s.convert("L"), cutoff=3))
                    res = reader.readtext(arr_c, detail=1, allowlist=allowlist)
                    for r in res:
                        if r[2] > best_conf:
                            best_conf = r[2]
                            best_char = r[1][:1]
                    if best_conf > 0.9:
                        break
                perchar += best_char

            clean = self._clean_token(perchar).upper().replace("?", "")
            if clean and len(clean) == 5:
                candidates.append((clean, 0.85, f"perchar_t{thresh}"))
                logger.debug(f"klub perchar_t{thresh}: '{clean}'")

            # Full-image OCR on this threshold too
            bin_img = Image.fromarray(255 - binary)
            w2, h2 = bin_img.size
            for scale in [6, 4]:
                scaled = bin_img.resize((w2 * scale, h2 * scale), Image.LANCZOS)
                arr = np.array(ImageOps.autocontrast(scaled.convert("L"), cutoff=3))
                results = reader.readtext(arr, detail=1, allowlist=allowlist)
                combined = "".join(r[1] for r in results).upper()
                combined = self._clean_token(combined)
                if combined and len(combined) == 5:
                    candidates.append((combined, 0.70, f"full_t{thresh}_{scale}x"))
                elif combined and len(combined) > 5:
                    for start in range(len(combined) - 4):
                        sub = combined[start:start + 5]
                        candidates.append((sub, 0.55, f"full_trim_t{thresh}_{scale}x"))

        # Also try standard full-image OCR as fallback
        for scale in [4, 6]:
            w2, h2 = img.size
            scaled = img.resize((w2 * scale, h2 * scale), Image.LANCZOS)
            arr = np.array(ImageOps.autocontrast(scaled.convert("L"), cutoff=5))
            results = reader.readtext(arr, detail=1, allowlist=allowlist)
            combined = "".join(r[1] for r in results).upper()
            combined = self._clean_token(combined)
            if combined and len(combined) == 5:
                candidates.append((combined, 0.60, f"standard_{scale}x"))

        if not candidates:
            return "", "none", 0.0

        for c_text, c_conf, c_engine in sorted(candidates, key=lambda x: -x[1])[:8]:
            logger.debug(f"  klub candidate: '{c_text}' conf={c_conf:.3f} via {c_engine}")

        # Prefer 5-char candidates
        len5 = [(t, c, e) for t, c, e in candidates if len(t) == 5]
        if len5:
            candidates = len5

        consensus = self._find_consensus(candidates, "klub")
        if consensus:
            return consensus.upper(), "consensus", 0.75

        best = max(candidates, key=lambda r: r[1] + 0.02 * len(r[0]))
        return best[0].upper(), best[2], best[1]

    @staticmethod
    def _yii2_hash(text: str) -> int:
        """Compute Yii2 captcha validation hash (sum of lowercase char codes)."""
        return sum(ord(c) for c in text.lower())

    def _hash_rerank(self, candidates, captcha_hash):
        """Use a Yii2 captcha hash to confirm or lightly repair OCR candidates on a
        generic (user-added) site. Returns (text, engine, conf) on a hash-validated
        answer, else None. Case-insensitive (the hash lowercases)."""
        if captcha_hash is None or not candidates:
            return None
        from collections import Counter
        # 1) Exact hash matches among the candidate pool — vote by frequency. With a
        #    dozen binarizations x scales x engines, the correct read is often already
        #    present; the hash simply confirms which one it is.
        matches = [(t, c, e) for t, c, e in candidates if t and self._yii2_hash(t) == captcha_hash]
        if matches:
            counts = Counter(t for t, _, _ in matches)
            best_text, cnt = counts.most_common(1)[0]
            return (best_text, "hash_match", min(0.97, 0.85 + 0.03 * cnt))
        # 2) Single-character confusion repair on the strongest few candidates.
        CONFUSE = {
            'o': '0', '0': 'o', 'l': '1', '1': 'l', 'i': '1', '5': 's', 's': '5',
            '2': 'z', 'z': '2', 'b': '6', '6': 'b', 'g': '9', '9': 'g', 'q': 'g',
            'a': 'e', 'e': 'a', 'n': 'm', 'm': 'n', 'u': 'v', 'v': 'u', '8': 'b',
        }
        for t, c, e in sorted(candidates, key=lambda x: -x[1])[:6]:
            if not t:
                continue
            for i, ch in enumerate(t.lower()):
                dst = CONFUSE.get(ch)
                if dst:
                    cand = t[:i] + dst + t[i + 1:]
                    if self._yii2_hash(cand) == captcha_hash:
                        logger.info(f"generic hash repair: '{t}' -> '{cand}'")
                        return (cand, "hash_repair", 0.88)
        return None

    def _solve_kazan(self, img: Image.Image, captcha_hash: int | None = None) -> tuple[str, str, float]:
        """Kazan-specific OCR: bold italic blue text on white background.
        Uses raw RGB + high-contrast + shear correction (de-italicize).
        If captcha_hash is provided, validates candidates against the Yii2 hash."""
        allowlist = "abcdefghijklmnopqrstuvwxyz"
        candidates = []

        # ddddocr (captcha-specialized) is very strong on kazan's bold-italic blue
        # font (benchmark: 5/6 vs EasyOCR's 3/6). Seed the pool with its reading so
        # the Yii2-hash validation + confusion/ghost/delete repair below can confirm
        # or fix it. Ranked high; the hash gates correctness, so this is safe.
        _dd_kazan = self._clean_token(self._try_ddddocr(img)).lower()
        if _dd_kazan and len(_dd_kazan) >= 4:
            candidates.append((_dd_kazan, 0.90, "ddddocr"))

        high_contrast = _preprocess_high_contrast(img)

        def _early_hash_hit(cands, target_hash, min_count=3):
            """Check if candidates already have a strong hash consensus."""
            if target_hash is None or len(cands) < min_count:
                return None
            from collections import Counter as _C
            matches = [(t, c, e) for t, c, e in cands if self._yii2_hash(t) == target_hash]
            if not matches:
                return None
            counts = _C(t for t, _, _ in matches)
            best_text, best_count = counts.most_common(1)[0]
            if best_count >= min_count:
                best_conf = max(c for t, c, _ in matches if t == best_text)
                best_eng = next(e for t, _, e in matches if t == best_text)
                return (best_text, best_conf, best_eng, best_count, len(matches))
            return None

        # De-italicize: apply shear to straighten italic text
        deskewed_variants = [("orig", img)]
        for shear in [-0.3, -0.2, -0.4]:
            try:
                w, h = img.size
                new_w = int(w + abs(shear) * h)
                coeffs = (1, shear, -shear * h if shear > 0 else 0, 0, 1, 0)
                deskewed = img.transform((new_w, h), Image.AFFINE, coeffs,
                                         resample=Image.BICUBIC,
                                         fillcolor=(255, 255, 255))
                deskewed_variants.append((f"shear{shear}", deskewed))
            except Exception:
                pass

        for label, variant in deskewed_variants:
            for scale in [4, 3, 5, 6]:
                text, conf = self._try_easyocr(variant, scale, allowlist=allowlist)
                if text:
                    clean = self._clean_token(text).lower()
                    if clean and len(clean) >= 4:
                        candidates.append((clean, conf, f"rgb_{label}_{scale}x"))

        early = _early_hash_hit(candidates, captcha_hash, min_count=3)
        if early:
            t, c, e, cnt, total = early
            logger.info(f"kazan early hash hit after RGB! '{t}' (count={cnt}, {total} matches, skipping rest)")
            return t.lower(), f"hash_early_{e}", 0.95

        # No-allowlist variants at higher scales — the bold italic font reads
        # BETTER without constraining to lowercase (e.g. 'o' misread as 'g')
        for scale in [5, 6, 8]:
            reader = self.model_manager.get_easyocr()
            w, h = img.size
            scaled = _scale_up(img, scale) if scale > 1 else img.copy()
            arr = np.array(ImageOps.autocontrast(scaled.convert("L"), cutoff=5))
            results = reader.readtext(arr, detail=1)
            texts = [(r[1], float(r[2])) for r in results if r[2] > 0.005]
            if texts:
                combined = "".join(t for t, c in texts).lower()
                combined = self._clean_token(combined)
                avg_conf = sum(c for _, c in texts) / len(texts)
                if combined and len(combined) >= 4:
                    candidates.append((combined, avg_conf + 0.1, f"noallow_{scale}x"))

        # High contrast variants
        for scale in [4, 3, 5]:
            text, conf = self._try_easyocr(high_contrast, scale, allowlist=allowlist)
            if text:
                clean = self._clean_token(text).lower()
                if clean and len(clean) >= 4:
                    candidates.append((clean, conf, f"hicon_{scale}x"))

        early = _early_hash_hit(candidates, captcha_hash, min_count=3)
        if early:
            t, c, e, cnt, total = early
            logger.info(f"kazan early hash hit after hicon! '{t}' (count={cnt}, {total} matches, skipping rest)")
            return t.lower(), f"hash_early_{e}", 0.95

        # Morphological thinning — reduce bold stroke width to separate touching chars
        try:
            gray_np = np.array(img.convert("L"))
            _, bold_bin = cv2.threshold(gray_np, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            thin_kernel = np.ones((2, 2), np.uint8)
            thinned = cv2.erode(bold_bin, thin_kernel, iterations=1)
            thinned_img = Image.fromarray(255 - thinned)
            for scale in [4, 3, 5]:
                text, conf = self._try_easyocr(thinned_img, scale, allowlist=allowlist)
                if text:
                    clean = self._clean_token(text).lower()
                    if clean and len(clean) >= 4:
                        candidates.append((clean, conf, f"thinned_{scale}x"))
        except Exception:
            pass

        early = _early_hash_hit(candidates, captcha_hash, min_count=3)
        if early:
            t, c, e, cnt, total = early
            logger.info(f"kazan early hash hit after thinned! '{t}' (count={cnt}, {total} matches, skipping rest)")
            return t.lower(), f"hash_early_{e}", 0.95

        # Blue text extraction with morphological cleanup
        try:
            blue_bin = _binarize_blue_text(img)
            kernel = np.ones((3, 3), np.uint8)
            blue_np = np.array(blue_bin)
            blue_eroded = cv2.erode(blue_np, kernel, iterations=1)
            blue_clean = Image.fromarray(blue_eroded)
            for scale in [4, 5]:
                text, conf = self._try_easyocr(blue_clean, scale, allowlist=allowlist)
                if text:
                    clean = self._clean_token(text).lower()
                    if clean and len(clean) >= 4:
                        candidates.append((clean, conf, f"blue_{scale}x"))
        except Exception:
            pass

        # Blue text + thinning combined
        try:
            blue_np2 = np.array(_binarize_blue_text(img))
            thin_k = np.ones((2, 2), np.uint8)
            blue_thinned = cv2.erode(blue_np2, thin_k, iterations=1)
            blue_thin_img = Image.fromarray(blue_thinned)
            for scale in [4, 5]:
                text, conf = self._try_easyocr(blue_thin_img, scale, allowlist=allowlist)
                if text:
                    clean = self._clean_token(text).lower()
                    if clean and len(clean) >= 4:
                        candidates.append((clean, conf, f"blue_thin_{scale}x"))
        except Exception:
            pass

        early = _early_hash_hit(candidates, captcha_hash, min_count=3)
        if early:
            t, c, e, cnt, total = early
            logger.info(f"kazan early hash hit after blue! '{t}' (count={cnt}, {total} matches, skipping rest)")
            return t.lower(), f"hash_early_{e}", 0.95

        # Sharpened 12x — recovers narrow chars (x, r, i) lost in bold italic font.
        # Works best on canvas-extracted images (no anti-aliasing blur).
        try:
            sharpen_kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]], dtype=np.float32)
            for base_scale in [12, 8]:
                scaled = _scale_up(img, base_scale)
                gray_arr = np.array(scaled.convert("L"))
                sharpened = cv2.filter2D(gray_arr, -1, sharpen_kernel)
                sharpened = np.clip(sharpened, 0, 255).astype(np.uint8)
                sharp_img = Image.fromarray(sharpened)
                reader = self.model_manager.get_easyocr()
                results = reader.readtext(np.array(ImageOps.autocontrast(sharp_img, cutoff=5)),
                                          detail=1, allowlist=allowlist)
                texts = [(r[1], float(r[2])) for r in results if r[2] > 0.005]
                if texts:
                    best_r = max(texts, key=lambda t: t[1])
                    clean = self._clean_token(best_r[0]).lower()
                    if clean and len(clean) >= 4:
                        candidates.append((clean, best_r[1] + 0.15, f"sharp_{base_scale}x"))
                        logger.debug(f"kazan sharp_{base_scale}x: '{clean}' conf={best_r[1]:.3f}")
        except Exception:
            pass

        # Early exit: if 3+ candidates already match hash with same text, skip Tesseract
        early = _early_hash_hit(candidates, captcha_hash, min_count=3)
        if early:
            t, c, e, cnt, total = early
            logger.info(f"kazan early hash hit! '{t}' hash={captcha_hash} via {e} (count={cnt}, {total} total, skipping Tesseract)")
            return t.lower(), f"hash_early_{e}", 0.95

        # Tesseract on deskewed variants for more diversity
        if _init_tesseract():
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH
            tess_whitelist = "abcdefghijklmnopqrstuvwxyz"
            tess_inputs = []
            for label, variant in deskewed_variants[:3]:
                for scale in [4, 3]:
                    try:
                        scaled = _scale_up(variant, scale) if scale > 1 else variant.copy()
                        inverted = ImageOps.invert(scaled.convert("L"))
                        tess_inputs.append((f"tess_{label}_{scale}x", inverted))
                    except Exception:
                        continue
            # Tesseract on thinned image (catches narrow chars EasyOCR merges)
            try:
                thin_for_tess = Image.fromarray(255 - cv2.erode(
                    cv2.threshold(np.array(img.convert("L")), 0, 255,
                                  cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1],
                    np.ones((2, 2), np.uint8), iterations=1))
                for scale in [4, 5]:
                    tess_inputs.append((f"tess_thin_{scale}x",
                                       ImageOps.invert(_scale_up(thin_for_tess, scale).convert("L"))))
            except Exception:
                pass
            # Tesseract on high-contrast
            try:
                for scale in [4, 5]:
                    tess_inputs.append((f"tess_hicon_{scale}x",
                                       ImageOps.invert(_scale_up(high_contrast, scale).convert("L"))))
            except Exception:
                pass
            for label, tess_img in tess_inputs:
                try:
                    for psm in ["7", "13"]:
                        text = pytesseract.image_to_string(
                            tess_img,
                            config=f"--psm {psm} -c tessedit_char_whitelist={tess_whitelist}"
                        ).strip()
                        clean = self._clean_token(text).lower()
                        if clean and len(clean) >= 4:
                            candidates.append((clean, 0.40, f"{label}_psm{psm}"))
                except Exception:
                    continue

        if not candidates:
            return "", "none", 0.0

        logger.debug(f"kazan candidates: {[(t, f'{c:.3f}', e) for t, c, e in sorted(candidates, key=lambda x: -x[1])[:8]]}")

        # Hash validation: if we have the Yii2 hash, find the candidate that matches
        if captcha_hash is not None:
            hash_matches = []
            for text, conf, engine in candidates:
                h = self._yii2_hash(text)
                if h == captcha_hash:
                    hash_matches.append((text, conf, engine))

            if hash_matches:
                # Rank by consensus (frequency × avg confidence) not just max confidence
                from collections import Counter as _Counter
                hm_counts = _Counter(t for t, _, _ in hash_matches)
                hm_confs: dict[str, list[float]] = {}
                for t, c, _ in hash_matches:
                    hm_confs.setdefault(t, []).append(c)
                best_text = max(hm_counts.keys(),
                                key=lambda t: hm_counts[t] * (0.5 + sum(hm_confs[t]) / len(hm_confs[t])))
                best_engine = next(e for t, _, e in hash_matches if t == best_text)
                best_conf = max(c for t, c, _ in hash_matches if t == best_text)
                bc = hm_counts[best_text]
                ret_conf = 0.95 if bc >= 3 else (0.88 if bc >= 2 else 0.78)
                logger.info(f"kazan hash match! '{best_text}' hash={captcha_hash} via {best_engine} "
                            f"(conf={best_conf:.3f}, count={bc}, {len(hash_matches)} total, ret={ret_conf})")
                return best_text.lower(), f"hash_{best_engine}", ret_conf

            # No direct match — try single-char confusion substitutions
            confusions = {
                'l': ['i', '1', 'j', 't'],
                'i': ['l', '1', 'j', 't'],
                'u': ['v', 'w', 'n'],
                'v': ['u', 'w', 'y'],
                'o': ['a', 'e', '0', 'c', 'q'],
                'a': ['o', 'e', 'u'],
                'n': ['m', 'u', 'h', 'r'],
                'm': ['n', 'rn', 'nn'],
                'r': ['n', 'v'],
                'q': ['g', 'p', 'd', '9'],
                'g': ['q', 'y', 'j', '9'],
                'w': ['vv', 'vu', 'uv'],
                'b': ['d', '6'],
                'd': ['b', 'cl'],
                'p': ['q'],
                'e': ['o', 'a', 'c'],
                'f': ['t'],
                't': ['f', 'l'],
                'h': ['b', 'n'],
                'c': ['e', 'o'],
                'x': ['k'],
                'k': ['x'],
                'y': ['v', 'g'],
                'j': ['i', 'l'],
                's': ['z', '5'],
                'z': ['s', '2'],
            }
            for text, conf, engine in sorted(candidates, key=lambda r: -r[1])[:15]:
                for pos in range(len(text)):
                    ch = text[pos]
                    for alt in confusions.get(ch, []):
                        variant = text[:pos] + alt + text[pos+1:]
                        h = self._yii2_hash(variant)
                        if h == captcha_hash:
                            logger.info(f"kazan hash match via confusion! '{text}' -> '{variant}' at pos {pos} ({ch}->{alt}), hash={captcha_hash}")
                            return variant.lower(), f"hash_conf_{engine}", 0.84

            # Two-char confusion substitutions
            for text, conf, engine in sorted(candidates, key=lambda r: -r[1])[:8]:
                for p1 in range(len(text)):
                    for alt1 in confusions.get(text[p1], []):
                        if len(alt1) != 1:
                            continue
                        for p2 in range(p1 + 1, len(text)):
                            for alt2 in confusions.get(text[p2], []):
                                if len(alt2) != 1:
                                    continue
                                variant = text[:p1] + alt1 + text[p1+1:p2] + alt2 + text[p2+1:]
                                h = self._yii2_hash(variant)
                                if h == captcha_hash:
                                    logger.info(f"kazan hash 2-char confusion! '{text}' -> '{variant}' ({text[p1]}->{alt1}, {text[p2]}->{alt2}), hash={captcha_hash}")
                                    return variant.lower(), f"hash_conf2_{engine}", 0.80

            # Insert missing narrow chars — bold italic font drops thin glyphs (i, l, r, t, j)
            ghost_chars = list("ilrtj1")
            for text, conf, engine in sorted(candidates, key=lambda r: -r[1])[:10]:
                for pos in range(len(text) + 1):
                    for gc in ghost_chars:
                        variant = text[:pos] + gc + text[pos:]
                        if self._yii2_hash(variant) == captcha_hash:
                            logger.info(f"kazan hash insert! '{text}' -> '{variant}' (inserted '{gc}' at pos {pos}), hash={captcha_hash}")
                            return variant.lower(), f"hash_ins_{engine}", 0.84

            # Delete extra char — OCR may hallucinate a duplicate
            for text, conf, engine in sorted(candidates, key=lambda r: -r[1])[:10]:
                if len(text) >= 5:
                    for pos in range(len(text)):
                        variant = text[:pos] + text[pos+1:]
                        if self._yii2_hash(variant) == captcha_hash:
                            logger.info(f"kazan hash delete! '{text}' -> '{variant}' (removed pos {pos}), hash={captcha_hash}")
                            return variant.lower(), f"hash_del_{engine}", 0.83

            # Combined: confusion swap + ghost insertion (two simultaneous errors)
            for text, conf, engine in sorted(candidates, key=lambda r: -r[1])[:5]:
                for pos in range(len(text)):
                    for alt in confusions.get(text[pos], []):
                        if len(alt) != 1:
                            continue
                        swapped = text[:pos] + alt + text[pos+1:]
                        for ipos in range(len(swapped) + 1):
                            for gc in ghost_chars:
                                variant = swapped[:ipos] + gc + swapped[ipos:]
                                if self._yii2_hash(variant) == captcha_hash:
                                    logger.info(f"kazan hash swap+insert! '{text}' -> '{variant}' (swap {text[pos]}->{alt}@{pos}, ins {gc}@{ipos}), hash={captcha_hash}")
                                    return variant.lower(), f"hash_si_{engine}", 0.82

            logger.warning(f"kazan: no candidate matches hash {captcha_hash}. Best hashes: {[(t, self._yii2_hash(t)) for t, _, _ in sorted(candidates, key=lambda r: -r[1])[:5]]}")

        # Prefer 5-7 char candidates (kazan captchas are typically this length)
        good_len = [(t, c, e) for t, c, e in candidates if 5 <= len(t) <= 7]
        if good_len and len(good_len) >= 2:
            candidates = good_len

        # Hash-length mismatch: if hash suggests N chars but best candidates are much shorter,
        # the OCR is fundamentally misreading — use very low confidence to force refresh
        fallback_conf = 0.75
        if captcha_hash is not None:
            expected_len = round(captcha_hash / 109.5)
            best_lens = [len(t) for t, c, _ in sorted(candidates, key=lambda r: -r[1])[:5]]
            avg_best_len = sum(best_lens) / len(best_lens) if best_lens else 0
            if abs(expected_len - avg_best_len) >= 1.5:
                fallback_conf = 0.50
                logger.info(f"kazan hash-length mismatch: hash={captcha_hash} suggests ~{expected_len} chars, candidates avg {avg_best_len:.1f} — forcing low conf")

        consensus = self._find_consensus(candidates, "kazan")
        if consensus:
            return consensus.lower(), "consensus", fallback_conf

        best = max(candidates, key=lambda r: r[1] + 0.05 * min(len(r[0]), 7))
        return best[0].lower(), best[2], min(best[1], fallback_conf)

    def _find_consensus(self, candidates: list[tuple[str, float, str]], site_hint: str = "") -> str | None:
        if len(candidates) < 2:
            return None

        tokens = [c[0] for c in candidates]

        # Exact match consensus — weighted by count * avg_confidence
        token_counts = Counter(tokens)
        token_conf: dict[str, float] = {}
        for t, c, _ in candidates:
            token_conf.setdefault(t, []).append(c) if isinstance(token_conf.get(t), list) else None
        token_scores: dict[str, float] = {}
        for t, count in token_counts.items():
            confs = [c for text, c, _ in candidates if text == t]
            avg_conf = sum(confs) / len(confs) if confs else 0
            token_scores[t] = count * (0.5 + avg_conf)

        for token, _ in sorted(token_scores.items(), key=lambda x: -x[1]):
            if token_counts[token] >= 2 and len(token) >= 3:
                logger.info(f"consensus found: '{token}' ({token_counts[token]}/{len(candidates)} engines, score={token_scores[token]:.2f})")
                return token

        # Case-insensitive consensus
        normalized_map: dict[str, list[str]] = {}
        for t in tokens:
            low = re.sub(r"[^a-zA-Z0-9]", "", t.lower())
            if low not in normalized_map:
                normalized_map[low] = []
            normalized_map[low].append(t)

        for norm, originals in sorted(normalized_map.items(), key=lambda x: -len(x[1])):
            if len(originals) >= 2 and len(norm) >= 3:
                best_original = originals[0]
                for c_text, c_conf, _ in sorted(candidates, key=lambda x: -x[1]):
                    if c_text in originals:
                        best_original = c_text
                        break
                logger.info(f"case-insensitive consensus: '{best_original}' ({len(originals)}/{len(candidates)} agree)")
                return best_original

        # Edit-distance-1 consensus: group tokens that differ by at most 1 character
        if len(tokens) >= 3:
            best_cluster = []
            best_repr = ""
            best_score = 0
            unique_tokens = list(set(tokens))
            for i, t1 in enumerate(unique_tokens):
                cluster = [t1]
                for j, t2 in enumerate(unique_tokens):
                    if i != j and len(t1) == len(t2):
                        diffs = sum(1 for a, b in zip(t1, t2) if a != b)
                        if diffs <= 1:
                            cluster.append(t2)
                total_count = sum(token_counts[t] for t in cluster)
                if total_count > best_score and len(cluster) >= 2:
                    best_cluster = cluster
                    best_repr = t1
                    best_score = total_count

            if best_score >= 3 and len(best_repr) >= 3:
                result_chars = []
                for pos in range(len(best_repr)):
                    char_votes = Counter()
                    for t in best_cluster:
                        if pos < len(t):
                            char_votes[t[pos]] += token_counts[t]
                    if char_votes:
                        result_chars.append(char_votes.most_common(1)[0][0])
                consensus_text = "".join(result_chars)
                logger.info(f"edit-distance consensus: '{consensus_text}' from {best_score} votes across {len(best_cluster)} variants")
                return consensus_text

        return None

    def _clean_token(self, text: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "", text)


SolverRegistry.register(OCRSolver())
