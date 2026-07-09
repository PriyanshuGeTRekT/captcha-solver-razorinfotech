from __future__ import annotations

import asyncio
import functools
import json
import sys
import time
from typing import Any, Callable, Coroutine, Optional

import click

from src.config import AppConfig, SolverConfig, get_config, save_config
from src.utils.logging import setup_logging
from src.models import CaptchaType
from src.profiles import ProfileManager
from src.router import StrategyRouter
from src.browser import get_browser, close_browser


def async_command(f: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(f(*args, **kwargs))
    return wrapper


CAPTCHA_TYPE_MAP = {
    "image": CaptchaType.IMAGE_CAPTCHA,
    "recaptcha-v2": CaptchaType.RECAPTCHA_V2,
    "recaptcha-v2-invisible": CaptchaType.RECAPTCHA_V2_INVISIBLE,
    "recaptcha-v3": CaptchaType.RECAPTCHA_V3,
    "recaptcha-enterprise": CaptchaType.RECAPTCHA_ENTERPRISE,
    "hcaptcha": CaptchaType.HCAPTCHA,
    "hcaptcha-invisible": CaptchaType.HCAPTCHA_INVISIBLE,
    "turnstile": CaptchaType.TURNSTILE,
    "funcaptcha": CaptchaType.FUNCAPTCHA,
    "geetest": CaptchaType.GEETEST_V3,
    "aws": CaptchaType.AWS_WAF,
    "auto": None,
}


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option("--config", "config_path", type=click.Path(), help="Config file path")
@click.pass_context
def cli(ctx: click.Context, debug: bool, config_path: Optional[str]) -> None:
    setup_logging("DEBUG" if debug else "INFO")
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


@cli.command()
@click.option("--url", "-u", required=True, help="Target page URL")
@click.option("--type", "-t", "captcha_type", default="auto",
              type=click.Choice(list(CAPTCHA_TYPE_MAP.keys())),
              help="Force specific captcha type")
@click.option("--profile", "-p", default="default", help="Browser profile name")
@click.option("--proxy", "-x", default=None, help="Proxy URL (e.g. socks5://user:pass@host:port)")
@click.option("--headless", is_flag=True, help="Run browser in headless mode")
@click.option("--keep-open", is_flag=True, help="Keep browser open after solving for manual verification")
@async_command
async def solve(
    url: str,
    captcha_type: str,
    profile: str,
    proxy: Optional[str],
    headless: bool,
    keep_open: bool,
) -> None:
    """Detect and solve a CAPTCHA on a webpage."""
    config = get_config()
    if headless:
        config.solver.browser_headless = True
    if proxy:
        from src.config import ProxyConfig

        pm = ProfileManager()
        profile_obj = pm.get_or_create(profile)
        profile_obj.proxy = ProxyConfig(server=proxy)
        config.add_profile(profile_obj)
        save_config()

    force_type = CAPTCHA_TYPE_MAP[captcha_type]
    type_label = force_type.name if force_type else "auto-detect"
    click.echo(f"Solving CAPTCHA at {url} (type: {type_label}, profile: {profile})")

    try:
        browser = await get_browser(config.solver)
        pm = ProfileManager()
        router = StrategyRouter(config.solver, browser, pm)
        solution = await router.solve(url, profile_name=profile, force_type=force_type, keep_open=keep_open)

        if solution.success:
            click.echo(f"\nSolved ({solution.solved_via}, {solution.elapsed_ms:.0f}ms, {solution.attempts} attempts)")
            click.echo(f"Token: {solution.token[:80]}{'...' if len(solution.token) > 80 else ''}")
        else:
            click.echo(f"\nFailed: {solution.error}", err=True)
            sys.exit(1)
    finally:
        if keep_open:
            click.echo("\nBrowser kept open for manual verification. Press Enter to close...")
            input()
        await close_browser()


@cli.command()
@click.option("--url", "-u", required=True, help="Target page URL")
@async_command
async def detect(url: str) -> None:
    """Detect CAPTCHA type without solving."""
    from src.detector import detect_captcha

    config = get_config()
    browser = await get_browser(config.solver)
    try:
        from src.browser import create_context

        async with create_context(browser) as context:
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3.0)
            challenge = await detect_captcha(page, url)
            click.echo(f"Type: {challenge.type.name}")
            click.echo(f"Sitekey: {challenge.sitekey or 'N/A'}")
            click.echo(f"Action: {challenge.action or 'N/A'}")
            click.echo(f"Invisible: {challenge.is_invisible}")
            click.echo(f"Extra: {challenge.extra}")
    finally:
        await close_browser()


