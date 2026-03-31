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

answer_cache = {}


def normalize_cache_key(text):
    if not text:
        return ""
    s = re.sub(r'_+', '___', text)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ---------------------------------------------------------------------------
# Vision prompt (same exercise types as the main script)
# ---------------------------------------------------------------------------

PROMPT = """You are an AI agent that solves Duolingo exercises automatically.
This is an English course for Vietnamese speakers. The UI may be in Vietnamese.

Look at this Duolingo screenshot and determine:
1. The type of exercise
2. The correct answer
3. The exact action(s) needed to answer

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
            f'A Duolingo exercise shows this sentence with a blank: "{sentence}"',
        ]
        if prefix:
            prompt_parts.append(
                f'The blank already has a pre-filled prefix: "{prefix}". '
                f'The missing word MUST start with "{prefix}".'
            )
        prompt_parts.append(
            'What is the missing word or phrase that fills the blank? '
            'Reply with ONLY the missing word(s), nothing else.'
        )
        try:
            r = client.responses.create(
                model="gpt-4o-mini",
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
        model="gpt-4o",
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": PROMPT},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
            ],
        }],
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
                    skip_texts = {"check", "skip", "continue", "can't listen now",
                                  "use keyboard", "start", "guidebook",
                                  "kiểm tra", "bỏ qua", "tiếp tục"}
                    if not text or text.lower() in skip_texts or len(text) > 20:
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

        GENERIC_TITLES = [
            'nghe và điền', 'nhập từ còn thiếu', 'đọc câu này',
            'chọn cặp từ', 'nghe và tìm từ còn thiếu',
            'tap what you hear', 'type the missing word',
        ]
        is_generic = question_text.lower().strip() in GENERIC_TITLES

        if correct and question_text and not is_generic:
            cache_key = normalize_cache_key(question_text)
            answer_cache[cache_key] = correct
            print(f"  📝 Cached: '{question_text}' → '{correct}'")

        return correct
    except Exception:
        return None


def handle_post_answer(page, question_text=""):
    human_sleep(0.1, 0.2)
    click_button(page, ["Check", "KIỂM TRA", "CHECK", "Kiểm tra"])
    human_sleep(0.15, 0.3)

    banner_visible = False
    try:
        for sel in ['[data-test*="blame-incorrect"]', '[data-test*="blame-correct"]', '[data-test="blame"]']:
            if page.locator(sel).first.is_visible(timeout=800):
                banner_visible = True
                break
    except Exception:
        pass

    if not banner_visible:
        print(f"  ⚠ No feedback banner detected")
        return

    correct_answer = capture_correct_answer(page, question_text)
    if correct_answer:
        print(f"  ❌ Incorrect! Correct: {correct_answer}")
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
        else:
            print(f"  ✅ Correct!")

    click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
    human_sleep(0.15, 0.3)


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
        options_str = "\n".join(f'  {o["index"]}. {o["text"]}' for o in visible_options)
        prompt_parts = [
            f'A Duolingo {q_type} exercise.',
            f'\nQuestion: {question}',
        ]
        if cached:
            prompt_parts.append(f'Known correct answer: {cached}')
        if ai_answer and ai_answer != cached:
            prompt_parts.append(f'AI suggested answer: {ai_answer}')
        prompt_parts.append(f'\nVisible options:\n{options_str}')
        if q_type == "checkbox":
            prompt_parts.append('\nWhich option(s) are correct? Reply with ONLY the number(s) separated by commas.')
        else:
            prompt_parts.append('\nWhich option number is correct? Reply with ONLY the number.')

        r = client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "\n".join(prompt_parts)}],
        )
        picked = r.output_text.strip().replace(" ", "")
        keys = [k.strip() for k in picked.split(",") if k.strip().isdigit()]
        if keys:
            matched_opts = [o for o in visible_options if str(o["index"]) in keys]
            result["actions"] = [make_action(o) for o in matched_opts]
            print(f"  ✅ Picked option(s): {keys}")
    except Exception as e:
        print(f"  ⚠ MC refinement failed: {e}")


def refine_word_bank_actions(page, result):
    tokens = get_all_word_tokens(page)
    if not tokens:
        return

    available_words = [t["display_text"] for t in tokens]
    question = result.get("question", "")
    ai_answer = result.get("answer", "")
    cached = answer_cache.get(normalize_cache_key(question), "")

    print(f"  📋 Word bank tokens: {available_words}")

    if cached:
        cached_words = cached.rstrip('.').strip()
        available_lower = [w.lower() for w in available_words]
        if cached_words.lower() in available_lower:
            idx = available_lower.index(cached_words.lower())
            result["actions"] = [{"action": "click", "target": available_words[idx]}]
            print(f"  ✅ Cached answer matches token: '{available_words[idx]}'")
            return
        cached_split = cached_words.split()
        matched_tokens = []
        remaining = list(available_words)
        all_matched = True
        for word in cached_split:
            found = False
            for rw in remaining:
                if rw.lower() == word.lower():
                    matched_tokens.append(rw)
                    remaining.remove(rw)
                    found = True
                    break
            if not found:
                all_matched = False
                break
        if all_matched and matched_tokens:
            result["actions"] = [{"action": "click", "target": w} for w in matched_tokens]
            print(f"  ✅ Cached answer matched tokens: {matched_tokens}")
            return

    print(f"  🔄 Arranging words using actual word bank...")
    try:
        prompt_parts = [
            'A Duolingo word bank exercise. Click words IN THE CORRECT ORDER.',
            f'\nQuestion: {question}',
        ]
        if cached:
            prompt_parts.append(f'Known correct answer: {cached}')
        if ai_answer and ai_answer != cached:
            prompt_parts.append(f'AI suggested answer: {ai_answer}')
        prompt_parts.append(f'\nAvailable words: {available_words}')
        prompt_parts.append(
            '\nArrange the words in correct order. Not all words need to be used.\n'
            'Reply with ONLY the words separated by " | ".\n'
            'Example: Can | we | check | out'
        )

        r = client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "\n".join(prompt_parts)}],
        )
        ordered = [w.strip() for w in r.output_text.strip().split("|") if w.strip()]

        valid_ordered = []
        remaining = list(available_words)
        for word in ordered:
            if word in remaining:
                valid_ordered.append(word)
                remaining.remove(word)
                continue
            for rw in remaining:
                if rw.lower() == word.lower():
                    valid_ordered.append(rw)
                    remaining.remove(rw)
                    break

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
            consecutive_feedback_skip = 0
            question_count = 0

            while True:
                try:
                    q_start = time.time()
                    human_sleep(0.5, 0.8)

                    # Quick DOM check: detect lesson completion screen before expensive vision call
                    try:
                        body = page.inner_text("body", timeout=1000)
                        completion_keywords = [
                            "LUYỆN TẬP LẠI", "XEM LẠI BÀI HỌC",
                            "Bí mật nè", "TỔNG ĐIỂM KN", "Tổng điểm KN",
                            "Lesson complete", "Practice complete",
                            "Bạn đã chinh phục", "lỗi sai trong bài",
                        ]
                        if any(kw in body for kw in completion_keywords):
                            print(f"\n  🎉 Lesson complete screen detected!")
                            click_button(page, ["TIẾP TỤC", "Tiếp tục", "Continue", "CONTINUE"])
                            human_sleep(0.3, 0.5)
                            total_questions += question_count
                            elapsed = time.time() - farm_start
                            print(f"  ✅ Practice complete! ({question_count} questions)")
                            print(f"  📊 Total: {session_count} sessions, {total_questions} questions, {elapsed:.0f}s")
                            break
                    except Exception:
                        pass

                    # If stuck in feedback loop, force break to new session
                    if consecutive_feedback_skip >= 3:
                        print(f"  ⚠ Stuck in feedback loop ({consecutive_feedback_skip}x), starting new session...")
                        total_questions += question_count
                        break

                    img = page.screenshot(type="jpeg", quality=80)
                    result = analyze_screen(img)

                    q_type = result.get("type", "unknown")
                    question = result.get("question", "")
                    answer = result.get("answer", "")

                    elapsed = time.time() - farm_start
                    print(f"\n⏱ {elapsed:.0f}s | Session #{session_count} | Type: {q_type}")
                    print(f"  Question: {question}")
                    print(f"  Answer: {answer}")

                    if q_type == "no_question":
                        consecutive_no_question += 1
                        print("  No question detected, waiting...")

                        q_lower = question.lower()
                        if any(kw in q_lower for kw in ["log in", "login", "sign in"]):
                            print("  ⚠ Login screen detected!")
                            if not do_login():
                                raise Exception("Re-login failed")
                            context.storage_state(path=SESSION_FILE)
                            consecutive_no_question = 0
                            continue

                        found_continue = click_button(
                            page,
                            ["TIẾP TỤC", "Tiếp tục", "Continue", "CONTINUE", "Next",
                             "BẮT ĐẦU", "Bắt đầu", "START", "Start"],
                        )

                        if not found_continue:
                            current_url = page.url
                            not_in_lesson = "/lesson" not in current_url

                            if not_in_lesson and question_count > 0:
                                print(f"  ✅ Practice complete! ({question_count} questions)")
                                total_questions += question_count
                                print(f"  📊 Total: {session_count} sessions, {total_questions} questions, {elapsed:.0f}s")
                                break

                            if consecutive_no_question >= 5 and question_count > 0:
                                print(f"  ✅ Practice complete! ({question_count} questions)")
                                total_questions += question_count
                                print(f"  📊 Total: {session_count} sessions, {total_questions} questions, {elapsed:.0f}s")
                                break

                            if consecutive_no_question >= 8 and question_count == 0:
                                print("  ⚠ Stuck: no questions detected.")
                                break

                        human_sleep(0.1, 0.2)
                        continue

                    consecutive_no_question = 0
                    question_count += 1

                    # Check if feedback banner is showing (leftover from previous answer)
                    continue_ready = False
                    try:
                        for sel in ['[data-test*="blame-incorrect"]', '[data-test*="blame-correct"]', '[data-test="blame"]']:
                            if page.locator(sel).first.is_visible(timeout=200):
                                for cont_text in ["TIẾP TỤC", "Tiếp tục", "Continue", "CONTINUE"]:
                                    try:
                                        cont_btn = page.locator(f'button:has-text("{cont_text}")').first
                                        if cont_btn.is_visible(timeout=300) and cont_btn.is_enabled(timeout=300):
                                            continue_ready = True
                                            print(f"  ⏩ Feedback visible, clicking '{cont_text}'...")
                                            cont_btn.click(timeout=1000)
                                            human_sleep(0.1, 0.2)
                                            consecutive_feedback_skip += 1
                                            break
                                    except Exception:
                                        continue
                                break
                    except Exception:
                        pass
                    if continue_ready:
                        continue

                    consecutive_feedback_skip = 0

                    q_text = result.get("question", "")

                    # Check answer cache
                    q_cache_key = normalize_cache_key(q_text) if q_text else ""
                    if q_cache_key and q_cache_key in answer_cache:
                        cached = answer_cache[q_cache_key]
                        print(f"  💾 Found cached answer: '{cached}'")
                        result["answer"] = cached
                        if q_type == "typing":
                            result["actions"] = [{"action": "type", "target": "input", "value": cached}]
                        elif q_type in ("multiple_choice", "image_choice"):
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
                            words = cached.split()
                            result["actions"] = [{"action": "click", "target": w} for w in words]

                    # Skip audio-only exercises (no audio in practice mode)
                    if q_type in ("audio_matching", "audio_fill_blank", "listening", "speaking"):
                        print(f"  🔇 Skipping {q_type} exercise (no audio)")
                        skip_if_stuck(page)
                        human_sleep(0.1, 0.2)
                        click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
                        human_sleep(0.1, 0.2)
                        continue

                    # Fill-in-the-blank — infer from context/cache
                    if q_type == "listen_and_type":
                        human_sleep(0.05, 0.15)
                        executed = handle_listen_and_type(page, result)
                        if executed:
                            handle_post_answer(page, q_text)
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
                        handle_post_answer(page, q_text)
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
