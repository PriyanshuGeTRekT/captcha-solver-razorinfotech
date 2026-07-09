"""Validate the generic DOM field auto-detector against unseen guestbook forms."""
import asyncio
import sys
sys.path.insert(0, ".")
from playwright.async_api import async_playwright
from web_server import _FIELD_DETECT_JS, _augment_config_from_dom
from src.site_configs import make_generic_config

# A guestbook form whose field NAMES match none of the built-in 5 configs, using
# underscores + camelCase to stress the token normalizer. Includes decoy inputs
# (a search box + a login form) that must NOT be picked.
FIXTURE = """
<!doctype html><html><body>
<form action="/search"><input name="q" placeholder="Search site"><button type="submit">Go</button></form>
<form action="/login">
  <input name="login_user" placeholder="Username">
  <input type="password" name="login_pass">
  <button type="submit">Log in</button>
</form>
<h2>Sign our guestbook</h2>
<form action="/guestbook/save" method="post">
  <label>Your Name <input type="text" name="visitor_nick"></label>
  <label>E-Mail <input type="email" name="contact_email"></label>
  <label>Homepage <input type="url" name="your_homepage"></label>
  <label>Town <input type="text" name="home_town"></label>
  <label>Message <textarea name="guest_comment"></textarea></label>
  <input type="hidden" name="captchaHash" value="abc123">
  <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==" alt="captcha image" width="120" height="40">
  <label>Enter code <input type="text" name="verifyCode"></label>
  <button type="submit" name="do_sign">Post Entry</button>
</form>
</body></html>
"""

EXPECT = {
    "name": "visitor_nick",
    "email": "contact_email",
    "url": "your_homepage",
    "city": "home_town",
    "message": "guest_comment",
    "captcha": "verifyCode",
    "captcha_hash": "captchaHash",
    "submit_name": "do_sign",
}


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.set_content(FIXTURE)

        detected = await page.evaluate(_FIELD_DETECT_JS)
        print("RAW DETECTED:", detected)

        ok = True
        for k, want in EXPECT.items():
            got = detected.get(k)
            mark = "OK " if got == want else "!! "
            if got != want:
                ok = False
            print(f"  {mark}{k:12} expected={want!r:18} got={got!r}")

        # Also exercise the full augment step end-to-end on a generic config.
        cfg = make_generic_config("https://newguestbook.example/guestbook/save")
        cfg2 = await _augment_config_from_dom(page, cfg)
        print("\nAUGMENTED CONFIG:")
        for a in ("name_field", "email_field", "url_field", "city_field",
                  "message_field", "captcha_field", "captcha_hash_field", "submit_selector"):
            print(f"  {a:20} = {getattr(cfg2, a)!r}")

        # Decoys must not be chosen
        for bad in ("q", "login_user", "login_pass"):
            if bad in detected.values():
                ok = False
                print(f"  !! DECOY PICKED: {bad}")

        await browser.close()
        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
