"""Per-site form field mappings and random profile data for backlink generation."""
from __future__ import annotations

import json
import logging
import random
import re
import tempfile
from dataclasses import dataclass, field, asdict, replace, fields as dc_fields
from pathlib import Path
from typing import Optional

logger = logging.getLogger("backlink_gen")


@dataclass
class SiteFormConfig:
    """Maps form fields for a specific site."""
    domain: str
    url_template: str
    name_field: str
    email_field: str = ""
    url_field: str = ""
    city_field: str = ""
    message_field: str = "message"
    captcha_field: str = "captcha"
    captcha_hash_field: str = ""
    submit_selector: str = ""
    success_keywords: list[str] = field(default_factory=list)
    failure_keywords: list[str] = field(default_factory=list)
    honeypot_fields: list[str] = field(default_factory=list)  # Leave empty
    select_fields: dict[str, list[str]] = field(default_factory=dict)  # name -> options
    skip_fields: list[str] = field(default_factory=list)  # Fields to leave empty
    rating_selector: str = ""  # CSS selector for the star/rating element to click
    rating_value: int = 0  # the rating value to set (e.g., 5 for 5 stars)
    # True for synthesized/user-added sites: the runtime DOM auto-detector fills
    # in any empty field name from the live page. The built-in 5 leave this False
    # so their hand-tuned mappings are never overridden (zero regression).
    auto_detect: bool = False


RANDOM_NAMES = [
    "Alex Schmidt", "Maria Weber", "Jonas Fischer", "Sophie Wagner", "Lukas Becker",
    "Emma Hoffmann", "Felix Bauer", "Anna Koch", "Max Richter", "Laura Klein",
    "David Wolf", "Julia Schroder", "Tim Neumann", "Lisa Schwarz", "Tom Zimmermann",
    "Sara Brandt", "Nico Krause", "Mia Lehmann", "Paul Hartmann", "Eva Lange",
    "Leon Werner", "Nora Herrmann", "Finn Kaiser", "Ida Vogel", "Ben Friedrich",
    "Romy Peters", "Jan Kraemer", "Pia Arnold", "Kai Gunther", "Lea Schuster",
]

RANDOM_CITIES = [
    "Berlin", "Munchen", "Hamburg", "Koln", "Frankfurt",
    "Stuttgart", "Dusseldorf", "Dortmund", "Essen", "Leipzig",
    "Warszawa", "Krakow", "Wroclaw", "Poznan", "Gdansk",
    "Moskva", "SPb", "Kazan", "Novosibirsk", "Ekaterinburg",
]

RANDOM_EMAIL_DOMAINS = ["gmail.com", "web.de", "gmx.de", "outlook.com", "yahoo.com", "mail.ru", "yandex.ru"]

RANDOM_COMMENTS = [
    "Great website! Very informative and well structured. Keep up the excellent work!",
    "Thanks for sharing this valuable content. I learned a lot from your site.",
    "Excellent resource! I've bookmarked this page for future reference.",
    "Really appreciate the effort put into this website. Very helpful information!",
    "This is exactly what I was looking for. Thanks for making it so easy to understand.",
    "Fantastic content! I'll definitely be coming back for more.",
    "Very well written and easy to navigate. One of the best sites on this topic!",
    "Impressive work! The design is clean and the content is top-notch.",
    "I found this site very useful. Thank you for providing such great information.",
    "Wonderful website! The attention to detail is remarkable.",
    "Very professional and informative. Keep the great content coming!",
    "This site is a goldmine of information. Thanks for sharing your knowledge!",
    "Outstanding quality content. I really enjoyed browsing your site.",
    "Super helpful! I was struggling with this topic and your site cleared everything up.",
    "Beautifully designed and full of useful information. Highly recommended!",
]


def get_random_profile() -> dict:
    name = random.choice(RANDOM_NAMES)
    nick = name.lower().replace(" ", "_")
    email_domain = random.choice(RANDOM_EMAIL_DOMAINS)
    return {
        "name": name,
        "email": f"{nick}@{email_domain}",
        "city": random.choice(RANDOM_CITIES),
        "comment": random.choice(RANDOM_COMMENTS),
    }


