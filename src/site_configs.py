"""Per-site form field mappings and random profile data for backlink generation."""
from __future__ import annotations

import json
import logging
import random
import re
import tempfile
import time
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
DISABLED_SITES_FILE = Path("disabled_sites.json")

# Populated from disk by load_user_sites(); domain -> SiteFormConfig.
USER_SITES: dict[str, SiteFormConfig] = {}
# Domains permanently removed from the active list (e.g. a built-in whose site went
# down). The built-in code stays but the domain is hidden from get_all_configs / UI.
DISABLED_SITES: set[str] = set()

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
    """Remove a user-added site by domain. Returns True if something was removed."""
    key = normalize_domain(domain)
    if key in USER_SITES:
        del USER_SITES[key]
        save_user_sites()
        return True
    return False


def _builtin_domains() -> set[str]:
    return {normalize_domain(d) for d in SITE_CONFIGS}


def load_disabled_sites() -> set[str]:
    """Load the set of permanently-removed domains from disk."""
    DISABLED_SITES.clear()
    try:
        if DISABLED_SITES_FILE.exists():
            for d in (json.loads(DISABLED_SITES_FILE.read_text(encoding="utf-8")) or []):
                DISABLED_SITES.add(normalize_domain(d))
    except Exception as e:
        logger.error(f"Failed to load {DISABLED_SITES_FILE}: {e}")
    return DISABLED_SITES


def save_disabled_sites() -> None:
    try:
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(DISABLED_SITES_FILE.parent or "."),
            prefix=".disabled_", suffix=".tmp", delete=False,
        )
        try:
            json.dump(sorted(DISABLED_SITES), tmp, ensure_ascii=False, indent=2)
            tmp.flush()
        finally:
            tmp.close()
        Path(tmp.name).replace(DISABLED_SITES_FILE)
    except Exception as e:
        logger.error(f"Failed to save {DISABLED_SITES_FILE}: {e}")


def delete_site(domain: str) -> bool:
    """Permanently remove a site from the active list. A user-added site is deleted
    from the registry outright; a built-in is recorded as DISABLED (its hand-tuned
    code stays, but the domain is hidden everywhere). Returns True if it removed one."""
    key = normalize_domain(domain)
    removed = False
    if key in USER_SITES:
        del USER_SITES[key]
        save_user_sites()
        removed = True
    if key in _builtin_domains() and key not in DISABLED_SITES:
        DISABLED_SITES.add(key)
        save_disabled_sites()
        removed = True
    return removed


def restore_site(domain: str) -> bool:
    """Re-enable a previously-disabled built-in. Returns True if one was restored."""
    key = normalize_domain(domain)
    if key in DISABLED_SITES:
        DISABLED_SITES.discard(key)
        save_disabled_sites()
        return True
    return False


def disabled_builtin_domains() -> list[str]:
    """Built-in domains the user has removed (candidates for restore)."""
    return sorted(DISABLED_SITES & _builtin_domains())


def get_all_configs() -> dict[str, SiteFormConfig]:
    """Merged view: built-in defaults overlaid with user-added/overridden sites,
    MINUS any domains the user has permanently removed."""
    merged: dict[str, SiteFormConfig] = {normalize_domain(d): c for d, c in SITE_CONFIGS.items()}
    merged.update(USER_SITES)
    for d in DISABLED_SITES:
        merged.pop(d, None)
    return merged


# ============================================================
# Learning layer — site_memory.json (a DISPOSABLE learned cache)
# ============================================================
#
# After a CONFIRMED post (the client backlink actually appeared on the page), we
# remember what worked for that site: the resolved field mapping and which captcha
# engine solved it. Next run, learned field names fill any slot the live detector
# missed (detector always wins — stale memory can never shadow a correct detection).
# This is a rebuildable cache: deleting the file is a zero-impact full reset, and a
# corrupt file degrades gracefully to today's per-run auto-detect.
#
# SAFETY: the SOLE writer (record_site_learning) no-ops unless the site is
# auto_detect AND not a built-in — so the hand-tuned 5 can never receive a learned
# byte. Writes only fire on the definitive backlink-count-increase signal.

SITE_MEMORY_FILE = Path("site_memory.json")
SITE_MEMORY: dict[str, dict] = {}          # normalized-domain -> learned entry
MAX_MEMORY_SITES = 200
MEMORY_FAILURE_THRESHOLD = 3               # consecutive definite rejections -> stop applying
_MEMORY_LEARNABLE_FIELDS = [
    "name_field", "email_field", "url_field", "city_field",
    "message_field", "captcha_field", "captcha_hash_field", "submit_selector",
]


def load_site_memory() -> dict:
    """Load the learned cache. ANY error -> start empty (it's rebuildable)."""
    global SITE_MEMORY
    SITE_MEMORY = {}
    try:
        if SITE_MEMORY_FILE.exists():
            raw = json.loads(SITE_MEMORY_FILE.read_text(encoding="utf-8")) or {}
            sites = raw.get("sites", {}) if isinstance(raw, dict) else {}
            if isinstance(sites, dict):
                SITE_MEMORY = {normalize_domain(k): v for k, v in sites.items() if isinstance(v, dict)}
    except Exception as e:
        logger.warning(f"site_memory load failed ({e}) — starting with an empty cache")
        SITE_MEMORY = {}
    return SITE_MEMORY


