"""Form detection, filling, and submission for backlink generation."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Page

logger = logging.getLogger("captcha_solver")


@dataclass
class FormConfig:
    """Per-site form field mapping."""
    name_field: str = "name"
    email_field: str = "mail"
    url_field: str = "url"
    message_field: str = "message"
    captcha_field: str = "captcha"
    captcha_hash_field: str = ""
    submit_button: str = ""
    success_text: str = ""

    @classmethod
    def auto_detect(cls) -> "FormConfig":
        return cls()


@dataclass
class SubmissionResult:
    success: bool
    error: str = ""
    attempts: int = 0
    elapsed_ms: float = 0.0
    response_text: str = ""


class FormSubmitter:
    """Fills and submits guestbook/comment forms with captcha solving."""

    def __init__(self, page: Page):
        self.page = page

    async def detect_fields(self) -> dict[str, str]:
        """Auto-detect form fields on the current page."""
        fields = await self.page.evaluate("""
        (() => {
            const result = {};
            const inputs = document.querySelectorAll('input, textarea, select');
            inputs.forEach(el => {
                const name = (el.name || el.id || '').toLowerCase();
                const type = (el.type || 'text').toLowerCase();
                const placeholder = (el.placeholder || '').toLowerCase();

                // Match name/email/url fields
                if (!result.name && (
                    name.includes('name') || name === 'autor' || name === 'nick' ||
                    placeholder.includes('name') || placeholder.includes('nick')
                ) && type !== 'hidden' && type !== 'submit') {
                    result.name = el.name || el.id;
                }
                if (!result.email && (
                    name.includes('mail') || name.includes('email') || name.includes('e-mail') ||
                    placeholder.includes('mail') || placeholder.includes('@')
                ) && type !== 'hidden' && type !== 'submit') {
                    result.email = el.name || el.id;
                }
                if (!result.url && (
                    name.includes('url') || name.includes('website') || name.includes('homepage') ||
                    placeholder.includes('url') || placeholder.includes('http')
                ) && type !== 'hidden' && type !== 'submit') {
                    result.url = el.name || el.id;
                }
                if (!result.message && (
                    (el.tagName === 'TEXTAREA') ||
                    name.includes('message') || name.includes('comment') || name.includes('text') ||
                    name.includes('nachricht') || name.includes('eintrag') || name.includes('tresc') ||
                    placeholder.includes('message') || placeholder.includes('comment')
                ) && type !== 'hidden' && type !== 'submit') {
                    result.message = el.name || el.id;
                }
                if (!result.captcha && (
                    name.includes('captcha') || name.includes('captcha') || name.includes('kod') ||
                    name.includes('security') || name.includes('verify')
                ) && type !== 'hidden') {
                    result.captcha = el.name || el.id;
                }
                if (!result.captcha_hash && name.includes('captcha') && type === 'hidden') {
                    result.captcha_hash = el.name || el.id;
                }
                if (!result.submit && (
                    type === 'submit' || name.includes('submit') || name.includes('send') ||
                    name.includes('save') || name.includes('add') || name.includes('dodaj')
                )) {
                    result.submit = el.name || el.id;
                }
            });
            // Fallback: find any submit button
            if (!result.submit) {
                const btns = document.querySelectorAll('input[type=\"submit\"], button[type=\"submit\"], button, input[type=\"image\"]');
                if (btns.length > 0) result.submit = btns[0].name || btns[0].id || 'button';
            }
            return result;
        })()
        """)
        logger.info(f"detected fields: {fields}")
        return fields

    async def fill_form(
        self,
        name: str,
        email: str,
        url: str,
        message: str,
        captcha_token: str,
        config: FormConfig | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        """Fill all form fields and the captcha answer."""
        if fields is None:
            fields = await self.detect_fields()

        cfg = config or FormConfig()

        mappings = {
            "name": name,
            "email": email,
            "url": url,
            "message": message,
            "captcha": captcha_token,
        }

        for field_type, value in mappings.items():
            selector = fields.get(field_type, "")
            if not selector:
                # Try known fallback names
                for fallback in self._get_fallbacks(field_type):
                    try:
                        el = self.page.locator(f"input[name='{fallback}'], textarea[name='{fallback}'], input[id='{fallback}']").first
                        if await el.count() > 0:
                            await el.fill(value, timeout=5000)
                            logger.info(f"filled {field_type} via fallback '{fallback}'")
                            break
                    except Exception:
                        continue
                continue
            try:
                el = self.page.locator(f"[name='{selector}'], #{selector}").first
                if await el.count() > 0:
                    await el.fill(value, timeout=5000)
                    logger.info(f"filled {field_type} -> {selector}")
                else:
                    logger.warning(f"field '{selector}' not found for {field_type}")
            except Exception as e:
                logger.warning(f"fill {field_type} failed: {e}")

    async def submit(self, fields: dict[str, str] | None = None, timeout: int = 15000) -> SubmissionResult:
        """Click submit and detect success/failure."""
        if fields is None:
            fields = await self.detect_fields()

        submit_sel = fields.get("submit", "")
        if not submit_sel:
            # Try clicking generic submit buttons
            for sel in [
                "input[type='submit']",
                "button[type='submit']",
                "input[type='image']",
                "button:has-text('Send')",
                "button:has-text('Submit')",
                "button:has-text('Dodaj')",
                "button:has-text('Senden')",
            ]:
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0:
                        submit_sel = sel
                        break
                except Exception:
                    continue

        if not submit_sel:
            return SubmissionResult(success=False, error="no submit button found")

        try:
            btn = self.page.locator(submit_sel).first
            if "[" not in submit_sel and "#" != submit_sel[0]:
                btn = self.page.locator(f"[name='{submit_sel}'], #{submit_sel}").first

            await btn.click(timeout=5000)
            logger.info(f"clicked submit: {submit_sel}")
            await asyncio.sleep(2.0)
        except Exception as e:
            return SubmissionResult(success=False, error=f"submit click failed: {e}")

        await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        await asyncio.sleep(1.0)

        body = await self.page.locator("body").inner_text()
        body_lower = body[:2000].lower()

        success_keywords = [
            "success", "thank you", "dodano", "entry added", "eintrag",
            "added", "published", "comment added", "your message",
            "erfolgreich", "wurde hinzugef", "zostal dodany",
            "moderation", "awaiting", "approval", "wartet",
            "freigeschaltet", "publikacji",
        ]
        failure_keywords = [
            "error", "wrong captcha", "invalid captcha", "incorrect",
            "try again", "failed", "błędny", "falsch", "fehler",
            "nieprawidlowy", "captcha incorrect",
        ]

        for kw in failure_keywords:
            if kw in body_lower:
                return SubmissionResult(
                    success=False,
                    error=f"detected failure keyword: '{kw}'",
                    response_text=body[:500],
                )

        for kw in success_keywords:
            if kw in body_lower:
                return SubmissionResult(
                    success=True,
                    response_text=body[:500],
                )

        return SubmissionResult(
            success=True,
            response_text=body[:500],
        )

    def _get_fallbacks(self, field_type: str) -> list[str]:
        fallbacks = {
            "name": ["name", "autor", "nick", "nickname", "username"],
            "email": ["mail", "email", "e_mail", "email_adresse"],
            "url": ["url", "website", "homepage", "www"],
            "message": ["message", "comment", "text", "nachricht", "eintrag", "tresc", "body"],
            "captcha": ["captcha", "captcha", "kod", "security_code", "verify"],
        }
        return fallbacks.get(field_type, [])
