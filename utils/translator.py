"""
AdSentry AI Translator Utility
Hybrid translation using Google Gemini Flash (preferred) or deep-translator (fallback).
Supports Arabic <-> English bilingual translation for ad content.

NOTE: Uses the new `google-genai` SDK (google.genai), NOT the deprecated
`google-generativeai` (google.generativeai) package.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

logger = logging.getLogger("AdSentry.Translator")

# ---------------------------------------------------------------------------
# Language Detection Helper
# ---------------------------------------------------------------------------

def detect_language(text: str) -> Literal["ar", "en"]:
    """Detects whether text is primarily Arabic or English."""
    arabic_chars = sum(1 for c in text if "\u0600" <= c <= "\u06FF")
    total_alpha = sum(1 for c in text if c.isalpha())
    if total_alpha == 0:
        return "en"
    return "ar" if arabic_chars / total_alpha > 0.4 else "en"


# ---------------------------------------------------------------------------
# Gemini Flash Translation (Primary)
# ---------------------------------------------------------------------------

async def _translate_with_gemini(text: str, target_lang: Literal["ar", "en"]) -> str | None:
    """Translates text using Google Gemini 2.0 Flash (new google-genai SDK v2.31+)."""
    try:
        from google import genai  # type: ignore  # pip: google-genai
        from config import Config

        if not Config.GEMINI_API_KEY:
            logger.debug("GEMINI_API_KEY not set – skipping Gemini translation.")
            return None

        client = genai.Client(api_key=Config.GEMINI_API_KEY)

        lang_name = "Arabic" if target_lang == "ar" else "English"
        prompt = (
            f"Translate the following Discord advertisement text to {lang_name}. "
            "Preserve all emojis, markdown formatting (**bold**, links, bullet points), "
            "and Discord-specific terms (e.g. Server, Bot, DM, Nitro) without translating them. "
            "Return ONLY the translated text, nothing else.\n\n"
            f"Text:\n{text}"
        )

        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=prompt,
                ),
            ),
            timeout=12.0,
        )

        translated: str | None = None
        if response and response.text:
            translated = response.text.strip() or None

        if translated:
            logger.info(f"Gemini translation successful (gemini-2.0-flash) -> {target_lang}")
        return translated

    except ImportError:
        logger.warning("google-genai not installed. Falling back to deep-translator.")
        return None
    except Exception as e:
        logger.warning(f"Gemini translation failed: {e}. Falling back to deep-translator.")
        return None


# ---------------------------------------------------------------------------
# deep-translator Fallback (Free, No API Key)
# ---------------------------------------------------------------------------

async def _translate_with_deep(text: str, target_lang: Literal["ar", "en"]) -> str | None:
    """Translates text using deep-translator (Google Translate with MyMemory fallback)."""
    loop = asyncio.get_event_loop()

    # 1. Try GoogleTranslator first
    try:
        from deep_translator import GoogleTranslator  # type: ignore

        lang_map = {"ar": "arabic", "en": "english"}
        target = lang_map[target_lang]

        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: GoogleTranslator(source="auto", target=target).translate(text),
            ),
            timeout=8.0,
        )
        if result:
            logger.info(f"deep-translator (Google) translation successful -> {target_lang}")
            return result
    except Exception as e:
        logger.warning(f"deep-translator GoogleTranslator failed ({e}). Trying MyMemory fallback...")

    # 2. Try MyMemoryTranslator as a robust fallback
    try:
        from deep_translator import MyMemoryTranslator  # type: ignore

        target_code = "ar-SA" if target_lang == "ar" else "en-US"
        source_code = "en-US" if target_lang == "ar" else "ar-SA"

        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: MyMemoryTranslator(source=source_code, target=target_code).translate(text),
            ),
            timeout=8.0,
        )
        if result:
            logger.info(f"deep-translator (MyMemory) translation successful -> {target_lang}")
            return result
    except Exception as e:
        logger.warning(f"deep-translator MyMemoryTranslator failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Main Public Translation Function
# ---------------------------------------------------------------------------

async def translate_ad(text: str, target_lang: Literal["ar", "en"]) -> str | None:
    """
    Translates ad text to the target language using the hybrid strategy:
    1. Try Gemini Flash (if GEMINI_API_KEY is set)
    2. Fall back to deep-translator (Google Translate, free)
    Returns translated text or None if all methods fail.
    """
    # Truncate very long text to stay within model token limits
    MAX_CHARS = 3000
    truncated = text[:MAX_CHARS] + ("..." if len(text) > MAX_CHARS else "")

    # Try Gemini first
    result = await _translate_with_gemini(truncated, target_lang)
    if result:
        return result

    # Fall back to deep-translator
    result = await _translate_with_deep(truncated, target_lang)
    return result
