from __future__ import annotations

import logging
import re

from playwright.async_api import Page

from src.models import CaptchaChallenge, CaptchaType

logger = logging.getLogger("captcha_solver")


CAPTCHA_SIGNATURES: dict[CaptchaType, dict] = {
    CaptchaType.RECAPTCHA_V2: {
        "globals": ["grecaptcha"],
        "selectors": ["div.g-recaptcha", "iframe[src*='recaptcha/api2/anchor']",
                       "iframe[title*='recaptcha']", "div[data-sitekey]"],
    },
    CaptchaType.HCAPTCHA: {
        "globals": ["hcaptcha"],
        "selectors": ["div.h-captcha", "iframe[src*='hcaptcha.com/captcha']",
                       "iframe[src*='newassets.hcaptcha.com']"],
    },
    CaptchaType.TURNSTILE: {
        "globals": ["turnstile"],
        "selectors": ["div.cf-turnstile", "iframe[src*='challenges.cloudflare.com']"],
    },
    CaptchaType.FUNCAPTCHA: {
        "globals": [],
        "selectors": ["iframe[src*='funcaptcha']", "iframe[src*='arkoselabs']"],
    },
    CaptchaType.GEETEST_V3: {
        "globals": [],
        "selectors": ["div.geetest_captcha", "iframe[src*='gee']",
                       "script[src*='gee']", "script[src*='gt.js']"],
    },
    CaptchaType.AWS_WAF: {
        "globals": [],
        "selectors": ["iframe[src*='aws']", "div[data-aws-captcha]",
                       "script[src*='aws']"],
    },
}


async def detect_captcha(page: Page, page_url: str) -> CaptchaChallenge:
    challenge = CaptchaChallenge(page_url=page_url)

    js_result = await page.evaluate("""
    (() => {
        const types = [];
        if (typeof grecaptcha !== 'undefined') types.push('grecaptcha');
        if (typeof hcaptcha !== 'undefined') types.push('hcaptcha');
        if (typeof turnstile !== 'undefined') types.push('turnstile');

        const scripts = Array.from(document.querySelectorAll('script')).map(s => s.src).join('|');

        // Extract only what we need instead of full page source
        const sitekeyEl = document.querySelector('[data-sitekey]');
        const sitekey = sitekeyEl ? sitekeyEl.getAttribute('data-sitekey') : '';

        const dataSEl = document.querySelector('[data-s]');
        const dataS = dataSEl ? dataSEl.getAttribute('data-s') : '';

        const actionEl = document.querySelector('[data-action]');
        const action = actionEl ? actionEl.getAttribute('data-action') : '';

        const sizeEl = document.querySelector('[data-size="invisible"]');
        const isInvisible = !!sizeEl;

        // Check for specific patterns in a limited scope (first 50KB of HTML)
        const limitedSource = document.documentElement.outerHTML.substring(0, 50000);

        const hasEnterprise = limitedSource.includes('recaptcha/enterprise.js') || limitedSource.includes('recaptcha.net');
        const hasV3 = limitedSource.includes('recaptcha/api.js?render=') || limitedSource.includes('grecaptcha.execute');
        const hasFuncaptcha = limitedSource.includes('funcaptcha') || limitedSource.includes('arkoselabs');
        const hasGeetest = limitedSource.toLowerCase().includes('geetest');
        const hasDatadome = limitedSource.toLowerCase().includes('datadome');
        const hasMtcaptcha = limitedSource.toLowerCase().includes('mtcaptcha');
        const hasAws = scripts.toLowerCase().includes('aws') && (limitedSource.toLowerCase().includes('captcha') || limitedSource.toLowerCase().includes('challenge'));

        return { types, scripts, sitekey, dataS, action, isInvisible, hasEnterprise, hasV3, hasFuncaptcha, hasGeetest, hasDatadome, hasMtcaptcha, hasAws, limitedSource };
    })()
    """)

    globals_list = js_result.get("types", [])
    scripts = js_result.get("scripts", "")

    _classify_recaptcha(challenge, globals_list, js_result, scripts)
    if challenge.type != CaptchaType.UNKNOWN:
        return challenge

    _classify_hcaptcha(challenge, globals_list, js_result)
    if challenge.type != CaptchaType.UNKNOWN:
        return challenge

    _classify_turnstile(challenge, globals_list, js_result)
    if challenge.type != CaptchaType.UNKNOWN:
        return challenge

    _classify_other(challenge, js_result, scripts)
    if challenge.type != CaptchaType.UNKNOWN:
        return challenge

    img_captcha = await _detect_image_captcha(page)
    if img_captcha:
        challenge.type = CaptchaType.IMAGE_CAPTCHA
        return challenge

    return challenge