SITE_CONFIGS: dict[str, SiteFormConfig] = {
    "klubabstynenta.piekary.pl": SiteFormConfig(
        domain="klubabstynenta.piekary.pl",
        url_template="http://www.klubabstynenta.piekary.pl/ksiega-gosci/dodaj",
        name_field="autor",
        email_field="email",
        url_field="www",
        city_field="miasto",
        message_field="tresc",
        captcha_field="captcha-code",
        submit_selector="input[type='submit'], button:has-text('Dodaj')",
        success_keywords=["dodano", "zostal dodany", "został dodany", "czeka na akceptacje",
                          "moderacje", "dziekujemy", "dziękujemy"],
        failure_keywords=["bledny", "nieprawidlowy", "error", "nie zostal", "nie został",
                          "formularz nie", "poprawnie wypełniony", "błędny",
                          "nie został poprawnie"],
    ),
    "ersterzug-hq.com": SiteFormConfig(
        domain="ersterzug-hq.com",
        url_template="http://ersterzug-hq.com/index.php/guestbook/index/newentry",
        name_field="name",
        email_field="email",
        url_field="homepage",
        message_field="text",
        captcha_field="captcha",
        submit_selector="input[type='submit'], input[value='Eintragen'], button:has-text('Senden'), button:has-text('Eintragen'), button:has-text('Submit')",
        # NOTE: "eintrag" removed — it matches the ever-present "Eintragen"
        # button and "Neuer Eintrag" heading, marking any page a false success.
        # These are real Ilch success-banner phrases only.
        success_keywords=["erfolgreich", "wurde hinzugef", "vielen dank für"],
        failure_keywords=["falsch", "fehler", "incorrect", "wrong", "try again"],
        honeypot_fields=["bot"],
        skip_fields=["login_emailname", "login_password"],
    ),
    "geini.de": SiteFormConfig(
        domain="geini.de",
        url_template="https://geini.de/index.php/guestbook/index/newentry",
        name_field="name",
        email_field="email",
        url_field="homepage",
        message_field="text",
        captcha_field="captcha",
        submit_selector="input[type='submit'], input[value='Eintragen'], button:has-text('Senden'), button:has-text('Eintragen')",
        # "eintrag" removed (matches static "Eintragen" chrome — false positives).
        success_keywords=["erfolgreich", "wurde hinzugef", "vielen dank für"],
        failure_keywords=["falsch", "fehler", "incorrect", "wrong"],
        honeypot_fields=["bot"],
        skip_fields=["shoutbox_name", "shoutbox_textarea"],
    ),
    "starwars-freakz.de": SiteFormConfig(
        domain="starwars-freakz.de",
        url_template="https://www.starwars-freakz.de/index.php?commentspage=44&pollID=10&site=polls&sorttype=DESC",
        name_field="name",
        email_field="mail",
        url_field="url",
        message_field="message",
        captcha_field="captcha",
        captcha_hash_field="captcha_hash",
        submit_selector="input[name='savevisitorcomment'], button:has-text('Submit')",
        # Bare "success"/"added"/"published" removed — they match page counters
        # and static chrome. Keep only phrases that appear on a real accepted post.
        success_keywords=["comment added", "gespeichert", "erfolgreich",
                          "hinzugef", "kommentar wurde"],
        failure_keywords=["falsch", "fehler", "ungültig",
                          "falscher", "falsche eingabe",
                          "wrong captcha", "invalid captcha", "incorrect captcha"],
        skip_fields=["ws_user", "pwd"],
        select_fields={
            "fontcolor": ["#000000"],
            "fontsize": ["12"],
            "font": ["Arial"],
            "align": ["left"],
        },
    ),
    "kazan.top100lingua.ru": SiteFormConfig(
        domain="kazan.top100lingua.ru",
        url_template="https://kazan.top100lingua.ru/infinity-school/anglijskij-dlja-vseh-vozrastov",
        name_field="comment-form-new[from]",
        email_field="comment-form-new[from_email]",
        url_field="",
        message_field="comment-form-new[text]",
        captcha_field="comment-form-new[verifyCode]",
        submit_selector="input[type='submit'], button[type='submit'], button:has-text('Submit')",
        success_keywords=["success", "thank", "sent", "spasibo", "otpravleno", "opublikovan", "dobavlen", "otziv"],
        failure_keywords=["wrong", "invalid", "oshibka", "incorrectly", "not filled",
                          "nepravilny", "неправильный проверочный", "следующие ошибки",
                          "должно быть", "исправьте следующие"],
        rating_selector="div.rating-area label, .rating label, span.star, input[name*='rating'], input[name*='score'], .stars label, .stars a, [class*='star'], [data-rating]",
        rating_value=5,
    ),
}


# ============================================================
# User-added site registry (persisted) + generic synthesis
# ============================================================
#
# The built-in SITE_CONFIGS above are immutable defaults. Sites the user adds at
# runtime are persisted to user_sites.json and MERGED over the built-ins by
# get_all_configs(). For any URL that matches no config at all, we synthesize a
# permissive generic config (auto_detect=True) so the fill/submit pipeline always
# runs — the DOM auto-detector then supplies the field names from the live page.

USER_SITES_FILE = Path("user_sites.json")

# Populated from disk by load_user_sites(); domain -> SiteFormConfig.
USER_SITES: dict[str, SiteFormConfig] = {}