def save_site_memory() -> None:
    """Persist atomically; LRU-trim to MAX_MEMORY_SITES by last_confirmed_at."""
    try:
        if len(SITE_MEMORY) > MAX_MEMORY_SITES:
            keep = sorted(SITE_MEMORY.items(),
                          key=lambda kv: kv[1].get("last_confirmed_at", 0), reverse=True)[:MAX_MEMORY_SITES]
            SITE_MEMORY.clear()
            SITE_MEMORY.update(dict(keep))
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(SITE_MEMORY_FILE.parent or "."),
            prefix=".sitemem_", suffix=".tmp", delete=False,
        )
        try:
            json.dump({"version": 1, "sites": SITE_MEMORY}, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
        finally:
            tmp.close()
        Path(tmp.name).replace(SITE_MEMORY_FILE)
    except Exception as e:
        logger.error(f"site_memory save failed: {e}")


def _field_sig(config: SiteFormConfig) -> str:
    return "|".join(str(getattr(config, f, "") or "") for f in _MEMORY_LEARNABLE_FIELDS)


def record_site_learning(config: SiteFormConfig, engine: str = "", confidence: float = 0.0,
                         had_hash: bool = False) -> None:
    """SOLE writer into SITE_MEMORY. NO-OP unless this is a CONFIRMED post on an
    auto_detect (non-built-in) site. Learns the field mapping + captcha engine."""
    if not getattr(config, "auto_detect", False):
        return
    key = normalize_domain(config.domain)
    if not key or key in _builtin_domains():
        return
    try:
        sig = _field_sig(config)
        now = time.time()
        entry = SITE_MEMORY.get(key)
        fields = {f: (getattr(config, f, "") or "") for f in _MEMORY_LEARNABLE_FIELDS}
        if entry is None or entry.get("field_sig") != sig:
            # New site, or the form changed (drift): replace mapping, reset confirms.
            entry = {
                "domain": key, "fields": fields, "field_sig": sig,
                "captcha": {"best_engine": "", "engines": {}, "had_hash": had_hash, "last_confidence": confidence},
                "confirms": 0, "recent_failures": 0,
                "created_at": (entry or {}).get("created_at", now),
            }
        entry["confirms"] = entry.get("confirms", 0) + 1
        entry["recent_failures"] = 0
        entry["updated_at"] = now
        entry["last_confirmed_at"] = now
        if engine:
            engines = entry.setdefault("captcha", {}).setdefault("engines", {})
            engines.setdefault(engine, {"confirmed": 0})["confirmed"] += 1
            entry["captcha"]["had_hash"] = had_hash
            entry["captcha"]["last_confidence"] = round(float(confidence or 0.0), 3)
            entry["captcha"]["best_engine"] = max(engines.items(), key=lambda kv: kv[1].get("confirmed", 0))[0]
        SITE_MEMORY[key] = entry
        save_site_memory()
        logger.info(f"Learned from confirmed post on {key}: confirms={entry['confirms']} engine={engine!r}")
    except Exception as e:
        logger.warning(f"record_site_learning failed for {key}: {e}")


def record_site_failure(config: SiteFormConfig) -> None:
    """On a DEFINITE rejection, bump recent_failures so a stale mapping self-evicts."""
    if not getattr(config, "auto_detect", False):
        return
    key = normalize_domain(config.domain)
    entry = SITE_MEMORY.get(key)
    if entry:
        entry["recent_failures"] = entry.get("recent_failures", 0) + 1
        save_site_memory()


def apply_site_memory(config: SiteFormConfig) -> SiteFormConfig:
    """Fill EMPTY config slots from learned memory (a detector-MISS fallback). Never
    overwrites a non-empty (user-entered or live-detected) value; skips evicted
    entries. Returns config unchanged for built-ins (auto_detect=False)."""
    if not getattr(config, "auto_detect", False):
        return config
    entry = SITE_MEMORY.get(normalize_domain(config.domain))
    if not entry or entry.get("recent_failures", 0) >= MEMORY_FAILURE_THRESHOLD:
        return config
    fields = entry.get("fields", {})
    updates = {f: fields[f] for f in _MEMORY_LEARNABLE_FIELDS
               if fields.get(f) and not (getattr(config, f, "") or "")}
    if updates:
        config = replace(config, **updates)
        logger.info(f"Applied learned mapping for {config.domain}: {list(updates)}")
    return config


def clear_site_memory(domain: Optional[str] = None) -> None:
    """Wipe one site's learned memory, or all of it (domain=None)."""
    global SITE_MEMORY
    if domain is None:
        SITE_MEMORY = {}
    else:
        SITE_MEMORY.pop(normalize_domain(domain), None)
    save_site_memory()


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
