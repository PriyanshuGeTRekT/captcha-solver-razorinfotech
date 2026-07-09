from __future__ import annotations

import pytest

from src.models import CaptchaType, CaptchaChallenge, CaptchaSolution
from src.solvers.base import SolverRegistry, BaseSolver
from src.solvers.ocr import OCRSolver, _vote_math_answers, _safe_eval_math
from src.solvers.audio import AudioSolver
from src.solvers.image_classifier import ImageClassifierSolver


def test_captcha_challenge_defaults():
    c = CaptchaChallenge()
    assert c.type == CaptchaType.UNKNOWN
    assert c.page_url == ""
    assert c.sitekey == ""


def test_captcha_solution():
    s = CaptchaSolution(type=CaptchaType.IMAGE_CAPTCHA, token="abc123", success=True)
    assert s.token == "abc123"
    assert s.success


def test_solver_registry():
    solvers = SolverRegistry.get_solvers()
    assert len(solvers) >= 3
    names = {s.name for s in solvers}
    assert "ocr" in names
    assert "audio" in names
    assert "image_classifier" in names


def test_ocr_can_solve():
    solver = OCRSolver()
    image_challenge = CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA)
    assert solver.can_solve(image_challenge)
    recaptcha_challenge = CaptchaChallenge(type=CaptchaType.RECAPTCHA_V2)
    assert not solver.can_solve(recaptcha_challenge)


def test_audio_can_solve():
    solver = AudioSolver()
    assert solver.can_solve(CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA, extra={"audio_data": "dummy"}))
    assert not solver.can_solve(CaptchaChallenge(type=CaptchaType.RECAPTCHA_V2))
    assert not solver.can_solve(CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA))


def test_image_classifier_can_solve():
    solver = ImageClassifierSolver()
    assert solver.can_solve(CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA, extra={"image_data": "dummy", "prompt": "bus"}))
    assert solver.can_solve(CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA, extra={"image_data": "dummy", "grid_size": 9}))
    assert not solver.can_solve(CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA, extra={"image_data": "dummy"}))
    assert not solver.can_solve(CaptchaChallenge(type=CaptchaType.RECAPTCHA_V2))


def test_registry_find():
    ocr_challenge = CaptchaChallenge(type=CaptchaType.IMAGE_CAPTCHA)
    solver = SolverRegistry.find(ocr_challenge)
    assert solver is not None
    assert solver.name == "ocr"

    no_match = CaptchaChallenge(type=CaptchaType.GEETEST_V4)
    solver = SolverRegistry.find(no_match)
    assert solver is None


# --- Math-captcha answer-voting (starwars) ---------------------------------

def test_vote_math_consensus_beats_missegmentation():
    # '5+4=' mis-segmented by the component path as '5*4*4', but multiple
    # whole-image reads agree on 5+4 -> the correct answer 9 must win the vote.
    votes = _vote_math_answers(["5+ 4=", "5 + 4", "5+4=", "5*4*4"])
    assert votes[0][0] == 9
    assert votes[0][1] >= 3  # clear consensus margin


def test_vote_math_multidigit_operands():
    # Two-digit operands must be votable (old validator rejected len>1 operands).
    votes = _vote_math_answers(["2 3+ 2 7=", "23+27=", "23 + 27"])
    assert votes[0][0] == 50


def test_vote_math_three_operand():
    votes = _vote_math_answers(["1*9*3=", "1 * 9 * 3", "9*3"])
    assert votes[0][0] == 27


def test_vote_math_range_filter_and_empty():
    # Out-of-range results are not voted; noise / empty yields no votes.
    assert _vote_math_answers([""]) == []
    assert _vote_math_answers(["abc def"]) == []
    # 50*50=2500 is filtered out by the default max_result=200 cap.
    assert all(r <= 200 for r, _, _ in _vote_math_answers(["50*50"]))


def test_safe_eval_math_sandbox():
    assert _safe_eval_math("5+4") == 9
    assert _safe_eval_math("2*3*4") == 24
    assert _safe_eval_math("__import__('os')") is None  # no code execution
    assert _safe_eval_math("") is None
