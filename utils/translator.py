"""
AdSentry AI Translator Utility
Hybrid translation using Google Gemini Flash (preferred) or deep-translator (fallback).
Supports Arabic <-> English bilingual translation for ad content.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    """Translates text using Google Gemini Flash API (context-aware, ideal for Discord ads)."""
    try:
        import google.generativeai as genai  # type: ignore
        from config import Config

        if not Config.GEMINI_API_KEY:
            return None

        genai.configure(api_key=Config.GEMINI_API_KEY)

        lang_name = "Arabic" if target_lang == "ar" else "English"
        prompt = (
            f"Translate the following Discord advertisement text to {lang_name}. "
            f"Preserve all emojis, markdown formatting (**bold**, links, bullet points), "
            f"and Discord-specific terms (e.g. Server, Bot, DM, Nitro) without translating them. "
            f"Return ONLY the translated text, nothing else.\n\n"
            f"Text:\n{text}"
        )

        # Try gemini-3.8-flash first, then gemini-flash-latest
        model_name = "gemini-3.8-flash"
        model = genai.GenerativeModel(model_name)

        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: model.generate_content(prompt)),
            timeout=3.5,
        )

        translated = response.text.strip() if response.text else None
        if translated:
            logger.info(f"Gemini translation successful ({model_name}) -> {target_lang}")
        return translated

    except ImportError:
        logger.warning("google-generativeai not installed. Falling back to deep-translator.")
        return None
    except Exception as e:
        logger.warning(f"Gemini translation failed: {e}. Falling back.")
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
                lambda: GoogleTranslator(source="auto", target=target).translate(text)
            ),
            timeout=3.5,
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
                lambda: MyMemoryTranslator(source=source_code, target=target_code).translate(text)
            ),
            timeout=4.0,
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