_CONFIG_FIELD_NAMES = {f.name for f in dc_fields(SiteFormConfig)}


def normalize_domain(url_or_host: str) -> str:
    """Reduce a URL or host to a bare lowercase host with no scheme/www/path/port."""
    s = (url_or_host or "").strip()
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", s)  # strip scheme
    s = s.split("/")[0].split("?")[0].split("#")[0]      # host only
    s = s.split("@")[-1]                                   # strip userinfo
    s = s.split(":")[0]                                    # strip port
    return s.replace("www.", "").strip().lower()


def make_generic_config(url: str) -> SiteFormConfig:
    """Synthesize a permissive default config for an unknown site.

    All field-name slots are left empty so the runtime DOM auto-detector fills
    them from the live page; captcha_field falls back to 'captcha' only if
    detection finds nothing. success/failure keyword lists are empty on purpose —
    the generic classifier relies on the keyword-free backlink-count-increase and
    structural error-node signals instead of brittle per-site phrases.
    """
    return SiteFormConfig(
        domain=normalize_domain(url) or url,
        url_template=url,
        name_field="",
        email_field="",
        url_field="",
        city_field="",
        message_field="",
        captcha_field="",
        submit_selector="",
        success_keywords=[],
        failure_keywords=[],
        auto_detect=True,
    )


def _config_from_dict(data: dict) -> Optional[SiteFormConfig]:
    """Reconstruct a SiteFormConfig from a persisted dict, ignoring unknown keys."""
    try:
        clean = {k: v for k, v in (data or {}).items() if k in _CONFIG_FIELD_NAMES}
        if not clean.get("domain"):
            return None
        clean.setdefault("url_template", "")
        clean.setdefault("name_field", "")
        clean.setdefault("auto_detect", True)  # user sites always auto-detect blanks
        return SiteFormConfig(**clean)
    except Exception as e:
        logger.warning(f"Skipping invalid user-site record {data!r}: {e}")
        return None


def load_user_sites() -> dict[str, SiteFormConfig]:
    """Load persisted user sites from disk into USER_SITES (idempotent)."""
    USER_SITES.clear()
    try:
        if USER_SITES_FILE.exists():
            raw = json.loads(USER_SITES_FILE.read_text(encoding="utf-8"))
            records = raw if isinstance(raw, list) else raw.get("sites", [])
            for rec in records:
                cfg = _config_from_dict(rec)
                if cfg:
                    USER_SITES[normalize_domain(cfg.domain)] = cfg
    except Exception as e:
        logger.error(f"Failed to load {USER_SITES_FILE}: {e} — falling back to built-ins only")
    return USER_SITES


def save_user_sites() -> None:
    """Persist USER_SITES atomically (temp file + rename) to avoid corruption."""
    try:
        records = [asdict(cfg) for cfg in USER_SITES.values()]
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(USER_SITES_FILE.parent or "."),
            prefix=".user_sites_", suffix=".tmp", delete=False,
        )
        try:
            json.dump(records, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
        finally:
            tmp.close()
        Path(tmp.name).replace(USER_SITES_FILE)
    except Exception as e:
        logger.error(f"Failed to save {USER_SITES_FILE}: {e}")


def add_user_site(config: SiteFormConfig) -> SiteFormConfig:
    """Register (or overwrite) a user site and persist it."""
    config = replace(config, domain=normalize_domain(config.domain), auto_detect=True)
    USER_SITES[config.domain] = config
    save_user_sites()
    return config


def remove_user_site(domain: str) -> bool:
    """Remove a user site by domain. Returns True if something was removed.

    Only user-added sites can be removed; the built-in 5 are immutable.
    """
    key = normalize_domain(domain)
    if key in USER_SITES:
        del USER_SITES[key]
        save_user_sites()
        return True
    return False


def get_all_configs() -> dict[str, SiteFormConfig]:
    """Merged view: built-in defaults overlaid with user-added/overridden sites."""
    merged: dict[str, SiteFormConfig] = {normalize_domain(d): c for d, c in SITE_CONFIGS.items()}
    merged.update(USER_SITES)
    return merged


def get_config_for_url(url: str) -> Optional[SiteFormConfig]:
    """Return the built-in or user config whose domain matches this URL, else None."""
    host = normalize_domain(url)
    merged = get_all_configs()
    if host in merged:
        return merged[host]
    # Fall back to substring match (handles subdomains / path-embedded domains).
    for domain, config in merged.items():
        if domain and domain in url:
            return config
    return None


def get_config_or_generic(url: str) -> SiteFormConfig:
    """Like get_config_for_url but never None — synthesizes a generic auto-detect
    config for unknown sites so the pipeline always runs."""
    return get_config_for_url(url) or make_generic_config(url)
