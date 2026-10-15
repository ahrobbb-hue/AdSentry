"""
AdSentry Auto-Moderation & Content Guard
Comprehensive bilingual filter for sexual content, NSFW, hate speech, racism, slurs,
scam links, and unauthorized advertising.
"""

from __future__ import annotations

import re
from typing import List, Tuple
from config import Config

# -------------------------------------------------------------
# 1. Content Dictionaries & Pattern Definitions
# -------------------------------------------------------------

# English Explicit / Sexual / NSFW Terms
EXPLICIT_EN: set[str] = {
    "sex", "sexual", "porn", "porno", "pornography", "xxx", "nsfw",
    "nude", "nudes", "nudity", "dick", "dicks", "cock", "cocks",
    "pussy", "pussies", "boob", "boobs", "tits", "titties", "vagina",
    "penis", "blowjob", "handjob", "anal", "dildo", "dildos",
    "masturbat", "masturbation", "horny", "onlyfans", "cum", "cumming",
    "semen", "sperm", "whore", "slut", "milf", "hentai", "ecchi",
    "rule34", "brazzers", "pornhub", "xvideos", "xnxx", "stripchat",
    "chaturbate", "erotic", "orgasm", "gangbang", "bdsm", "threesome",
}

# Arabic Explicit / Sexual / NSFW Terms
EXPLICIT_AR: set[str] = {
    "سكس", "جنس", "جنسي", "اباحي", "اباحيه", "بورن", "شيميل",
    "سحاق", "سحاقيات", "لواط", "لوطي", "منيوك", "منيوكه", "نيك",
    "طيز", "زب", "كس", "قحبه", "قحبة", "شرموطه", "شرموطة", "شرمطه",
    "عرص", "منيكه", "منيكة", "ديوث", "مخنث", "تفريغ", "دعاره",
    "دعارة", "مومس", "عاهره", "عاهرة", "قواد", "قواده", "استمناء",
    "شهوه", "شهوة", "صدر", "مؤخره", "مؤخرة", "بزاز", "اثداء",
    "لحس", "مص", "سكسي", "ممحون", "ممحونه", "ممحونة", "محنه", "محنة",
}

# Short Arabic terms that require strict word boundary matching to avoid false positives
SHORT_AR_BOUNDED: set[str] = {
    "كس", "زب", "طيز", "نيك", "عرص", "مص", "صدر", "لحس", "جنس", "سكس",
}

# Severe Hate Speech, Slurs, Racism, and Insults (English)
HATE_EN: set[str] = {
    "nigger", "nigga", "faggot", "fag", "retard", "kike", "chink",
    "spic", "wetback", "tranny", "kill yourself", "kys", "hitler",
    "nazi", "terrorist",
}

# Severe Insults, Cursing, and Blasphemy (Arabic)
HATE_AR: set[str] = {
    "كسمك", "كسختك", "كسامك", "يلعن ربك", "يلعن دينك", "ابن القحبه",
    "ابن القحبة", "ابن الشرموطه", "ابن الشرموطة", "ابن العاهره", "ابن العاهرة",
    "ابن الكلب", "كسم", "طز فيك", "منيوك", "عرص", "ديوث", "شرموط",
}

# Phishing, Scams, and Malicious Link Patterns
SCAM_PATTERNS: list[str] = [
    r"discord\.gift/",
    r"dlscord\.",
    r"discoord\.",
    r"discord-nitro\.",
    r"free\s+nitro",
    r"steamcommunity-",
    r"grabify\.",
    r"iplogger\.",
    r"2no\.co",
    r"bit\.ly",
    r"tinyurl\.com",
    r"goo\.gl",
]

# Mass Pings
UNAUTHORIZED_PINGS: set[str] = {
    "@everyone",
    "@here",
}

# Common asterisk obfuscations
WILDCARD_OBFUSCATIONS: list[tuple[str, str]] = [
    (r"\bs[\*@_]x\b", "sex"),
    (r"\bd[\*!_]ck\b", "dick"),
    (r"\bp[\*!_]ssy\b", "pussy"),
    (r"\bc[\*@_]ck\b", "cock"),
    (r"\bf[\*@_]ck\b", "fuck"),
    (r"\bn[\*!_]gg[a|er]\b", "slur"),
]

# Leetspeak translation table
LEET_TABLE = str.maketrans({
    "@": "a",
    "4": "a",
    "8": "b",
    "3": "e",
    "1": "i",
    "!": "i",
    "0": "o",
    "5": "s",
    "$": "s",
    "7": "t",
    "+": "t",
})

# Arabic Tashkeel (diacritics) regex
ARABIC_DIACRITICS_REGEX = re.compile(r"[\u064B-\u065F\u0670\u0640]")


def normalize_arabic(text: str) -> str:
    """Normalizes Arabic text by removing diacritics, tatweel, and standardizing letters."""
    # Remove diacritics and tatweel
    text = ARABIC_DIACRITICS_REGEX.sub("", text)
    # Standardize Alef
    text = re.sub(r"[إأآٱ]", "ا", text)
    # Standardize Yaa
    text = re.sub(r"[ىي]", "ي", text)
    # Standardize Taa Marbuta
    text = re.sub(r"ة", "ه", text)
    return text


def normalize_english(text: str) -> str:
    """Translates leetspeak and collapses repeating characters."""
    text = text.lower().translate(LEET_TABLE)
    # Collapse 3+ repeated characters down to 1 (e.g. seeeex -> sex)
    text = re.sub(r"(.)\1{2,}", r"\1", text)
    return text


