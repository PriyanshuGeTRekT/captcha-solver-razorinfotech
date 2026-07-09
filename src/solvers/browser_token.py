from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from typing import Optional

from playwright.async_api import Page, Frame

from src.models import CaptchaChallenge, CaptchaSolution, CaptchaType, RateLimitException
from src.solvers.base import BaseSolver
from src.solvers.audio import AudioSolver
from src.solvers.image_classifier import ImageClassifierSolver
from src.behavior import human_click, human_mouse_move, human_prebrowse
from src.utils.selectors import RecaptchaSelectors
from src.utils.model_manager import ModelManager
from src.utils.http import fetch_audio_base64_stealth
from src.config import SolverConfig

logger = logging.getLogger("captcha_solver")


class BrowserTokenSolver(BaseSolver):
    name = "browser_token"

    def __init__(self, config: SolverConfig, page: Page, model_manager: ModelManager | None = None):
        self.config = config
        self.page = page
        self.model_manager = model_manager or ModelManager(
            whisper_model_size=config.audio_model_size,
            clip_model_name=config.clip_model_name,
        )
        self._audio_solver = AudioSolver(model_manager=self.model_manager)
        self._image_solver = ImageClassifierSolver(model_manager=self.model_manager)

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        return challenge.type in (
            CaptchaType.RECAPTCHA_V2,
            CaptchaType.RECAPTCHA_V2_INVISIBLE,
            CaptchaType.RECAPTCHA_V3,
            CaptchaType.RECAPTCHA_ENTERPRISE,
            CaptchaType.HCAPTCHA,
            CaptchaType.HCAPTCHA_INVISIBLE,
            CaptchaType.TURNSTILE,
            CaptchaType.TURNSTILE_INVISIBLE,
        )

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        start = time.time()
        attempts = 0

        for attempt in range(self.config.max_retries):
            attempts += 1
            try:
                if challenge.type in (CaptchaType.TURNSTILE, CaptchaType.TURNSTILE_INVISIBLE):
                    token = await self._solve_turnstile(challenge)
                elif challenge.type in (CaptchaType.HCAPTCHA, CaptchaType.HCAPTCHA_INVISIBLE):
                    token = await self._solve_hcaptcha(challenge)
                else:
                    token = await self._solve_recaptcha(challenge)

                if token:
                    elapsed = (time.time() - start) * 1000
                    logger.info(f"solved {challenge.type.name} in {elapsed:.0f}ms (attempts={attempts})")
                    return CaptchaSolution(
                        type=challenge.type,
                        token=token,
                        solved_via="browser_token",
                        attempts=attempts,
                        elapsed_ms=elapsed,
                        success=True,
                    )

                if attempt < self.config.max_retries - 1:
                    delay = self.config.retry_delay_ms / 1000.0 * (attempt + 1)
                    logger.info(f"retrying in {delay:.1f}s (attempt {attempt + 2}/{self.config.max_retries})")
                    await asyncio.sleep(delay)

            except RateLimitException:
                raise
            except Exception as e:
                logger.warning(f"attempt {attempt + 1} failed: {e}")

        elapsed = (time.time() - start) * 1000
        return CaptchaSolution(
            type=challenge.type,
            success=False,
            error="max retries exhausted",
            attempts=attempts,
            elapsed_ms=elapsed,
        )

    async def _find_recaptcha_bframe(self) -> Optional[Frame]:
        for frame in self.page.frames:
            if "recaptcha/api2/bframe" in frame.url:
                return frame
        return None

    async def _solve_recaptcha(self, challenge: CaptchaChallenge) -> Optional[str]:
        await human_prebrowse(self.page)

        if challenge.type == CaptchaType.RECAPTCHA_V3:
            return await self._extract_v3_token(challenge)

        anchor_frame = await self._find_recaptcha_frame()
        if not anchor_frame:
            logger.warning("reCAPTCHA anchor iframe not found")
            return None

        await self._click_checkbox(anchor_frame)
        await asyncio.sleep(3.0)

        token = await self._extract_recaptcha_response(self.page)
        if token:
            logger.info("got token immediately after checkbox")
            return token

        bframe = await self._find_recaptcha_bframe()
        if not bframe:
            logger.warning("reCAPTCHA bframe not found after checkbox")
            return None

        logger.info("challenge bframe found, solving...")

        for audio_round in range(5):
            body_text = await bframe.locator("body").inner_text()
            if "Select all" in body_text or "Click verify" in body_text:
                if not await self._switch_to_audio(bframe):
                    await self._solve_image_challenge(bframe, self.page)
                    await asyncio.sleep(2.0)
                    token = await self._extract_recaptcha_response(self.page)
                    if token:
                        return token
                    continue

            if "Try again later" in body_text:
                logger.warning("rate limited by Google, waiting 10s before retry")
                await asyncio.sleep(10.0)
                raise RateLimitException("Google rate limit detected")

            answer = await self._solve_audio_challenge(bframe)
            if not answer:
                break

            await self._type_audio_answer(bframe, answer)
            await self._click_verify(bframe)
            await asyncio.sleep(3.0)

            token = await self._extract_recaptcha_response(self.page)
            if token:
                return token

            body_text = await bframe.locator("body").inner_text()
            if "Press PLAY" not in body_text and "Select all" not in body_text:
                break
            logger.info(f"audio round {audio_round + 2} needed...")

        return await self._extract_recaptcha_response(self.page)

    async def _switch_to_audio(self, frame: Frame) -> bool:
        try:
            audio_btn = frame.locator(RecaptchaSelectors.AUDIO_BUTTON).first
            await audio_btn.click(timeout=5000, force=True)
            logger.info("switched to audio challenge")
            await asyncio.sleep(3.0)
            return True
        except Exception:
            return False

    async def _solve_hcaptcha(self, challenge: CaptchaChallenge) -> Optional[str]:
        await human_prebrowse(self.page)

        hcaptcha_frame = await self._find_hcaptcha_frame()
        if not hcaptcha_frame:
            logger.warning("hCaptcha iframe not found")
            return None

        await self._click_hcaptcha_checkbox(hcaptcha_frame)

        await asyncio.sleep(2.0)
        token = await self._extract_hcaptcha_response(self.page)
        if token:
            return token

        await self._solve_image_challenge(hcaptcha_frame, self.page)
        await asyncio.sleep(2.0)
        return await self._extract_hcaptcha_response(self.page)

    async def _solve_turnstile(self, challenge: CaptchaChallenge) -> Optional[str]:
        await human_prebrowse(self.page)
        await asyncio.sleep(2.0)

        for _ in range(10):
            token = await self._extract_turnstile_response(self.page)
            if token:
                return token
            await asyncio.sleep(1.0)

        return None

    async def _find_recaptcha_frame(self) -> Optional[Frame]:
        for _ in range(10):
            for frame in self.page.frames:
                if "recaptcha/api2/anchor" in frame.url or "recaptcha/enterprise/anchor" in frame.url:
                    return frame
            await asyncio.sleep(0.5)
        return None

    async def _find_hcaptcha_frame(self) -> Optional[Frame]:
        for _ in range(10):
            for frame in self.page.frames:
                if "hcaptcha.com/captcha" in frame.url:
                    return frame
            await asyncio.sleep(0.5)
        return None

    async def _click_checkbox(self, frame: Frame) -> None:
        try:
            checkbox = frame.locator(RecaptchaSelectors.CHECKBOX).first
            await checkbox.wait_for(state="visible", timeout=10000)
            box = await checkbox.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                await human_mouse_move(frame.page, x, y)
                await asyncio.sleep(random.uniform(0.1, 0.4))
                await frame.page.mouse.click(x, y, delay=random.randint(50, 200))
                logger.info("clicked reCAPTCHA checkbox")
            else:
                await checkbox.click(delay=random.randint(100, 300))
                logger.info("clicked reCAPTCHA checkbox (direct)")
        except Exception as e:
            logger.warning(f"checkbox click failed: {e}")

    async def _click_hcaptcha_checkbox(self, frame: Frame) -> None:
        try:
            checkbox = frame.locator(RecaptchaSelectors.CHECKBOX_ALTERNATE)
            await human_click(frame.page, checkbox)
            logger.info("clicked hCaptcha checkbox")
        except Exception as e:
            logger.warning(f"hCaptcha checkbox click failed: {e}")

    async def _solve_audio_challenge(self, frame: Frame) -> Optional[str]:
        try:
            body_text = await frame.locator("body").inner_text()
            logger.info(f"frame body snippet: {body_text[:300]}")

            if "Press PLAY" in body_text or "PLAY" in body_text:
                pass
            elif "Select all" in body_text or "Click verify" in body_text:
                audio_btn = frame.locator(RecaptchaSelectors.AUDIO_BUTTON).first
                await audio_btn.click(timeout=5000, force=True)
                logger.info("switched to audio challenge")
                await asyncio.sleep(3.0)
            if "Try again later" in body_text:
                logger.warning("rate limited by Google, waiting 10s before retry")
                await asyncio.sleep(10.0)
                return None

            play_btn = frame.locator(RecaptchaSelectors.PLAY_BUTTON).first
            await play_btn.click(timeout=8000, force=True)
            logger.info("clicked PLAY")
            await asyncio.sleep(4.0)

        except Exception as e:
            logger.warning(f"audio challenge activation failed: {e}")
            return None

        audio_src = await self._extract_audio_src(frame)
        if not audio_src:
            return None

        audio_b64 = None
        try:
            audio_b64 = await fetch_audio_base64_stealth(audio_src)
            logger.info("audio downloaded via curl_cffi")
        except Exception as e:
            logger.warning(f"curl_cffi download failed: {e}, falling back to browser fetch")
            audio_b64 = await self._fetch_audio_browser(frame, audio_src)

        if not audio_b64:
            return None

        challenge = CaptchaChallenge(
            type=CaptchaType.IMAGE_CAPTCHA,
            extra={"audio_data": audio_b64},
        )
        solution = await self._audio_solver.solve(challenge)
        if solution.success:
            logger.info(f"whisper transcribed: '{solution.token}'")
            return solution.token

        return None

    async def _extract_audio_src(self, frame: Frame) -> Optional[str]:
        all_audio = await frame.evaluate("""
        (() => {
            const elements = [];
            document.querySelectorAll('audio, audio source, a[href*="mp3"], a[href*="audio"], .rc-audiochallenge-tdownload-link').forEach(el => {
                elements.push({tag: el.tagName, src: el.src || el.href, id: el.id, class: el.className});
            });
            return elements;
        })()
        """)
        logger.info(f"audio elements found: {all_audio}")

        for item in all_audio:
            if item.get("src"):
                return item["src"]
        return None

    async def _fetch_audio_browser(self, frame: Frame, audio_src: str) -> Optional[str]:
        try:
            audio_b64 = await self.page.evaluate(f"""
            (async () => {{
                const resp = await fetch('{audio_src}');
                const blob = await resp.blob();
                return new Promise((resolve) => {{
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result.split(',')[1]);
                    reader.readAsDataURL(blob);
                }});
            }})()
            """)
            return f"data:audio/mp3;base64,{audio_b64}"
        except Exception as e:
            logger.error(f"browser audio fetch failed: {e}")
        return None

    async def _type_audio_answer(self, frame: Frame, answer: str) -> None:
        try:
            input_field = frame.locator(RecaptchaSelectors.AUDIO_RESPONSE_INPUT).first
            await input_field.fill(answer, timeout=5000)
            logger.info(f"typed audio answer: {answer}")
        except Exception as e:
            logger.warning(f"audio input failed: {e}")

    async def _click_verify(self, frame: Frame) -> None:
        try:
            verify_btn = frame.locator(RecaptchaSelectors.VERIFY_BUTTON).first
            await verify_btn.click(timeout=5000)
            logger.info("clicked verify")
            await asyncio.sleep(2.0)
        except Exception as e:
            logger.warning(f"verify click failed: {e}")

    async def _solve_image_challenge(self, frame: Frame, page: Page) -> None:
        try:
            challenge_title = await frame.locator(
                RecaptchaSelectors.CHALLENGE_TITLE
            ).first.text_content(timeout=5000)
            prompt = challenge_title.strip() if challenge_title else ""
            logger.info(f"image challenge prompt: {prompt}")
        except Exception:
            prompt = ""

        try:
            tiles = frame.locator(RecaptchaSelectors.TILES)
            tile_count = await tiles.count()
            if tile_count == 0:
                logger.warning("no image challenge tiles found")
                return

            tile_images = []
            for i in range(tile_count):
                try:
                    tile = tiles.nth(i)
                    screenshot = await tile.screenshot(type="png")
                    tile_images.append(base64.b64encode(screenshot).decode())
                except Exception as e:
                    logger.warning(f"tile {i} screenshot failed: {e}")
                    tile_images.append("")

            selected_indices = await self._classify_tiles(tile_images, prompt, tile_count)

            if not selected_indices:
                logger.warning("CLIP returned no matches, falling back to random")
                selected_indices = self._fallback_tile_selection(tile_count)

            logger.info(f"selected tiles: {selected_indices}")
            for idx in sorted(selected_indices):
                if idx >= tile_count:
                    continue
                tile = tiles.nth(idx)
                await human_click(page, tile)
                await asyncio.sleep(random.uniform(0.3, 0.8))

            verify_btn = frame.locator(RecaptchaSelectors.VERIFY_BUTTON)
            await human_click(page, verify_btn)
        except Exception as e:
            logger.warning(f"image challenge interaction failed: {e}")

    async def _classify_tiles(
        self, tile_images: list[str], prompt: str, tile_count: int
    ) -> list[int]:
        import torch

        model, preprocess, tokenizer, device = self._image_solver._load_clip_model()
        labels = self._image_solver._get_labels_for_prompt(prompt)
        text_tokens = tokenizer(labels).to(device)

        with torch.no_grad():
            text_features = model.encode_text(text_tokens)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)

        selected = []
        for idx, img_b64 in enumerate(tile_images):
            if not img_b64:
                continue
            try:
                img = self._image_solver._decode_image(img_b64)
                tile = preprocess(img).unsqueeze(0).to(device)
                with torch.no_grad():
                    image_features = model.encode_image(tile)
                    image_features = image_features / image_features.norm(dim=-1, keepdim=True)
                    similarity = (100.0 * image_features @ text_features.T).softmax(dim=-1)
                    best_idx = similarity[0].argmax().item()

                if best_idx < len(labels) - 1 and similarity[0][best_idx] > 0.15:
                    selected.append(idx)
                    logger.info(f"tile {idx}: {labels[best_idx]} (conf={similarity[0][best_idx]:.3f})")
                else:
                    logger.info(f"tile {idx}: no match (best={labels[best_idx]}, conf={similarity[0][best_idx]:.3f})")
            except Exception as e:
                logger.warning(f"tile {idx} classification failed: {e}")

        if len(selected) < 2 and tile_count >= 6:
            selected = self._fallback_tile_selection(tile_count)

        return selected

    def _fallback_tile_selection(self, tile_count: int) -> list[int]:
        count = max(tile_count // 3, 1)
        return random.sample(range(tile_count), min(count, tile_count))

    async def _extract_recaptcha_response(self, page: Page) -> Optional[str]:
        try:
            token = await page.evaluate("""
            (() => {
                const elements = document.querySelectorAll('textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"]');
                if (elements.length > 0) return elements[0].value;
                if (typeof grecaptcha !== 'undefined' && grecaptcha.getResponse) {
                    return grecaptcha.getResponse();
                }
                return null;
            })()
            """)
            if token and len(token) > 10:
                return str(token)
        except Exception:
            pass
        return None

    async def _extract_hcaptcha_response(self, page: Page) -> Optional[str]:
        try:
            token = await page.evaluate("""
            (() => {
                const el = document.querySelector('textarea[name="h-captcha-response"], input[name="h-captcha-response"]');
                if (el) return el.value;
                if (typeof hcaptcha !== 'undefined' && hcaptcha.getResponse) {
                    return hcaptcha.getResponse();
                }
                return null;
            })()
            """)
            if token and len(token) > 10:
                return str(token)
        except Exception:
            pass
        return None

    async def _extract_turnstile_response(self, page: Page) -> Optional[str]:
        try:
            token = await page.evaluate("""
            (() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                if (el) return el.value;
                if (typeof turnstile !== 'undefined' && turnstile.getResponse) {
                    return turnstile.getResponse();
                }
                return null;
            })()
            """)
            if token and len(token) > 10:
                return str(token)
        except Exception:
            pass
        return None

    async def _extract_v3_token(self, challenge: CaptchaChallenge) -> Optional[str]:
        try:
            token = await self.page.evaluate(f"""
            (async () => {{
                if (typeof grecaptcha === 'undefined') return null;
                return new Promise((resolve) => {{
                    grecaptcha.ready(() => {{
                        grecaptcha.execute('{challenge.sitekey}', {{action: '{challenge.action or "homepage"}'}}).then(resolve);
                    }});
                }});
            }})()
            """)
            if token and isinstance(token, str) and len(token) > 10:
                await asyncio.sleep(1.0)
                return str(token)
        except Exception as e:
            logger.warning(f"v3 token extraction failed: {e}")
        return None


# BrowserTokenSolver is manually constructed in router.py — not registered
