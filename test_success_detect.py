"""Validate generic success/failure detection: robust backlink count + error node."""
import asyncio
import sys
sys.path.insert(0, ".")
from playwright.async_api import async_playwright
from web_server import _count_backlink_occurrences, _detect_visible_error

FRAG = "t1.com"

# Case A: a REAL posted entry — backlink shows in the comment list (a div), and is
# ALSO echoed back into the form textarea. Robust count must see it ONCE (the div),
# not twice, and must be > 0.
REAL_POST = """
<!doctype html><html><body>
  <div class="entries">
    <div class="entry">Nice site! https://t1.com</div>
  </div>
  <form>
    <textarea name="text">Nice site!

https://t1.com</textarea>
    <input name="captcha">
    <button type="submit">Sign</button>
  </form>
</body></html>
"""

# Case B: a validation ERROR re-render — the backlink is ONLY echoed in the form
# textarea (NOT posted anywhere), and a visible error banner is shown. Robust count
# must be 0 (no false success), and the error must be detected.
ERROR_ECHO = """
<!doctype html><html><body>
  <div class="alert-danger">The captcha code was incorrect. Please try again.</div>
  <form>
    <textarea name="text">Nice site!

https://t1.com</textarea>
    <input name="captcha">
    <button type="submit">Sign</button>
  </form>
</body></html>
"""

# Case C: fresh page (baseline) — backlink absent, no error.
FRESH = """
<!doctype html><html><body>
  <div class="entries"></div>
  <form><textarea name="text"></textarea><input name="captcha"><button type="submit">Sign</button></form>
</body></html>
"""


async def main():
    ok = True
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        await page.set_content(FRESH)
        before = await _count_backlink_occurrences(page, FRAG)
        err_fresh = await _detect_visible_error(page)
        print(f"FRESH:      count={before} (want 0)   error={err_fresh!r} (want '')")
        ok &= (before == 0 and err_fresh == "")

        await page.set_content(REAL_POST)
        real = await _count_backlink_occurrences(page, FRAG)
        err_real = await _detect_visible_error(page)
        print(f"REAL_POST:  count={real} (want 1, i.e. >{before})   error={err_real!r} (want '')")
        ok &= (real == 1 and real > before and err_real == "")

        await page.set_content(ERROR_ECHO)
        echo = await _count_backlink_occurrences(page, FRAG)
        err_echo = await _detect_visible_error(page)
        print(f"ERROR_ECHO: count={echo} (want 0, NOT >{before})   error={err_echo!r} (want non-empty)")
        ok &= (echo == 0 and not (echo > before) and bool(err_echo))

        await browser.close()
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
