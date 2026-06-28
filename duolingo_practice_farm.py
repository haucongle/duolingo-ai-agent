"""
Duolingo Practice Mode XP Farmer (English for Vietnamese speakers)
Continuously runs practice sessions at https://www.duolingo.com/practice
to farm XP without spending hearts.
"""

import base64
import json
import os
import random
import re
import time
import traceback
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from openai import OpenAI

HAS_AUDIO = False

load_dotenv()

EMAIL = os.getenv("VI_DUO_EMAIL")
PASSWORD = os.getenv("VI_DUO_PASSWORD")
DUO_JWT = os.getenv("VI_DUO_JWT") or os.getenv("DUO_JWT")

SESSION_FILE = "vi_duo_session.json"
MAX_SESSIONS = int(os.getenv("MAX_LESSONS", "0"))
PRACTICE_URL = "https://www.duolingo.com/practice"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5.4-mini")

answer_cache = {}
cache_fail_count = {}

GENERIC_QUESTION_EXACT = {
    'nghe và điền', 'nhập từ còn thiếu', 'đọc câu này',
    'chọn cặp từ', 'nghe và tìm từ còn thiếu',
    'tap what you hear', 'type the missing word',
    'hoàn thành câu', 'complete the sentence',
    'chọn nghĩa đúng', 'choose the correct meaning',
    'điền vào chỗ trống', 'fill in the blank',
    'chọn bản dịch đúng', 'choose the correct translation',
    'chọn đáp án đúng', 'choose the correct answer',
    'viết lại bằng tiếng anh', 'rewrite in english',
    'viết lại bằng tiếng việt', 'rewrite in vietnamese',
    'write what you hear', 'nghe và viết lại',
    'dịch câu này', 'translate this sentence',
    'complete the sentence with the missing word',
    'complete the sentence with the correct word',
}

GENERIC_QUESTION_SUBSTRINGS = [
    'hoàn thành câu', 'complete the sentence',
    'chọn nghĩa đúng', 'choose the correct meaning',
    'điền vào chỗ trống', 'fill in the blank',
    'type the missing word', 'nhập từ còn thiếu',
    'write what you hear', 'tap what you hear',
    'viết lại bằng tiếng', 'rewrite in ',
    'translate the sentence', 'dịch câu',
    'translate \'', 'translate "',
]

MAX_CACHE_FAILURES = 2


def _is_generic_question(text):
    """Return True if the question text is a generic title that should not be cached."""
    t = text.lower().strip()
    if t in GENERIC_QUESTION_EXACT:
        return True
    for sub in GENERIC_QUESTION_SUBSTRINGS:
        if sub in t:
            return True
    return False


def normalize_cache_key(text):
    if not text:
        return ""
    s = re.sub(r'_+', '___', text)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ---------------------------------------------------------------------------
# Vision prompt (same exercise types as the main script)
# ---------------------------------------------------------------------------

PROMPT = """You are an expert AI agent that solves Duolingo exercises automatically with perfect accuracy.
This is an English course for Vietnamese speakers. The UI is in Vietnamese.
Translations go Vietnamese → English or English → Vietnamese depending on the exercise.

Analyze this Duolingo screenshot carefully and determine:
1. The type of exercise (be precise — see type definitions below)
2. The COMPLETE question text (extract every word visible on screen)
3. The correct answer (grammatically perfect, with proper punctuation)
4. The exact action(s) needed to answer

CRITICAL: For "question" field, you MUST extract the COMPLETE visible text of the question/sentence being asked.
Do NOT summarize or paraphrase — copy the exact text shown on screen character by character.
For word_bank exercises, this is the Vietnamese sentence being translated.
For typing exercises, this is the sentence to translate.

Respond ONLY with valid JSON (no markdown, no explanation) using this format:

{
  "type": "image_choice | multiple_choice | checkbox | word_bank | typing | matching | audio_matching | audio_fill_blank | listen_and_type | speaking | listening | tap_pairs | no_question",
  "question": "brief description of the question",
  "answer": "the correct answer",
  "all_options": ["option1", "option2", "option3"],
  "total_options": 3,
  "sentence": "(for listen_and_type/audio_fill_blank) the sentence with ___ for the blank",
  "prefix": "(for listen_and_type/audio_fill_blank) any pre-filled letters visible in the blank",
  "actions": [
    {"action": "click", "target": "exact text of the button/word to click"},
    {"action": "type", "target": "selector description", "value": "text to type"},
    {"action": "press", "key": "1"}
  ]
}

Exercise types and how to answer:

- image_choice: Cards with images and labels with number shortcuts (1, 2, 3...).
  actions = [{"action": "press", "key": "2"}]
  all_options = ["coffee", "tofu", "rice"], total_options = 3

- multiple_choice: Text options. If numbered, prefer key press.
  actions = [{"action": "press", "key": "1"}]
  all_options = list of all option texts, total_options = count

- checkbox: Reading comprehension with checkboxes. Multiple answers may be correct.
  actions = [{"action": "click", "target": "option text"}, ...]

- word_bank: Click words in correct order from the word bank.
  actions = [{"action": "click", "target": "word1"}, {"action": "click", "target": "word2"}, ...]
  IMPORTANT: "question" must contain the FULL visible sentence being translated.

- typing: Type the answer in the text field.
  actions = [{"action": "type", "target": "input", "value": "the answer"}]

- matching / tap_pairs: "Select the matching pairs" / "Chọn cặp từ"
  Text-Text: actions = [{"action": "press", "key": "1"}, {"action": "press", "key": "7"}, ...]
  Audio-Text: return type="audio_matching" instead.

- audio_matching: Audio-to-text matching.
  actions = []
  all_options = right-side "number:text" list
  left_keys = ["1", "2", "3", "4"]

- audio_fill_blank: "Nghe và tìm từ còn thiếu" with audio option cards.
  actions = []
  sentence = "The sentence with ___"
  all_options = ["1", "2"]

- listen_and_type: "Nhập từ còn thiếu" / "Type the missing word" with text input.
  actions = []
  sentence = "I don't have any ___."
  prefix = "su" (pre-filled letters, or "")

- listening: "Tap what you hear" / "Nghe và điền" with word bank below speaker.
  actions = []
  all_options = list of all visible word chips

- speaking: "Đọc câu này" — cannot be solved, will be skipped.
  actions = []

- no_question: No exercise visible. actions = []

IMPORTANT:
- For word_bank: each "target" must be the exact visible text of the word on screen
- For multiple_choice: use "press" with number shortcuts when visible
- Be precise with text — it must match what's on screen exactly
"""


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def human_sleep(min_s=0.3, max_s=1.0):
    time.sleep(random.uniform(min_s, max_s))


def normalize_quotes(text):
    return text.replace("\u2018", "'").replace("\u2019", "'").replace("\u02BC", "'").replace("\u0060", "'")


# ---------------------------------------------------------------------------
# Exercise handlers
# ---------------------------------------------------------------------------


