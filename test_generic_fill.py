"""End-to-end generic-path test: treat a real form as an UNKNOWN user-added site,
run make_generic_config -> auto-detect -> fill, and read back the filled values.
Proves the generic pipeline correctly fills a real form it has no hardcoded config
for. Does NOT submit (no spam) — submit/solve are the same code the built-ins use."""
import asyncio, sys
sys.path.insert(0, ".")
from playwright.async_api import async_playwright
from web_server import _augment_config_from_dom, _fill_form_humanized
from src.site_configs import make_generic_config

URL = sys.argv[1] if len(sys.argv) > 1 else "https://geini.de/index.php/guestbook/index/newentry"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)

        # Pretend this site is unknown: synthesize a generic config, then auto-detect.
        cfg = make_generic_config(URL)
        assert cfg.auto_detect is True
        cfg = await _augment_config_from_dom(page, cfg)
        print("Detected config:")
        for a in ("name_field", "email_field", "url_field", "message_field", "captcha_field", "submit_selector"):
            print(f"   {a:16} = {getattr(cfg, a)!r}")

        profile = {"name": "Testy McTest", "email": "testy@example.com",
                   "city": "Berlin", "comment": "Great resource, thanks!"}
        backlinks = ["https://temp.com"]

        # Fill everything EXCEPT the captcha (we don't submit).
        await _fill_form_humanized(page, cfg, profile, "", backlinks, skip_captcha=True)
        await asyncio.sleep(0.5)

        # Read back what actually landed in the form inputs.
        async def val(name):
            if not name:
                return None
            try:
                el = page.locator(f"[name='{name}']").first
                if await el.count() == 0:
                    return "<field not found>"
                return await el.input_value()
            except Exception as e:
                return f"<err {e}>"

        name_v = await val(cfg.name_field)
        email_v = await val(cfg.email_field)
        url_v = await val(cfg.url_field)
        # message may be a WYSIWYG editor — read editor text if the textarea is hidden
        msg_v = await val(cfg.message_field)
        editor_text = await page.evaluate("""() => {
            const ed = document.querySelector('.ck-content, .cke_editable, .mce-content-body, [contenteditable=true], iframe.cke_wysiwyg_frame');
            if (!ed) return null;
            if (ed.tagName === 'IFRAME') { try { return ed.contentDocument.body.innerText; } catch(e){ return null; } }
            return ed.innerText;
        }""")

        print("\nFilled values read back:")
        print(f"   name    = {name_v!r}")
        print(f"   email   = {email_v!r}")
        print(f"   url     = {url_v!r}")
        print(f"   message(textarea) = {msg_v!r}")
        print(f"   message(editor)   = {editor_text!r}")

        ok = True
        if name_v != profile["name"]:
            ok = False; print("   !! name not filled correctly")
        if email_v != profile["email"]:
            ok = False; print("   !! email not filled correctly")
        if url_v != backlinks[0]:
            ok = False; print("   !! homepage/url not filled correctly")
        msg_ok = (backlinks[0] in (msg_v or "")) or (backlinks[0] in (editor_text or ""))
        if not msg_ok:
            ok = False; print("   !! backlink not present in message body")

        await browser.close()
        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