def detect_obfuscated_en(term: str, text: str) -> bool:
    """Detects spaced or punctuation-separated characters (e.g. s.e.x, s e x, s-e-x)."""
    chars = [re.escape(c) for c in term]
    pattern = rf"(?:^|[\s\W_])" + r"[\s\W_]*".join(chars) + rf"(?:$|[\s\W_])"
    return bool(re.search(pattern, text, re.IGNORECASE))


def check_arabic_word_boundary(term: str, text: str) -> bool:
    """
    Checks for an Arabic term with allowed Arabic prefixes (و, ف, ب, ك, ل, ال)
    and suffixes (ك, ه, ها, هم, كم, نا, ي, ين, ات) bounded by whitespace or punctuation.
    Avoids false positives like 'بالعكس' for 'كس' or 'زبون' for 'زب'.
    """
    pattern = (
        rf"(?:^|[\s\W_])"
        rf"(?:و|ف|ب|ك|ل|ال|وال|فال|بال|يا\s*)?"
        rf"{re.escape(term)}"
        rf"(?:ك|ه|ها|هم|كم|نا|ي|ين|ات|ه)?(?:$|[\s\W_])"
    )
    return bool(re.search(pattern, text))


# Discord Invite Links Regex
DISCORD_INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discord(?:app)?\.com/invite)/[a-zA-Z0-9\-]+",
    re.IGNORECASE,
)


def screen_content(
    title: str,
    content: str,
    allow_discord_invites: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Screens ad title and content against sexual/NSFW, hate speech, slurs,
    scam links, mass mentions, and configured forbidden patterns.
    Optionally permits Discord server invites if authored by server Admins/Owners.

    Returns:
        (is_flagged: bool, flagged_reasons: List[str])
    """
    raw_combined = f"{title}\n{content}"
    flagged: list[str] = []

    # 1. Check unauthorized mass pings
    for ping in UNAUTHORIZED_PINGS:
        if ping in raw_combined:
            flagged.append(ping)

    # 2. Check Scam & Phishing regex patterns
    for pattern in SCAM_PATTERNS:
        if re.search(pattern, raw_combined, re.IGNORECASE):
            flagged.append("Scam/Phishing link pattern")

    # 3. Check Discord Server Invites (Restricted to Admins/Owners only)
    if not allow_discord_invites:
        if (
            DISCORD_INVITE_REGEX.search(raw_combined)
            or "discord.gg/" in raw_combined.lower()
            or "invite.gg/" in raw_combined.lower()
            or "discord.com/invite" in raw_combined.lower()
        ):
            flagged.append("Discord server invite link (Admin/Owner exclusive)")

    # 4. Check Config-level forbidden words (.env)
    for word in Config.FORBIDDEN_WORDS:
        if not word:
            continue
        # Skip discord invite patterns if caller is authorized Admin/Owner
        if allow_discord_invites and any(inv in word.lower() for inv in ["discord.gg", "invite.gg"]):
            continue
        if word.lower() in raw_combined.lower():
            flagged.append(word)

    # 4. Standardize texts for Arabic and English checks
    norm_ar = normalize_arabic(raw_combined.lower())
    norm_en = normalize_english(raw_combined.lower())

    # 5. Check Wildcard Obfuscations (e.g. s*x, d*ck)
    for pattern, label in WILDCARD_OBFUSCATIONS:
        if re.search(pattern, raw_combined, re.IGNORECASE) or re.search(pattern, norm_en):
            flagged.append(f"Explicit pattern: {label}")

    # 6. English Explicit & NSFW Screening
    for term in EXPLICIT_EN:
        pattern = rf"\b{re.escape(term)}\b"
        if re.search(pattern, norm_en):
            flagged.append(f"Explicit term: {term}")
        elif len(term) >= 3 and detect_obfuscated_en(term, raw_combined):
            flagged.append(f"Explicit term (obfuscated): {term}")

    # 7. English Hate Speech & Slurs Screening
    for term in HATE_EN:
        pattern = rf"\b{re.escape(term)}\b"
        if re.search(pattern, norm_en) or detect_obfuscated_en(term, raw_combined):
            flagged.append(f"Hate speech / Slur: {term}")

    # 8. Arabic Explicit & NSFW Screening
    for term in EXPLICIT_AR:
        term_norm = normalize_arabic(term)
        if term_norm in SHORT_AR_BOUNDED or len(term_norm) <= 3:
            if check_arabic_word_boundary(term_norm, norm_ar):
                flagged.append(f"محتوى جنسي/غير لائق: {term}")
        else:
            # Longer compound terms can check boundary or substring
            pattern = rf"(^|[\s\W_]){re.escape(term_norm)}"
            if re.search(pattern, norm_ar):
                flagged.append(f"محتوى جنسي/غير لائق: {term}")

    # 9. Arabic Hate Speech & Insults Screening
    for term in HATE_AR:
        term_norm = normalize_arabic(term)
        if len(term_norm) <= 3:
            if check_arabic_word_boundary(term_norm, norm_ar):
                flagged.append(f"ألفاظ مسيئة/عنصرية: {term}")
        else:
            if term_norm in norm_ar:
                flagged.append(f"ألفاظ مسيئة/عنصرية: {term}")

    # Deduplicate while preserving order
    unique_flagged = list(dict.fromkeys(flagged))
    return (len(unique_flagged) > 0, unique_flagged)