def _classify_recaptcha(
    challenge: CaptchaChallenge,
    globals: list[str],
    js_result: dict,
    scripts: str,
) -> None:
    if "grecaptcha" not in globals:
        if "recaptcha/api.js" not in scripts and "recaptcha/enterprise.js" not in scripts:
            return

    is_enterprise = js_result.get("hasEnterprise", False)
    is_v3 = js_result.get("hasV3", False)

    if is_enterprise:
        challenge.type = CaptchaType.RECAPTCHA_ENTERPRISE
    elif is_v3:
        challenge.type = CaptchaType.RECAPTCHA_V3
    elif "grecaptcha" in globals:
        challenge.type = CaptchaType.RECAPTCHA_V2

    sitekey = js_result.get("sitekey", "")
    if sitekey:
        challenge.sitekey = sitekey

    data_s = js_result.get("dataS", "")
    if data_s:
        challenge.data_s = data_s
        challenge.extra["s_value"] = data_s

    action = js_result.get("action", "")
    if action:
        challenge.action = action

    challenge.is_invisible = js_result.get("isInvisible", False)


def _classify_hcaptcha(
    challenge: CaptchaChallenge,
    globals: list[str],
    js_result: dict,
) -> None:
    limited_source = js_result.get("limitedSource", "")
    if "hcaptcha" not in globals and "hcaptcha.com" not in limited_source:
        return

    challenge.type = CaptchaType.HCAPTCHA
    sitekey = js_result.get("sitekey", "")
    if sitekey:
        challenge.sitekey = sitekey

    challenge.is_invisible = js_result.get("isInvisible", False)


def _classify_turnstile(
    challenge: CaptchaChallenge,
    globals: list[str],
    js_result: dict,
) -> None:
    limited_source = js_result.get("limitedSource", "")
    if "turnstile" not in globals and "challenges.cloudflare.com" not in limited_source:
        return

    challenge.type = CaptchaType.TURNSTILE
    sitekey = js_result.get("sitekey", "")
    if sitekey:
        challenge.sitekey = sitekey


def _classify_other(
    challenge: CaptchaChallenge,
    js_result: dict,
    scripts: str,
) -> None:
    limited_source = js_result.get("limitedSource", "")

    if js_result.get("hasFuncaptcha", False) or "arkoselabs" in scripts:
        challenge.type = CaptchaType.FUNCAPTCHA
        pk_match = re.search(r'public_key\s*[:=]\s*["\']([^"\']+)["\']', limited_source)
        if pk_match:
            challenge.sitekey = pk_match.group(1)
        return

    if js_result.get("hasGeetest", False) or "gt.js" in scripts:
        challenge.type = CaptchaType.GEETEST_V3
        gt_match = re.search(r'(?:gt)\s*[:=]\s*["\']([^"\']+)["\']', limited_source)
        if gt_match:
            challenge.sitekey = gt_match.group(1)
        return

    if js_result.get("hasAws", False):
        challenge.type = CaptchaType.AWS_WAF
        return

    if js_result.get("hasDatadome", False):
        challenge.type = CaptchaType.DATADOME
        return

    if js_result.get("hasMtcaptcha", False):
        challenge.type = CaptchaType.MT_CAPTCHA
        sitekey = js_result.get("sitekey", "")
        if sitekey:
            challenge.sitekey = sitekey
        return


async def _detect_image_captcha(page: Page) -> bool:
    try:
        img_count = await page.evaluate("""
        (() => {
            const imgs = document.querySelectorAll(
                'img[src*="captcha"], img[class*="captcha"], img[id*="captcha"], ' +
                'img[src*="tmp/"], img[src*="Captcha"], ' +
                'input[type="image"][src*="tmp/"]'
            );
            if (imgs.length > 0) return imgs.length;
            const captchaInput = document.querySelector('input[name="captcha"], input[name*="captcha"], input[id*="captcha"]');
            if (captchaInput) return 1;
            return 0;
        })()
        """)
        return int(img_count) > 0
    except Exception:
        return False