def get_input_prefix(page):
    for sel in [
        '[data-test="challenge-text-input"]',
        'input[type="text"]', 'textarea',
        '[contenteditable="true"]',
    ]:
        try:
            loc = page.locator(sel).first
            if not loc.is_visible(timeout=300):
                continue
            try:
                val = loc.input_value(timeout=300)
            except Exception:
                val = loc.inner_text(timeout=300) or ""
            if val and val.strip() and val.strip().isalpha() and len(val.strip()) < 15:
                return val.strip()
        except Exception:
            continue

    for sel in [
        '[data-test="challenge-translate-input"]',
        '[data-test="challenge-input"]',
        '[class*="challenge"] [class*="input"]',
    ]:
        try:
            container = page.locator(sel).first
            if not container.is_visible(timeout=300):
                continue
            spans = container.locator("span")
            for i in range(spans.count()):
                t = spans.nth(i).inner_text(timeout=200).strip()
                if t and t.isalpha() and len(t) < 15:
                    return t
        except Exception:
            continue
    return ""


def extract_missing_from_cached(sentence, cached_answer):
    blank_pattern = re.compile(r'_+')
    if not sentence or not cached_answer:
        return None
    if not blank_pattern.search(sentence):
        return None
    parts = blank_pattern.split(sentence)
    if len(parts) < 2:
        return None
    before = parts[0].strip().rstrip('.,!?')
    after = parts[1].strip().lstrip('.,!?') if len(parts) > 1 else ""
    cached_clean = cached_answer.strip()
    if before:
        before_clean = before.lower().rstrip()
        start_idx = cached_clean.lower().find(before_clean)
        if start_idx >= 0:
            remaining = cached_clean[start_idx + len(before_clean):].strip()
            if after:
                after_clean = after.lower().strip().lstrip('.,!?')
                end_idx = remaining.lower().find(after_clean)
                if end_idx >= 0:
                    return remaining[:end_idx].strip()
            return remaining.strip().rstrip('.')
    return None


def handle_listen_and_type(page, result):
    """Handle fill-in-the-blank exercises. No audio — infer from context/cache."""
    sentence = result.get("sentence", "") or result.get("question", "")
    cached_answer = result.get("answer", "")
    print(f"  Sentence: {sentence}")

    is_dictation = any(kw in sentence for kw in [
        "Nhập lại nội dung", "nội dung bạn vừa nghe",
        "Type what you hear", "Write what you hear",
    ])
    if not is_dictation and "___" not in sentence and "_" not in sentence:
        is_dictation = True

    if is_dictation:
        if cached_answer:
            type_answer(page, cached_answer)
            return True
        print("  ⚠ Dictation without audio, skipping...")
        skip_if_stuck(page)
        return False

    prefix = get_input_prefix(page)
    if not prefix:
        prefix = result.get("prefix", "")
    if prefix:
        print(f"  Pre-filled prefix: '{prefix}'")

    missing_word = None
    q_text = result.get("question", "")
    full_cached = answer_cache.get(normalize_cache_key(q_text), "")

    if sentence:
        if full_cached:
            missing_word = extract_missing_from_cached(sentence, full_cached)
            if missing_word:
                print(f"  Missing word (from cache): '{missing_word}'")
        if not missing_word and cached_answer:
            extracted = extract_missing_from_cached(sentence, cached_answer)
            if extracted:
                missing_word = extracted
                print(f"  Missing word (from AI sentence): '{missing_word}'")
            elif len(cached_answer.split()) <= 3:
                missing_word = cached_answer.strip().rstrip(".")
                print(f"  Missing word (from AI answer): '{missing_word}'")
        if missing_word and prefix and not missing_word.lower().startswith(prefix.lower()):
            missing_word = None

    if not missing_word and sentence:
        prompt_parts = [
            'EXERCISE: Duolingo "Type the missing word" (English course for Vietnamese speakers)',
            f'\nSENTENCE WITH BLANK: "{sentence}"',
        ]
        if prefix:
            prompt_parts.append(
                f'PRE-FILLED PREFIX in the blank: "{prefix}" — the missing word MUST start with "{prefix}".'
            )
        prompt_parts.append(
            '\nWhat is the SINGLE missing word or phrase that fills the blank? '
            'Consider English grammar, vocabulary, and the sentence context. '
            'Reply with ONLY the missing word(s), nothing else.'
        )
        try:
            r = client.responses.create(
                model=CHAT_MODEL,
                input=[{"role": "user", "content": "\n".join(prompt_parts)}],
            )
            missing_word = r.output_text.strip().strip('"').strip("'").rstrip(".")
            print(f"  Missing word (GPT): '{missing_word}'")
        except Exception as e:
            print(f"  ⚠ GPT extraction failed: {e}")

    if missing_word and prefix and not missing_word.lower().startswith(prefix.lower()):
        missing_word = None

    if not missing_word:
        print("  ⚠ Could not find missing word, skipping...")
        skip_if_stuck(page)
        return False

    if prefix and missing_word.lower().startswith(prefix.lower()):
        remaining_text = missing_word[len(prefix):]
        if remaining_text:
            print(f"  Typing remaining after prefix '{prefix}': '{remaining_text}'")
            type_answer(page, remaining_text)
        else:
            print(f"  Word already fully pre-filled")
    else:
        print(f"  Typing missing word: '{missing_word}'")
        type_answer(page, missing_word)

    return True


# ---------------------------------------------------------------------------
# Screen analysis
# ---------------------------------------------------------------------------

