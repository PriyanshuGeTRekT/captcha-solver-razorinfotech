from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from src.config import BrowserProfile, SolverConfig

logger = logging.getLogger("captcha_solver")


@dataclass
class _BrowserState:
    """Holds the Playwright instance and browser, with a lock for safe concurrent access."""
    pw: Optional[Playwright] = None
    browser: Optional[Browser] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_state = _BrowserState()


async def _launch_browser(config: SolverConfig) -> tuple[Playwright, Browser]:
    """Launch Playwright and a Chromium browser, returning both handles."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=config.browser_headless,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-setuid-sandbox",
            f"--window-size=1920,1080",
        ],
    )
    return pw, browser


async def get_browser(config: SolverConfig) -> Browser:
    """Get or create a shared browser instance (concurrency-safe)."""
    async with _state.lock:
        if _state.browser is None or not _state.browser.is_connected():
            pw, browser = await _launch_browser(config)
            _state.pw = pw
            _state.browser = browser
        return _state.browser


async def close_browser() -> None:
    """Close the browser and stop the underlying Playwright server process."""
    async with _state.lock:
        if _state.browser:
            await _state.browser.close()
            _state.browser = None
        if _state.pw:
            await _state.pw.stop()
            _state.pw = None


@asynccontextmanager
async def create_context(
    browser: Browser,
    profile: Optional[BrowserProfile] = None,
    proxy: Optional[dict] = None,
    auto_close: bool = True,
) -> AsyncIterator[BrowserContext]:
    context_kwargs: dict = {
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "ignore_https_errors": True,
    }

    if profile and profile.user_data_dir:
        user_dir = Path(profile.user_data_dir)
        user_dir.mkdir(parents=True, exist_ok=True)
        state_file = user_dir / "state.json"
        if state_file.exists():
            context_kwargs["storage_state"] = str(state_file)

    if proxy:
        context_kwargs["proxy"] = proxy

    context = await browser.new_context(**context_kwargs)
    await _apply_stealth(context)
    try:
        yield context
    finally:
        if auto_close:
            await context.close()


async def _apply_stealth(context: BrowserContext) -> None:
    await context.add_init_script("""
    delete Object.getPrototypeOf(navigator).webdriver;
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) :
            originalQuery(parameters)
    );
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
    });
    """)

    await context.add_init_script("""
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) {
            return 'Intel Inc.';
        }
        if (parameter === 37446) {
            return 'Intel Iris OpenGL Engine';
        }
        return getParameter.call(this, parameter);
    };
    """)

    await context.add_init_script("""
    HTMLCanvasElement.prototype.toDataURL = (function(original) {
        return function() {
            const context = this.getContext('2d', {willReadFrequently: true});
            if (context) {
                const imageData = context.getImageData(0, 0, this.width, this.height);
                const data = imageData.data;
                for (let i = 0; i < data.length; i += 4) {
                    if (i % 40 === 0) {
                        data[i + 2] = (data[i + 2] + 1) % 256;
                    }
                }
                context.putImageData(imageData, 0, 0);
            }
            return original.apply(this, arguments);
        };
    })(HTMLCanvasElement.prototype.toDataURL);
    """)