@cli.command()
@click.option("--url", "-u", required=True, help="Target page URL (submission form)")
@click.option("--name", "-n", required=True, help="Name to submit")
@click.option("--email", "-e", required=True, help="Email to submit")
@click.option("--link", "-l", required=True, help="Backlink URL to submit")
@click.option("--message", "-m", default="Great site, keep up the good work!", help="Message text")
@click.option("--max-retries", default=3, help="Max captcha retries on failure")
@click.option("--headless", is_flag=True, help="Run browser in headless mode")
@async_command
async def backlink(
    url: str,
    name: str,
    email: str,
    link: str,
    message: str,
    max_retries: int,
    headless: bool,
) -> None:
    """Submit a backlink to a guestbook/comment form with captcha solving."""
    from src.submitter import FormSubmitter, SubmissionResult

    config = get_config()
    if headless:
        config.solver.browser_headless = True

    config.solver.max_retries = max_retries
    click.echo(f"Target: {url}")
    click.echo(f"Backlink: {link}\n")

    browser = await get_browser(config.solver)
    pm = ProfileManager()
    router = StrategyRouter(config.solver, browser, pm)

    from src.browser import create_context

    start = time.time()
    overall_success = False

    try:
        async with create_context(browser, auto_close=False) as context:
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3.0)

            submitter = FormSubmitter(page)
            fields = await submitter.detect_fields()

            click.echo(f"Detected fields: {json.dumps({k: v for k, v in fields.items() if v}, indent=2)}")

            for attempt in range(1, max_retries + 1):
                click.echo(f"\n--- Attempt {attempt}/{max_retries} ---")

                # Solve captcha
                captcha_solution = await router.solve_on_page(page, url)

                if not captcha_solution.success:
                    click.echo(f"Captcha solve failed: {captcha_solution.error}")
                    if attempt < max_retries:
                        click.echo("Retrying...")
                        continue
                    break

                captcha_token = captcha_solution.token
                click.echo(f"Captcha solved: '{captcha_token}' via {captcha_solution.solved_via}")

                # Fill form
                await submitter.fill_form(
                    name=name,
                    email=email,
                    url=link,
                    message=message,
                    captcha_token=captcha_token,
                    fields=fields,
                )
                await asyncio.sleep(0.5)

                # Submit
                result = await submitter.submit(fields=fields)
                click.echo(f"Submit result: {'SUCCESS' if result.success else 'FAILED'}")

                if result.success:
                    click.echo(f"Response: {result.response_text[:200]}")
                    overall_success = True
                    break
                else:
                    click.echo(f"Error: {result.error}")
                    click.echo(f"Response: {result.response_text[:200]}")
                    if attempt < max_retries:
                        click.echo("Captcha likely wrong, retrying...")
                        await page.reload(wait_until="domcontentloaded")
                        await asyncio.sleep(2.0)

    finally:
        await close_browser()

    elapsed = (time.time() - start) * 1000
    click.echo(f"\n{'SUCCESS' if overall_success else 'FAILED'} ({elapsed:.0f}ms)")

    if not overall_success:
        sys.exit(1)


@cli.group()
def profiles() -> None:
    """Manage browser profiles."""


@profiles.command("list")
@async_command
async def profiles_list() -> None:
    """List all browser profiles."""
    pm = ProfileManager()
    for p in pm.list():
        click.echo(f"  {p.name}: dir={p.user_data_dir}, uses={p.use_count}, success_rate={p.success_count/max(p.use_count,1)*100:.0f}%")


@profiles.command("create")
@click.option("--name", "-n", required=True, help="Profile name")
@click.option("--proxy", "-x", default=None, help="Proxy for this profile")
@async_command
async def profiles_create(name: str, proxy: Optional[str]) -> None:
    """Create a new browser profile."""
    from src.config import ProxyConfig

    proxy_cfg = ProxyConfig(server=proxy) if proxy else None
    pm = ProfileManager()
    profile = pm.create(name, proxy=proxy_cfg)
    click.echo(f"Created profile '{profile.name}' at {profile.user_data_dir}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