def analyze_screen(img):
    b64 = base64.b64encode(img).decode()
    r = client.responses.create(
        model=CHAT_MODEL,
        input=[
            {
                "role": "developer",
                "content": (
                    "You are an expert Duolingo exercise solver with perfect accuracy. "
                    "Analyze screenshots precisely. Extract ALL text exactly as shown — "
                    "never summarize or paraphrase. Return valid JSON only."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": PROMPT},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            },
        ],
    )
    raw = r.output_text.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```\w*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# DOM interaction helpers
# ---------------------------------------------------------------------------

def extract_display_text(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return (lines[0], "") if lines else (text, "")


UI_BUTTON_TEXTS = {"check", "skip", "continue", "can't listen now",
                   "use keyboard", "start", "guidebook", "next",
                   "kiểm tra", "bỏ qua", "tiếp tục", "luyện tập lại",
                   "practice again"}


def _is_ui_button(text):
    """Return True if text looks like a UI button rather than a word bank token."""
    return text.lower() in UI_BUTTON_TEXTS or len(text) > 30


def get_all_word_tokens(page):
    tokens = []
    token_selectors = [
        '[data-test="challenge-tap-token"]',
        '[data-test="challenge-tap-token-text"]',
        '[data-test="word-bank"] button',
        'button[data-test*="tap-token"]',
        '[class*="wordBank"] button',
    ]
    for sel in token_selectors:
        try:
            locs = page.locator(sel)
            count = locs.count()
            if count == 0:
                continue
            for i in range(count):
                loc = locs.nth(i)
                try:
                    if not loc.is_visible(timeout=200):
                        continue
                    full_text = loc.inner_text(timeout=200).strip()
                    if not full_text:
                        continue
                    display_text, secondary = extract_display_text(full_text)
                    tokens.append({
                        "full_text": full_text,
                        "display_text": display_text,
                        "secondary": secondary,
                        "locator": loc,
                    })
                except Exception:
                    continue
            if tokens:
                return tokens
        except Exception:
            continue

    if not tokens:
        try:
            all_buttons = page.locator("button")
            count = all_buttons.count()
            for i in range(count):
                btn = all_buttons.nth(i)
                try:
                    if not btn.is_visible(timeout=100):
                        continue
                    text = btn.inner_text(timeout=100).strip()
                    if not text or _is_ui_button(text):
                        continue
                    display_text, secondary = extract_display_text(text)
                    tokens.append({
                        "full_text": text,
                        "display_text": display_text,
                        "secondary": secondary,
                        "locator": btn,
                    })
                except Exception:
                    continue
        except Exception:
            pass
    return tokens


def get_word_bank_available_tokens(page):
    """Get only available (un-clicked) tokens from the word bank, not the answer area."""
    tokens = []

    bank_selectors = [
        ('[data-test="word-bank"] [data-test="challenge-tap-token"]', False),
        ('[data-test="word-bank"] button', False),
        ('[class*="wordBank"] button', False),
    ]

    for sel, _ in bank_selectors:
        try:
            locs = page.locator(sel)
            count = locs.count()
            if count == 0:
                continue
            for i in range(count):
                loc = locs.nth(i)
                try:
                    if not loc.is_visible(timeout=200):
                        continue
                    if loc.get_attribute("aria-disabled") == "true":
                        continue
                    if loc.get_attribute("disabled") is not None:
                        continue
                    full_text = loc.inner_text(timeout=200).strip()
                    if not full_text:
                        continue
                    display_text, secondary = extract_display_text(full_text)
                    tokens.append({
                        "full_text": full_text,
                        "display_text": display_text,
                        "secondary": secondary,
                        "locator": loc,
                    })
                except Exception:
                    continue
            if tokens:
                return tokens
        except Exception:
            continue

    fallback_selectors = [
        '[data-test="challenge-tap-token"]',
        'button[data-test*="tap-token"]',
    ]
    for sel in fallback_selectors:
        try:
            locs = page.locator(sel)
            count = locs.count()
            if count == 0:
                continue
            for i in range(count):
                loc = locs.nth(i)
                try:
                    if not loc.is_visible(timeout=200):
                        continue
                    if loc.get_attribute("aria-disabled") == "true":
                        continue
                    if loc.get_attribute("disabled") is not None:
                        continue
                    full_text = loc.inner_text(timeout=200).strip()
                    if not full_text:
                        continue
                    display_text, secondary = extract_display_text(full_text)
                    tokens.append({
                        "full_text": full_text,
                        "display_text": display_text,
                        "secondary": secondary,
                        "locator": loc,
                    })
                except Exception:
                    continue
            if tokens:
                return tokens
        except Exception:
            continue

    return tokens


def execute_word_bank_sequence(page, word_order):
    """Click word bank tokens in order, pre-collecting tokens to handle duplicates."""
    tokens = get_word_bank_available_tokens(page)
    if not tokens:
        print("  ⚠ No word bank tokens found")
        return False

    token_map = {}
    for token in tokens:
        key = token["display_text"]
        token_map.setdefault(key, []).append(token)

    token_map_lower = {}
    for token in tokens:
        key = token["display_text"].lower()
        token_map_lower.setdefault(key, []).append(token)

    for i, word in enumerate(word_order):
        if i > 0:
            time.sleep(random.uniform(0.12, 0.25))

        clicked = False

        if word in token_map and token_map[word]:
            token = token_map[word].pop(0)
            lower_key = word.lower()
            if lower_key in token_map_lower:
                token_map_lower[lower_key] = [t for t in token_map_lower[lower_key] if t is not token]
            try:
                print(f"  [{i+1}] Clicking: '{word}'")
                token["locator"].click(timeout=1000)
                clicked = True
            except Exception as e:
                print(f"  ⚠ Click failed for '{word}': {e}")

        if not clicked:
            lower_key = word.lower()
            if lower_key in token_map_lower and token_map_lower[lower_key]:
                token = token_map_lower[lower_key].pop(0)
                for key in token_map:
                    token_map[key] = [t for t in token_map[key] if t is not token]
                try:
                    print(f"  [{i+1}] Clicking: '{token['display_text']}' (for '{word}')")
                    token["locator"].click(timeout=1000)
                    clicked = True
                except Exception as e:
                    print(f"  ⚠ Click failed for '{word}': {e}")

        if not clicked:
            norm_word = normalize_quotes(word)
            for key in list(token_map.keys()):
                if token_map[key] and (norm_word in normalize_quotes(key) or normalize_quotes(key) in norm_word):
                    token = token_map[key].pop(0)
                    try:
                        print(f"  [{i+1}] Clicking: '{token['display_text']}' (partial for '{word}')")
                        token["locator"].click(timeout=1000)
                        clicked = True
                        break
                    except Exception:
                        pass

        if not clicked:
            print(f"  ⚠ Could not find token for: '{word}'")
            click_word_token(page, word)

    return True


def click_word_token(page, text):
    tokens = get_all_word_tokens(page)
    if tokens:
        for token in tokens:
            if token["display_text"] == text or token["secondary"] == text:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue
        text_lower = text.lower()
        for token in tokens:
            if token["display_text"].lower() == text_lower:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue
        for token in tokens:
            if text in token["display_text"] or token["display_text"] in text:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue
        for token in tokens:
            if text.lower() in token["full_text"].lower():
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue
    return click_target_generic(page, text)


def click_target(page, text, q_type=""):
    if q_type in ("checkbox", "multiple_choice", "image_choice"):
        return click_challenge_option(page, text)
    if click_word_token(page, text):
        return True
    return click_target_generic(page, text)


def click_challenge_option(page, text):
    TIMEOUT = 2000

    try:
        for exact in [True, False]:
            text_el = page.get_by_text(text, exact=exact).first
            if text_el.is_visible(timeout=500):
                parent = text_el.locator("xpath=..")
                btn = parent.locator('button[data-test="stories-choice"]').first
                if btn.is_visible(timeout=300):
                    btn.click(timeout=TIMEOUT)
                    return True
    except Exception:
        pass

    try:
        buttons = page.locator('button[data-test="stories-choice"]')
        count = buttons.count()
        if count > 0:
            for i in range(count):
                btn = buttons.nth(i)
                parent_text = btn.locator("xpath=..").inner_text(timeout=500).strip()
                if text.lower() in parent_text.lower():
                    btn.click(timeout=TIMEOUT)
                    return True
    except Exception:
        pass

    choice_selectors = [
        '[data-test="challenge-choice"]',
        'div[role="checkbox"]', 'div[role="radio"]', 'div[role="listitem"]',
    ]
    for sel in choice_selectors:
        try:
            choices = page.locator(sel)
            count = choices.count()
            for i in range(count):
                choice = choices.nth(i)
                try:
                    choice_text = choice.inner_text(timeout=500).strip()
                except Exception:
                    continue
                if text.lower() in choice_text.lower():
                    try:
                        choice.click(timeout=TIMEOUT)
                        return True
                    except Exception:
                        page.keyboard.press(str(i + 1))
                        return True
        except Exception:
            continue

    for exact in [True, False]:
        try:
            text_el = page.get_by_text(text, exact=exact).first
            if not text_el.is_visible(timeout=500):
                continue
            for ancestor_sel in [
                'xpath=ancestor::*[@data-test="challenge-choice"]',
                'xpath=ancestor::div[@role="checkbox"]',
                'xpath=ancestor::div[@role="radio"]',
                'xpath=ancestor::div[contains(@class,"choice")]',
                'xpath=ancestor::label',
            ]:
                try:
                    parent = text_el.locator(ancestor_sel).first
                    if parent.is_visible(timeout=300):
                        parent.click(timeout=TIMEOUT)
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    if click_word_token(page, text):
        return True
    return click_target_generic(page, text)


def click_target_generic(page, text):
    TIMEOUT = 2000
    selectors = [
        f'[data-test="challenge-choice"]:has-text("{text}")',
        f'[data-test="challenge-judge-text"]:has-text("{text}")',
        f'label:has-text("{text}")',
        f'div[role="checkbox"]:has-text("{text}")',
        f'div[role="radio"]:has-text("{text}")',
        f'button:has-text("{text}")',
        f'div[role="button"]:has-text("{text}")',
    ]
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=TIMEOUT)
            return True
        except Exception:
            continue
    for exact in [True, False]:
        try:
            page.get_by_text(text, exact=exact).first.click(timeout=TIMEOUT)
            return True
        except Exception:
            continue
    print(f"  ⚠ Could not find: '{text}'")
    return False


def type_answer(page, text):
    selectors = [
        '[data-test="challenge-text-input"]',
        'input[type="text"]', 'textarea',
        '[contenteditable="true"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=300)
            try:
                existing = loc.input_value(timeout=300)
            except Exception:
                existing = loc.inner_text(timeout=300) or ""
            to_type = text
            if existing and text.lower().startswith(existing.lower()):
                to_type = text[len(existing):]
                if not to_type:
                    return True
            elif existing:
                loc.fill("")
            for char in to_type:
                loc.type(char, delay=random.randint(8, 25))
            return True
        except Exception:
            continue
    try:
        page.keyboard.type(text, delay=random.randint(8, 25))
        return True
    except Exception:
        return False


def click_button(page, texts):
    for text in texts:
        try:
            page.locator(f'button:has-text("{text}")').first.click(timeout=500)
            return True
        except Exception:
            continue
    return False


def click_check_button(page):
    """Click the Check/Submit button specifically, avoiding word bank token buttons."""
    # Strategy 1: Duolingo's player-next button (most specific)
    for sel in ['[data-test="player-next"] button', 'button[data-test="player-next"]']:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=300):
                btn.click(timeout=1000)
                return True
        except Exception:
            continue

    # Strategy 2: Find a button whose trimmed text exactly matches "Check" variants,
    # excluding word bank tap-token buttons
    for check_text in ["Check", "KIỂM TRA", "CHECK", "Kiểm tra"]:
        try:
            buttons = page.locator("button")
            count = buttons.count()
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible(timeout=100):
                        continue
                    if btn.get_attribute("data-test") and "tap-token" in (btn.get_attribute("data-test") or ""):
                        continue
                    btn_text = btn.inner_text(timeout=100).strip()
                    if btn_text == check_text:
                        btn.click(timeout=1000)
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    # Strategy 3: Fallback to has-text (but exclude tap-token elements)
    for text in ["Check", "KIỂM TRA", "CHECK", "Kiểm tra"]:
        try:
            loc = page.locator(f'button:has-text("{text}"):not([data-test*="tap-token"])')
            if loc.first.is_visible(timeout=300):
                loc.first.click(timeout=1000)
                return True
        except Exception:
            continue

    # Strategy 4: Press Enter as last resort (submits the answer in Duolingo)
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        pass

    return False


def skip_if_stuck(page):
    skip_texts = [
        "HIỆN KHÔNG NGHE ĐƯỢC", "Hiện không nghe được",
        "TẠM THỜI KHÔNG NÓI ĐƯỢC", "Tạm thời không nói được",
        "CAN'T LISTEN NOW", "CAN'T SPEAK NOW",
        "Skip", "SKIP", "BỎ QUA",
    ]
    if click_button(page, skip_texts):
        return True
    for text in skip_texts:
        try:
            loc = page.get_by_text(text, exact=False).first
            if loc.is_visible(timeout=500):
                loc.click(timeout=1000)
                return True
        except Exception:
            continue
    return False


def capture_correct_answer(page, question_text=""):
    try:
        feedback_text = None
        for sel in [
            '[data-test="blame blame-incorrect"]', '[data-test="blame"]',
            '[data-test="challenge-judge-text"]',
            'div[class*="incorrect"]', 'div[class*="blame"]',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=500):
                    feedback_text = el.inner_text(timeout=500)
                    if feedback_text and len(feedback_text.strip()) > 3:
                        break
            except Exception:
                continue

        if not feedback_text:
            try:
                body_text = page.inner_text("body", timeout=500)
                for kw in ['Đáp án đúng:', 'Correct solution:', 'Correct answer:']:
                    if kw in body_text:
                        idx = body_text.index(kw)
                        feedback_text = body_text[idx:idx + 200]
                        break
            except Exception:
                pass

        if not feedback_text:
            return None

        correct = None
        lines = feedback_text.strip().split('\n')
        for i, line in enumerate(lines):
            line_lower = line.strip().lower()
            if any(kw in line_lower for kw in [
                'đáp án đúng', 'correct solution', 'correct answer', 'câu trả lời đúng'
            ]):
                if ':' in line.strip():
                    after = line.strip().split(':', 1)[1].strip()
                    if after:
                        correct = after
                        break
                if i + 1 < len(lines) and lines[i + 1].strip():
                    correct = lines[i + 1].strip()
                    break

        if correct:
            for noise in ['BÁO CÁO', 'TIẾP TỤC', 'CONTINUE', 'REPORT']:
                correct = correct.replace(noise, '').strip()

        if correct and question_text and not _is_generic_question(question_text):
            cache_key = normalize_cache_key(question_text)
            existing = answer_cache.get(cache_key)
            if existing and existing.strip().lower() == correct.strip().lower():
                pass
            else:
                answer_cache[cache_key] = correct
                cache_fail_count.pop(cache_key, None)
                print(f"  📝 Cached: '{question_text}' → '{correct}'")

        return correct
    except Exception:
        return None


def _detect_feedback_banner(page, timeout_ms=1500):
    """Check whether a feedback banner (correct/incorrect) is visible."""
    banner_selectors = [
        '[data-test*="blame-incorrect"]',
        '[data-test*="blame-correct"]',
        '[data-test="blame"]',
        '[data-test="challenge-judge-text"]',
        'div[class*="blame"]',
    ]
    for sel in banner_selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            continue
    return False


def handle_post_answer(page, question_text=""):
    """Returns 'correct', 'incorrect', or 'no_feedback'."""
    human_sleep(0.1, 0.2)

    check_clicked = click_check_button(page)
    human_sleep(0.2, 0.4)

    banner_visible = _detect_feedback_banner(page, timeout_ms=1500)

    if not banner_visible and not check_clicked:
        try:
            page.keyboard.press("Enter")
            human_sleep(0.3, 0.5)
            banner_visible = _detect_feedback_banner(page, timeout_ms=2000)
        except Exception:
            pass

    if not banner_visible:
        print(f"  ⚠ No feedback banner detected")
        click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
        human_sleep(0.15, 0.3)
        return "no_feedback"

    correct_answer = capture_correct_answer(page, question_text)
    result_status = "correct"
    if correct_answer:
        print(f"  ❌ Incorrect! Correct: {correct_answer}")
        result_status = "incorrect"
    else:
        is_incorrect = False
        try:
            is_incorrect = page.locator('[data-test*="blame-incorrect"]').first.is_visible(timeout=300)
        except Exception:
            pass
        if not is_incorrect:
            try:
                body = page.inner_text("body", timeout=300)
                for kw in ['Đáp án đúng:', 'Correct solution:']:
                    if kw in body:
                        is_incorrect = True
                        break
            except Exception:
                pass
        if is_incorrect:
            print(f"  ❌ Incorrect!")
            result_status = "incorrect"
        else:
            print(f"  ✅ Correct!")

    click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
    human_sleep(0.15, 0.3)
    return result_status


# ---------------------------------------------------------------------------
# Refinement helpers
# ---------------------------------------------------------------------------

def refine_multiple_choice_actions(page, result):
    try:
        choices = None
        is_stories = False
        for sel in [
            '[data-test="challenge-choice"]', '[data-test="stories-choice"]',
            '[data-test="challenge-judge-text"]', 'div[role="radiogroup"] > div',
            'div[role="checkbox"]', 'div[role="radio"]', 'li[data-test]',
        ]:
            loc = page.locator(sel)
            if loc.count() >= 2:
                choices = loc
                is_stories = "stories-choice" in sel
                break
        if not choices or choices.count() == 0:
            return
        count = choices.count()

        visible_options = []
        for i in range(count):
            try:
                if is_stories:
                    text = choices.nth(i).locator("xpath=..").inner_text(timeout=500).strip()
                else:
                    text = choices.nth(i).inner_text(timeout=500).strip()
                if text:
                    visible_options.append({"index": i + 1, "text": text})
            except Exception:
                continue

        if not visible_options:
            return

        question = result.get("question", "")
        ai_answer = result.get("answer", "")
        q_type = result.get("type", "")
        cached = answer_cache.get(normalize_cache_key(question), "")

        if not is_stories:
            for act in result.get("actions", []):
                key = act.get("key", "")
                if key and key.isdigit() and int(key) <= count:
                    return

        def make_action(opt):
            if is_stories:
                return {"action": "click", "target": opt["text"]}
            return {"action": "press", "key": str(opt["index"])}

        answer_to_match = cached or ai_answer
        if answer_to_match:
            for opt in visible_options:
                if answer_to_match.lower().strip() in opt["text"].lower():
                    result["actions"] = [make_action(opt)]
                    print(f"  📋 Matched answer to option {opt['index']}")
                    return

        print(f"  📋 Visible options: {[o['text'] for o in visible_options]}")

        system_msg = (
            'You are an expert Duolingo solver for an English course for Vietnamese speakers. '
            'Select the correct option(s) based on grammar, meaning, and context. '
            'Reply with ONLY the option number(s).'
        )

        options_str = "\n".join(f'  {o["index"]}. {o["text"]}' for o in visible_options)
        prompt_parts = [
            f'EXERCISE TYPE: Duolingo {q_type}',
            f'COURSE: English for Vietnamese speakers (translating between Vietnamese ↔ English)',
            f'\nQUESTION: {question}',
        ]
        if cached:
            prompt_parts.append(f'\nKNOWN CORRECT ANSWER (previously verified): {cached}')
        if ai_answer and ai_answer != cached:
            prompt_parts.append(f'AI SUGGESTED ANSWER: {ai_answer}')
        prompt_parts.append(f'\nVISIBLE OPTIONS:\n{options_str}')
        if q_type == "checkbox":
            prompt_parts.append(
                '\nSelect ALL correct options. Consider the full context/passage above.\n'
                'Reply with ONLY the number(s) separated by commas. Example: 1,2'
            )
        else:
            prompt_parts.append(
                '\nSelect the ONE correct option. Consider grammar, vocabulary, and context.\n'
                'Reply with ONLY the number, nothing else. Example: 1'
            )

        r = client.responses.create(
            model=CHAT_MODEL,
            input=[
                {"role": "developer", "content": system_msg},
                {"role": "user", "content": "\n".join(prompt_parts)},
            ],
        )
        picked = r.output_text.strip().replace(" ", "")
        keys = [k.strip() for k in picked.split(",") if k.strip().isdigit()]
        if keys:
            matched_opts = [o for o in visible_options if str(o["index"]) in keys]
            result["actions"] = [make_action(o) for o in matched_opts]
            print(f"  ✅ Picked option(s): {keys}")
    except Exception as e:
        print(f"  ⚠ MC refinement failed: {e}")


def _strip_punctuation(text):
    """Strip trailing/leading punctuation and commas from cached answers for word bank matching."""
    words = text.split()
    cleaned = []
    for w in words:
        w = w.strip(".,!?;:")
        if w:
            cleaned.append(w)
    return " ".join(cleaned)


def _try_split_contraction(word, remaining):
    """Try splitting a contraction into parts that match word bank tokens.
    e.g. "we're" → "we" + "'re", "don't" → "don" + "'t"
    Returns (matched_tokens, updated_remaining) or None."""
    norm = normalize_quotes(word)
    if "'" not in norm:
        return None
    parts = norm.split("'", 1)
    p1, p2_suffix = parts[0], "'" + parts[1]

    remaining_norm = [(rw, normalize_quotes(rw)) for rw in remaining]

    r1 = next((rw for rw, rn in remaining_norm if rn.lower() == p1.lower()), None)
    if not r1:
        return None
    r2 = next((rw for rw, rn in remaining_norm if rn.lower() == p2_suffix.lower() and rw != r1), None)
    if not r2:
        return None

    new_remaining = list(remaining)
    new_remaining.remove(r1)
    new_remaining.remove(r2)
    return ([r1, r2], new_remaining)


def refine_word_bank_actions(page, result):
    tokens = get_word_bank_available_tokens(page)
    if not tokens:
        tokens = get_all_word_tokens(page)
    if not tokens:
        return

    available_words = [t["display_text"] for t in tokens]
    question = result.get("question", "")
    ai_answer = result.get("answer", "")
    cached = answer_cache.get(normalize_cache_key(question), "")

    print(f"  📋 Word bank tokens: {available_words}")

    if cached:
        cached_clean = _strip_punctuation(cached)
        available_lower = [w.lower() for w in available_words]
        if cached_clean.lower() in available_lower:
            idx = available_lower.index(cached_clean.lower())
            result["actions"] = [{"action": "click", "target": available_words[idx]}]
            print(f"  ✅ Cached answer matches token: '{available_words[idx]}'")
            return

        cached_split = cached_clean.split()
        matched_tokens = []
        remaining = list(available_words)
        all_matched = True
        for word in cached_split:
            found = False
            for rw in remaining:
                if rw.lower() == word.lower() or normalize_quotes(rw).lower() == normalize_quotes(word).lower():
                    matched_tokens.append(rw)
                    remaining.remove(rw)
                    found = True
                    break
            if not found:
                split_result = _try_split_contraction(word, remaining)
                if split_result:
                    parts, remaining = split_result
                    matched_tokens.extend(parts)
                    found = True
            if not found:
                all_matched = False
                break
        if all_matched and matched_tokens:
            result["actions"] = [{"action": "click", "target": w} for w in matched_tokens]
            print(f"  ✅ Cached answer matched tokens: {matched_tokens}")
            return

    print(f"  🔄 Arranging words using actual word bank...")
    try:
        system_msg = (
            'You are an expert Duolingo solver for an English course for Vietnamese speakers. '
            'Your task is to arrange word bank tokens into the correct English sentence. '
            'You must ONLY output words separated by " | " — nothing else.'
        )

        prompt_parts = [
            'TASK: Arrange word bank tokens to form the correct English translation.',
            f'\nSOURCE (Vietnamese question/sentence on screen): {question}',
        ]
        if cached:
            prompt_parts.append(f'\nKNOWN CORRECT ANSWER (use this to determine exact word order): {cached}')
        if ai_answer and ai_answer != cached:
            prompt_parts.append(f'AI SUGGESTED ANSWER: {ai_answer}')
        prompt_parts.append(
            f'\nAVAILABLE WORD BANK TOKENS (you can ONLY use these exact strings): {available_words}'
        )
        prompt_parts.append(
            f'Total tokens in bank: {len(available_words)} (some are distractors — do NOT use all of them)'
        )
        prompt_parts.append(
            '\nCRITICAL RULES:\n'
            '1. EVERY token you output must EXACTLY match one item in the available list (case-sensitive).\n'
            '2. The answer must be a complete, grammatically correct English sentence.\n'
            '3. Include ALL necessary words: articles (a, an, the), prepositions (to, for, in, at, on), '
            'pronouns, and auxiliary verbs (will, have, has, do, does, can, could, would, should).\n'
            '4. Contractions may be SPLIT: "don\'t" → "don" + "\'t", "we\'re" → "we" + "\'re", '
            '"I\'ll" → "I" + "\'ll". Include ALL parts.\n'
            '5. Punctuation like "." or "," may be a separate token — include it if present in the bank.\n'
            '6. If a KNOWN CORRECT ANSWER is provided, use it as ground truth for word order.\n'
            '7. Distractor words exist in the bank — ignore words that do not belong in the sentence.\n'
            '\nReply with ONLY the words separated by " | " (pipe), nothing else.\n'
            'Example: We | will | pay | them | next | week | .'
        )

        r = client.responses.create(
            model=CHAT_MODEL,
            input=[
                {"role": "developer", "content": system_msg},
                {"role": "user", "content": "\n".join(prompt_parts)},
            ],
        )
        ordered = [w.strip() for w in r.output_text.strip().split("|") if w.strip()]

        valid_ordered = []
        remaining = list(available_words)
        for word in ordered:
            if word in remaining:
                valid_ordered.append(word)
                remaining.remove(word)
                continue
            matched = False
            for rw in remaining:
                if rw.lower() == word.lower() or normalize_quotes(rw).lower() == normalize_quotes(word).lower():
                    valid_ordered.append(rw)
                    remaining.remove(rw)
                    matched = True
                    break
            if not matched:
                split_result = _try_split_contraction(word, remaining)
                if split_result:
                    parts, remaining = split_result
                    valid_ordered.extend(parts)

        if valid_ordered:
            result["actions"] = [{"action": "click", "target": w} for w in valid_ordered]
            print(f"  ✅ Word order: {valid_ordered}")
    except Exception as e:
        print(f"  ⚠ Word bank refinement failed: {e}")


def execute_actions(page, result):
    actions = result.get("actions", [])
    if not actions:
        print("  No actions to perform")
        return False

    q_type = result.get("type", "")

    if q_type == "word_bank":
        word_order = [act.get("target", "") for act in actions
                      if act.get("action") == "click" and act.get("target")]
        if word_order:
            return execute_word_bank_sequence(page, word_order)

    is_matching = q_type in ("matching", "tap_pairs")

    for i, act in enumerate(actions):
        action = act["action"]
        target = act.get("target", "")
        value = act.get("value", "")
        key = act.get("key", "")

        if i > 0:
            human_sleep(0.08, 0.15)

        if action == "press":
            print(f"  [{i+1}] Pressing key: '{key}'")
            page.keyboard.press(key)
        elif action == "click":
            print(f"  [{i+1}] Clicking: '{target}'")
            click_target(page, target, q_type=q_type)
            time.sleep(0.08)
        elif action == "type":
            print(f"  [{i+1}] Typing: '{value}'")
            type_answer(page, value)

    return True


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login_with_jwt(context, page):
    if not DUO_JWT:
        return False
    print("  Using DUO_JWT token...")
    context.add_cookies([{
        "name": "jwt_token", "value": DUO_JWT,
        "domain": ".duolingo.com", "path": "/",
    }])
    page.goto(PRACTICE_URL)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)
    if "/login" not in page.url and "/log-in" not in page.url:
        print(f"  JWT login successful!")
        return True
    return False


def login_duolingo_via_browser(page):
    print("  Using browser-based API login...")
    page.goto("https://www.duolingo.com/")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    result = page.evaluate("""async ([email, password]) => {
        try {
            const resp = await fetch('https://www.duolingo.com/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({identifier: email, password: password}),
                credentials: 'include'
            });
            const body = await resp.json();
            return {ok: resp.ok, status: resp.status, username: body.username || null,
                    failure: body.failure || null, message: body.message || null};
        } catch(e) {
            return {ok: false, error: e.message};
        }
    }""", [EMAIL, PASSWORD])

    if not result.get("ok"):
        raise Exception(f"Browser API login failed: {result}")
    if result.get("failure"):
        raise Exception(f"Login rejected: {result.get('failure')}")

    print(f"  Login successful (username: {result.get('username')})")
    return True


def login_duolingo(page):
    page.goto("https://www.duolingo.com/?isLoggingIn=true")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)

    for sel in [
        'button:has-text("I ALREADY HAVE AN ACCOUNT")',
        'button:has-text("SIGN IN")', 'button:has-text("LOG IN")',
        'a:has-text("I ALREADY HAVE AN ACCOUNT")',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                page.wait_for_timeout(2000)
                break
        except Exception:
            continue

    email_input = None
    for sel in ['#web-ui1', 'input[data-test="email-input"]', 'input[type="email"]', 'input[type="text"]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(timeout=3000)
            email_input = loc
            break
        except Exception:
            continue

    password_input = None
    for sel in ['#web-ui2', 'input[data-test="password-input"]', 'input[type="password"]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(timeout=3000)
            password_input = loc
            break
        except Exception:
            continue

    if not email_input or not password_input:
        raise Exception(f"Could not find login fields. URL: {page.url}")

    email_input.fill(EMAIL)
    human_sleep(0.5, 1.0)
    password_input.fill(PASSWORD)
    human_sleep(0.5, 1.5)

    for sel in ['button:has-text("LOG IN")', 'button:has-text("SIGN IN")', 'button[type="submit"]']:
        try:
            page.locator(sel).first.click(timeout=3000)
            break
        except Exception:
            continue

    try:
        page.wait_for_url("**/learn**", timeout=30000)
    except Exception:
        page.wait_for_timeout(5000)
        if "/log-in" in page.url:
            raise Exception(f"Login failed: {page.url}")


# ---------------------------------------------------------------------------
# Main practice loop
# ---------------------------------------------------------------------------

def main():
    with sync_playwright() as p:

        headless = os.getenv("HEADLESS", "false").lower() == "true"
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )

        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

        video_dir = os.path.join(os.getcwd(), "playwright-videos")
        os.makedirs(video_dir, exist_ok=True)

        if os.path.exists(SESSION_FILE):
            print("Loading saved session...")
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                storage_state=SESSION_FILE, user_agent=ua,
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 800},
            )
        else:
            print("No session found → logging in")
            context = browser.new_context(
                viewport={"width": 1280, "height": 800}, user_agent=ua,
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 800},
            )

        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            delete navigator.__proto__.webdriver;
        """)

        def do_login():
            try:
                if login_with_jwt(context, page):
                    return True
            except Exception as e:
                print(f"  JWT login failed: {e}")
            try:
                if login_duolingo_via_browser(page):
                    return True
            except Exception as e:
                print(f"  Browser API login failed: {e}")
            if not headless:
                login_duolingo(page)
                return True
            return False

        if not os.path.exists(SESSION_FILE):
            if not do_login():
                raise Exception("All login methods failed")
            context.storage_state(path=SESSION_FILE)
        else:
            page.goto(PRACTICE_URL)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

            if "/login" in page.url or "/log-in" in page.url:
                print("⚠ Session expired, logging in again...")
                if not do_login():
                    raise Exception(f"Login failed. URL: {page.url}")
                context.storage_state(path=SESSION_FILE)

        # --- Start practice ---
        print("\n🏋️ Starting Practice Mode XP Farm")
        print(f"  URL: {PRACTICE_URL}")
        if MAX_SESSIONS > 0:
            print(f"  Max sessions: {MAX_SESSIONS}")
        else:
            print(f"  Max sessions: unlimited")

        session_count = 0
        total_questions = 0
        farm_start = time.time()

        while True:
            session_count += 1
            print(f"\n{'='*50}")
            print(f"🔄 Practice Session #{session_count}")
            print(f"{'='*50}")

            page.goto(PRACTICE_URL)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)

            consecutive_no_question = 0
            consecutive_continue = 0
            question_count = 0
            last_question_text = ""
            consecutive_same_question = 0
            consecutive_no_feedback = 0

            MAX_SAME_QUESTION = 3
            MAX_NO_FEEDBACK = 8

            while True:
                try:
                    q_start = time.time()
                    human_sleep(0.5, 0.8)

                    # Step 1: Check for "LUYỆN TẬP LẠI" button — only exists on completion screen
                    lesson_done = False
                    for done_text in ["LUYỆN TẬP LẠI", "Luyện tập lại", "PRACTICE AGAIN", "Practice again"]:
                        try:
                            el = page.get_by_text(done_text, exact=True).first
                            if el.is_visible(timeout=300):
                                print(f"\n  🎉 Lesson complete! (detected '{done_text}')")
                                for cont_text in ["TIẾP TỤC", "Tiếp tục", "Continue", "CONTINUE"]:
                                    try:
                                        btn = page.get_by_text(cont_text, exact=True).first
                                        if btn.is_visible(timeout=500):
                                            btn.click(timeout=1000)
                                            break
                                    except Exception:
                                        continue
                                human_sleep(0.3, 0.5)
                                total_questions += question_count
                                elapsed = time.time() - farm_start
                                print(f"  ✅ Practice complete! ({question_count} questions)")
                                print(f"  📊 Total: {session_count} sessions, {total_questions} questions, {elapsed:.0f}s")
                                lesson_done = True
                                break
                        except Exception:
                            continue
                    if lesson_done:
                        break

                    # Step 2: Check if "TIẾP TỤC" button is visible (previous answer done)
                    tiep_tuc_clicked = False
                    for cont_text in ["TIẾP TỤC", "Tiếp tục", "Continue", "CONTINUE"]:
                        try:
                            # Try button first
                            cont_btn = page.locator(f'button:has-text("{cont_text}")').first
                            if cont_btn.is_visible(timeout=200) and cont_btn.is_enabled(timeout=200):
                                cont_btn.click(timeout=1000)
                                print(f"  ⏩ Clicked '{cont_text}'")
                                human_sleep(0.2, 0.4)
                                tiep_tuc_clicked = True
                                consecutive_continue += 1
                                break
                        except Exception:
                            continue
                    if not tiep_tuc_clicked:
                        # Try any clickable element (a, div, etc.)
                        for cont_text in ["TIẾP TỤC", "Continue"]:
                            try:
                                el = page.get_by_text(cont_text, exact=True).first
                                if el.is_visible(timeout=200):
                                    el.click(timeout=1000)
                                    print(f"  ⏩ Clicked '{cont_text}' (non-button)")
                                    human_sleep(0.2, 0.4)
                                    tiep_tuc_clicked = True
                                    consecutive_continue += 1
                                    break
                            except Exception:
                                continue
                    if tiep_tuc_clicked:
                        if consecutive_continue >= 5:
                            print(f"  ⚠ Stuck clicking TIẾP TỤC {consecutive_continue}x, starting new session...")
                            total_questions += question_count
                            break
                        continue

                    consecutive_continue = 0

                    # Step 3: Take screenshot → AI → get answer
                    img = page.screenshot(type="jpeg", quality=80)
                    result = analyze_screen(img)

                    q_type = result.get("type", "unknown")
                    question = result.get("question", "")
                    answer = result.get("answer", "")

                    elapsed = time.time() - farm_start

                    if q_type == "no_question":
                        consecutive_no_question += 1
                        print(f"\n⏱ {elapsed:.0f}s | Session #{session_count} | No question detected, waiting...")

                        q_lower = question.lower()
                        if any(kw in q_lower for kw in ["log in", "login", "sign in"]):
                            print("  ⚠ Login screen detected!")
                            if not do_login():
                                raise Exception("Re-login failed")
                            context.storage_state(path=SESSION_FILE)
                            consecutive_no_question = 0
                            continue

                        if consecutive_no_question >= 5 and question_count > 0:
                            print(f"  ✅ Practice complete! ({question_count} questions)")
                            total_questions += question_count
                            elapsed = time.time() - farm_start
                            print(f"  📊 Total: {session_count} sessions, {total_questions} questions, {elapsed:.0f}s")
                            break

                        if consecutive_no_question >= 8 and question_count == 0:
                            print("  ⚠ Stuck: no questions detected.")
                            break

                        human_sleep(0.1, 0.2)
                        continue

                    consecutive_no_question = 0
                    question_count += 1

                    print(f"\n⏱ {elapsed:.0f}s | Session #{session_count} | Q{question_count} | Type: {q_type}")
                    print(f"  Question: {question}")
                    print(f"  Answer: {answer}")

                    q_text = result.get("question", "")

                    # --- Repeated-question detection ---
                    q_normalized = normalize_cache_key(q_text)
                    if q_normalized and q_normalized == normalize_cache_key(last_question_text):
                        consecutive_same_question += 1
                    else:
                        consecutive_same_question = 0
                    last_question_text = q_text

                    if consecutive_same_question >= MAX_SAME_QUESTION:
                        print(f"  🔁 Same question repeated {consecutive_same_question + 1}x — stuck loop detected")
                        skip_if_stuck(page)
                        human_sleep(0.2, 0.4)
                        click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
                        human_sleep(0.2, 0.4)
                        if consecutive_same_question >= MAX_SAME_QUESTION + 2:
                            print(f"  🔄 Restarting lesson after {consecutive_same_question + 1} repeats")
                            total_questions += question_count
                            break
                        q_elapsed = time.time() - q_start
                        print(f"  ⏱ Question took {q_elapsed:.1f}s")
                        continue

                    if question_count > 50:
                        print(f"  ⚠ Session exceeded 50 questions ({question_count}) — treating as stuck, starting new session...")
                        total_questions += question_count
                        break

                    if consecutive_no_feedback >= MAX_NO_FEEDBACK:
                        print(f"  🔄 No feedback detected for {consecutive_no_feedback} consecutive questions — restarting lesson")
                        total_questions += question_count
                        break

                    # Check answer cache (skip if generic or already failed too many times)
                    used_cache = False
                    q_cache_key = normalize_cache_key(q_text) if q_text else ""
                    if q_cache_key and q_cache_key in answer_cache:
                        if cache_fail_count.get(q_cache_key, 0) >= MAX_CACHE_FAILURES:
                            print(f"  💾 Cached answer evicted (failed {cache_fail_count[q_cache_key]}x): '{answer_cache[q_cache_key]}'")
                            del answer_cache[q_cache_key]
                            cache_fail_count.pop(q_cache_key, None)
                        elif _is_generic_question(q_text):
                            print(f"  💾 Skipping cache for generic question")
                        else:
                            cached = answer_cache[q_cache_key]
                            print(f"  💾 Found cached answer: '{cached}'")
                            used_cache = True
                            if q_type == "listen_and_type":
                                pass
                            elif q_type == "typing":
                                result["answer"] = cached
                                result["actions"] = [{"action": "type", "target": "input", "value": cached}]
                            elif q_type in ("multiple_choice", "image_choice"):
                                result["answer"] = cached
                                all_opts = result.get("all_options", [])
                                cached_lower = cached.lower().strip()
                                for idx, opt in enumerate(all_opts):
                                    if opt.lower().strip() == cached_lower:
                                        result["actions"] = [{"action": "press", "key": str(idx + 1)}]
                                        print(f"  💾 Mapped to option {idx + 1}")
                                        break
                                else:
                                    for idx, opt in enumerate(all_opts):
                                        if cached_lower in opt.lower() or opt.lower() in cached_lower:
                                            result["actions"] = [{"action": "press", "key": str(idx + 1)}]
                                            break
                                    else:
                                        result["actions"] = [{"action": "click", "target": cached}]
                            elif q_type == "word_bank":
                                result["answer"] = cached
                            else:
                                result["answer"] = cached

                    # Skip audio-only exercises (no audio in practice mode)
                    if q_type in ("audio_matching", "audio_fill_blank", "listening", "speaking"):
                        print(f"  🔇 Skipping {q_type} exercise (no audio)")
                        skip_if_stuck(page)
                        human_sleep(0.1, 0.2)
                        click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
                        human_sleep(0.1, 0.2)
                        continue

                    # Reclassify: typing exercises with blanks are really fill-in-blank
                    if q_type == "typing":
                        q_combined = f"{question} {result.get('sentence', '')}".lower()
                        has_blank = '___' in q_combined or '_' in q_combined
                        has_completion_hint = any(kw in q_combined for kw in [
                            'complete the sentence', 'hoàn thành câu',
                            'fill in the blank', 'điền vào chỗ trống',
                            'missing word', 'từ còn thiếu',
                        ])
                        if has_blank or has_completion_hint:
                            print(f"  🔄 Reclassified typing → listen_and_type (fill-in-blank detected)")
                            q_type = "listen_and_type"
                            result["type"] = "listen_and_type"

                    # Fill-in-the-blank — infer from context/cache
                    if q_type == "listen_and_type":
                        human_sleep(0.05, 0.15)
                        executed = handle_listen_and_type(page, result)
                        if executed:
                            feedback = handle_post_answer(page, q_text)
                            if feedback == "no_feedback":
                                consecutive_no_feedback += 1
                            else:
                                consecutive_no_feedback = 0
                                if feedback == "incorrect" and used_cache and q_cache_key:
                                    cache_fail_count[q_cache_key] = cache_fail_count.get(q_cache_key, 0) + 1
                        else:
                            skip_if_stuck(page)
                            click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
                        continue

                    # Refine actions using DOM
                    if q_type in ("multiple_choice", "checkbox"):
                        refine_multiple_choice_actions(page, result)
                    if q_type == "word_bank":
                        refine_word_bank_actions(page, result)

                    # Thinking time
                    num_actions = len(result.get("actions", []))
                    if num_actions <= 1:
                        time.sleep(random.uniform(0.05, 0.15))
                    elif num_actions <= 3:
                        time.sleep(random.uniform(0.1, 0.2))
                    else:
                        time.sleep(random.uniform(0.15, 0.3))

                    # Execute
                    print("  Executing actions...")
                    executed = execute_actions(page, result)
                    if executed:
                        feedback = handle_post_answer(page, q_text)
                        if feedback == "no_feedback":
                            consecutive_no_feedback += 1
                        else:
                            consecutive_no_feedback = 0
                            if feedback == "incorrect" and used_cache and q_cache_key:
                                cache_fail_count[q_cache_key] = cache_fail_count.get(q_cache_key, 0) + 1
                    else:
                        print("  No actions executed, skipping...")
                        skip_if_stuck(page)

                    q_elapsed = time.time() - q_start
                    print(f"  ⏱ Question took {q_elapsed:.1f}s")

                except json.JSONDecodeError as e:
                    print(f"  ⚠ Invalid JSON from AI: {e}")
                    human_sleep(0.1, 0.2)

                except KeyboardInterrupt:
                    print(f"\n🛑 Stopped by user")
                    print(f"  Sessions: {session_count} | Questions: {total_questions + question_count}")
                    print(f"  Total time: {time.time() - farm_start:.0f}s")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                except Exception as e:
                    print(f"  ⚠ Error: {e}")
                    traceback.print_exc()
                    human_sleep(0.1, 0.2)

            # Session ended — check if we should continue
            if MAX_SESSIONS > 0 and session_count >= MAX_SESSIONS:
                print(f"\n🎉 Completed {session_count} practice sessions!")
                break

            # Save session and refresh to start new practice
            context.storage_state(path=SESSION_FILE)
            print(f"\n🔄 Refreshing to start new practice session...")

        elapsed_total = time.time() - farm_start
        print(f"\n{'='*50}")
        print(f"🏁 XP Farm Complete!")
        print(f"  Sessions: {session_count}")
        print(f"  Total questions: {total_questions}")
        print(f"  Total time: {elapsed_total:.0f}s ({elapsed_total/60:.1f}m)")
        print(f"{'='*50}")

        try:
            browser.close()
        except Exception:
            pass


main()
