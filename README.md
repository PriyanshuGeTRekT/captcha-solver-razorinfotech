---
title: RazorInfotech Backlink Generator
emoji: 🔗
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# RazorInfotech Backlink Generator

Automated SEO backlink posting to guestbook / comment forms, with **local** CAPTCHA
solving (EasyOCR + ddddocr + Yii2-hash validation — no external captcha APIs).

- Post to the 5 built-in sites **or add any site by URL** — form fields, captcha, and
  success are auto-detected generically.
- FastAPI backend + embedded UI. Runs headless Chromium (Playwright) for form filling.

Runs comfortably on Hugging Face Spaces free CPU (16 GB RAM). The same Docker image
also deploys to Render (which injects its own `$PORT`, overriding the default below).

Open the Space URL and use the UI. No configuration or API keys required.
