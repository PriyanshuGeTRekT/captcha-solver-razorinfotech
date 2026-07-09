from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from playwright.async_api import Browser

from src.models import CaptchaChallenge, CaptchaSolution, CaptchaType
from src.solvers.base import SolverRegistry
from src.solvers.browser_token import BrowserTokenSolver
from src.solvers import ocr as _  # register OCR solver
from src.solvers import audio as _  # register Audio solver
from src.solvers import image_classifier as _  # register ImageClassifier solver
from src.solvers import geetest as _  # register GeeTest solver
from src.solvers import funcaptcha as _  # register FunCaptcha solver
from src.browser import create_context
from src.config import SolverConfig
from src.detector import detect_captcha
from src.profiles import ProfileManager
from src.utils.model_manager import ModelManager

logger = logging.getLogger("captcha_solver")


class StrategyRouter:
    def __init__(
        self,
        config: SolverConfig,
        browser: Browser,
        profile_manager: Optional[ProfileManager] = None,
    ):
        self.config = config
        self.browser = browser
        self.profiles = profile_manager or ProfileManager()
        self.model_manager = ModelManager(
            whisper_model_size=config.audio_model_size,
            clip_model_name=config.clip_model_name,
            gpu=getattr(config, "gpu", True),
        )

    async def solve(
        self,
        page_url: str,
        profile_name: str = "default",
        force_type: Optional[CaptchaType] = None,
        keep_open: bool = False,
    ) -> CaptchaSolution:
        profile = self.profiles.get_or_create(profile_name)
        proxy_config = profile.proxy.as_playwright() if profile.proxy else None

        async with create_context(self.browser, profile, proxy_config, auto_close=not keep_open) as context:
            page = await context.new_page()
            await self._prewarm_page(page, page_url)
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3.0)
            return await self.solve_on_page(page, page_url, force_type)

    async def solve_on_page(
        self,
        page,
        page_url: str,
        force_type: Optional[CaptchaType] = None,
        profile_name: str = "default",
    ) -> CaptchaSolution:
        start = time.time()
        challenge = None

        try:
            if force_type:
                challenge = CaptchaChallenge(type=force_type, page_url=page_url)
                await self._enhance_challenge(challenge, page)
            else:
                challenge = await detect_captcha(page, page_url)
                logger.info(f"detected captcha type: {challenge.type.name}")

            if challenge.type == CaptchaType.UNKNOWN:
                return CaptchaSolution(type=CaptchaType.UNKNOWN, success=False, error="no captcha detected on page")

            if challenge.type == CaptchaType.IMAGE_CAPTCHA:
                solution = await self._solve_image_captcha_with_retry(challenge, page, page_url)
                self.profiles.record_use(profile_name, solution.success)
                return solution

            solver = SolverRegistry.find(challenge)
            if solver and solver.name != "browser_token":
                challenge.extra["page"] = page
                solution = await solver.solve(challenge)
            else:
                browser_solver = BrowserTokenSolver(self.config, page, model_manager=self.model_manager)
                solution = await browser_solver.solve(challenge)

            self.profiles.record_use(profile_name, solution.success)
            return solution

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            logger.error(f"routing error: {e}")
            return CaptchaSolution(
                type=challenge.type if challenge else CaptchaType.UNKNOWN,
                success=False,
                error=str(e),
                elapsed_ms=elapsed,
            )

    async def _prewarm_page(self, page, target_url: str) -> None:
        prewarm_sites = ["https://en.wikipedia.org", "https://www.bing.com"]
        for site in prewarm_sites[:1]:
            try:
                await page.goto(site, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1.5)
                await page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.5)
                await page.evaluate("window.scrollBy(0, -200)")
                await asyncio.sleep(0.5)
            except Exception:
                pass

    async def _enhance_challenge(self, challenge: CaptchaChallenge, page) -> None:
        try:
            detected = await detect_captcha(page, challenge.page_url)
            if detected.sitekey:
                challenge.sitekey = detected.sitekey
            if detected.action:
                challenge.action = detected.action
            if detected.is_invisible:
                challenge.is_invisible = detected.is_invisible
            challenge.extra.update(detected.extra)
        except Exception:
            pass

    async def _extract_image_captcha(self, challenge: CaptchaChallenge, page) -> None:
        import base64
        from pathlib import Path

        selectors = [
            "img[src*='captcha']", "img[class*='captcha']", "img[id*='captcha']",
            "img[src*='Captcha']", "img[src*='kod']", "img[src*='img']",
            "img[src*='tmp/']", "input[type='image']",
        ]
        for selector in selectors:
            try:
                img = page.locator(selector).first
                if await img.count() > 0:
                    screenshot = await img.screenshot(type="png")
                    challenge.extra["image_data"] = base64.b64encode(screenshot).decode()
                    logger.info(f"extracted captcha image via '{selector}'")
                    screenshots_dir = Path("screenshots")
                    screenshots_dir.mkdir(exist_ok=True)
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    path = screenshots_dir / f"captcha_{ts}.png"
                    path.write_bytes(screenshot)
                    logger.info(f"saved captcha screenshot to {path}")
                    return
            except Exception:
                continue
        logger.warning("could not extract captcha image from page")

    async def _solve_image_captcha_with_retry(
        self, challenge: CaptchaChallenge, page, page_url: str
    ) -> CaptchaSolution:
        max_image_retries = 3
        best_solution = None

        for retry_num in range(max_image_retries):
            if retry_num > 0:
                logger.info(f"retrying image captcha with fresh image (attempt {retry_num + 1}/{max_image_retries})")
                try:
                    reload_btn = page.locator("a[href*='captcha'], img[src*='captcha'], button[title*='odswie'], input[type='image']").first
                    if await reload_btn.count() > 0:
                        await reload_btn.click()
                    else:
                        await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(2.0)
                except Exception:
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(2.0)
                challenge.extra.clear()

            await self._extract_image_captcha(challenge, page)
            if "image_data" not in challenge.extra:
                continue

            solver = SolverRegistry.find(challenge)
            if solver:
                solution = await solver.solve(challenge)
            else:
                solution = CaptchaSolution(type=CaptchaType.IMAGE_CAPTCHA, success=False, error="no solver")

            if solution.success:
                if best_solution is None:
                    best_solution = solution
                return solution

            if best_solution is None:
                best_solution = solution
            elif solution.success and not best_solution.success:
                best_solution = solution

        return best_solution or CaptchaSolution(
            type=CaptchaType.IMAGE_CAPTCHA, success=False, error="image captcha retries exhausted",
        )
