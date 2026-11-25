"""Shared helpers for the Laqta autopilot. Standard library only (runs on the Mac or a Linux runner)."""
import json, hashlib, re, os, subprocess, shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

AP = Path(__file__).resolve().parents[1]          # social/autopilot
Q = AP / "queue"
LOG = AP / "log"
CFG = json.loads((AP / "config.json").read_text())
# Public runner (laqta-publisher): the private rule lists (banned words, venue names, ...) are not in its config.json.
# They arrive as the LAQTA_RULES secret (JSON). Refuse to run without them, so gates never pass on empty lists.
PRIVATE_RULE_KEYS = ("banned_words", "banned_claims", "venues_need_written_confirmation",
                     "banned_patterns", "sale_banned_patterns", "sale_must_mention")
if CFG.get("rules_from_env"):
    _r = os.environ.get("LAQTA_RULES")
    if not _r:
        raise SystemExit("LAQTA_RULES secret missing: refusing to run gates without the private rule lists")
    CFG.update(json.loads(_r))
BH =timezone(timedelta(hours=3))                 # Bahrain, no DST
TYPES = ("reel", "carousel", "story")


def control():
    """CONTROL.md → dict of `key: value` lines (comments after # dropped)."""
    out = {}
    for l in (AP / "CONTROL.md").read_text().splitlines():
        m = re.match(r"^([a-z_0-9]+):\s*(.*)$", l)
        if m:
            v = m.group(2).split("#")[0].strip().strip('"')
            out[m.group(1)] = {"true": True, "false": False}.get(v.lower(), v)
    out.setdefault("mode", "dry_run")
    return out


def sha(text):
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def load_post(slot):
    return json.loads((Q / slot / "post.json").read_text())


def slots():
    return sorted(p.name for p in Q.iterdir() if (p / "post.json").exists()) if Q.exists() else []


def due_utc(post):
    """scheduled_at (ISO with +03:00) → aware UTC datetime."""
    return datetime.fromisoformat(post["scheduled_at"]).astimezone(timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def keychain(service, account):
    if not shutil.which("security"):
        return ""
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"], capture_output=True, text=True)
    return r.stdout.strip()


def buffer_key():
    if os.environ.get("BUFFER_API_KEY"):
        return os.environ["BUFFER_API_KEY"]
    k = CFG["buffer_key_keychain"]
    return keychain(k["service"], k["account"])


def compose_caption(post):
    """What Instagram receives: English first, Arabic second, then at most 5 hashtags (STRATEGY.md §Captions)."""
    tags = " ".join(post.get("hashtags", [])[: CFG["max_hashtags"]])
    parts = [post.get("caption_en", "").strip(), post.get("caption_ar", "").strip(), tags]
    return "\n\n".join(p for p in parts if p)


def youtube_captions(post):
    """(en, ar) for YouTube: the Instagram call to action ("DM us ..." / «راسلنا/راسلونا ...») becomes the YouTube one
    (config.json youtube_cta). Posts without a DM line stay as they are."""
    cta = CFG["youtube_cta"]; en, ar = post.get("caption_en", ""), post.get("caption_ar", "")
    en2, n1 = re.subn(r"DM us(?: for (?:details|the price))?\.?", cta["en"] + ".", en)
    ar2, n2 = re.subn(r"راسل(?:نا|ونا)(?: للتفاصيل| لمعرفة السعر| للسعر)?", cta["ar"], ar)
    return en2, ar2


def linkedin_text(post):
    """What the LinkedIn page receives: English first, Arabic second (both with the WhatsApp / website call to action, as on
    YouTube: LinkedIn has no 'DM us' habit for a company page), then at most 3 hashtags, Latin-script ones first."""
    en, ar = youtube_captions(post)
    tags = post.get("hashtags", [])
    tags = sorted(tags, key=lambda h: not re.match(r"#[A-Za-z]", h))[: CFG.get("linkedin_max_hashtags", 3)]
    return "\n\n".join(x for x in [en.strip(), ar.strip(), " ".join(tags)] if x)


LI_PDF = "media/linkedin.pdf"      # carousels: the slides as one PDF (LinkedIn document post), built by tools/li_document.py
