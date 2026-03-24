import base64
import json
import os
import random
import re
import tempfile
import time
import traceback
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from openai import OpenAI

try:
    import sounddevice as sd
    import soundfile as sf
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False
    print("⚠ sounddevice/soundfile not installed. Listening exercises will be skipped.")
    print("  Install with: pip install sounddevice soundfile")

load_dotenv()

if os.getenv("NO_AUDIO", "").lower() in ("true", "1", "yes"):
    HAS_AUDIO = False
    print("⚠ Audio disabled via NO_AUDIO env var. Listening exercises will be skipped.")

EMAIL = os.getenv("DUO_EMAIL")
PASSWORD = os.getenv("DUO_PASSWORD")
DUO_JWT = os.getenv("DUO_JWT")  # Optional: pre-authenticated JWT token

SESSION_FILE = "en_duo_session.json"

# Chance of deliberately answering wrong (0.0 = disabled)
WRONG_ANSWER_CHANCE = 0.0
MAX_WRONG_PER_LESSON = 0
# Max lessons to complete (0 = unlimited). Set via env MAX_LESSONS.
MAX_LESSONS = int(os.getenv("MAX_LESSONS", "0"))

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT = """You are an AI agent that solves Duolingo exercises automatically.

Look at this Duolingo screenshot and determine:
1. The type of exercise
2. The correct answer
3. The exact action(s) needed to answer
4. ALL available options (for deliberate wrong answers)

Respond ONLY with valid JSON (no markdown, no explanation) using this format:

{
  "type": "image_choice | multiple_choice | word_bank | typing | matching | listening | tap_pairs | tracing | no_question",
  "question": "brief description of the question",
  "answer": "the correct answer",
  "all_options": ["option1", "option2", "option3"],
  "total_options": 3,
  "actions": [
    {"action": "click", "target": "exact text of the button/word to click"},
    {"action": "type", "target": "selector description", "value": "text to type"},
    {"action": "press", "key": "1"}
  ]
}

Exercise types and how to answer:

- image_choice: Cards with images and labels, each card has a number shortcut (1, 2, 3...).
  Press the number key of the correct card.
  actions = [{"action": "press", "key": "2"}]
  all_options = ["咖啡", "豆腐", "粥"], total_options = 3

- multiple_choice: Text options to click. actions = [{"action": "click", "target": "exact option text"}]
  If options have number shortcuts (1, 2, 3...), prefer: actions = [{"action": "press", "key": "1"}]
  all_options = list of all option texts, total_options = number of options

- word_bank: Click words in the correct order from the word bank.
  actions = [{"action": "click", "target": "word1"}, {"action": "click", "target": "word2"}, ...]
  all_options = list of all available words in the bank, total_options = number of words
  IMPORTANT: "question" must contain the FULL visible sentence/text being translated or completed (not just "translate this").
  For example: question = "Translate: She likes coffee" or "The ___ is on the table."

- typing: Type the answer in the text field.
  actions = [{"action": "type", "target": "input", "value": "the answer"}]
  all_options = [], total_options = 0

- matching / tap_pairs: "Select the matching pairs" - cards with number shortcuts (1-5 left, 6-0 right).
  Look at the number shown on each card. Press the number for the left item, then the number for its right match.
  actions = [{"action": "press", "key": "1"}, {"action": "press", "key": "9"}, {"action": "press", "key": "2"}, {"action": "press", "key": "7"}, ...]
  Pair format: left_number, right_number, left_number, right_number, ...
  all_options = ["1:This is", "2:tea", "3:tofu", "6:这是", "7:粥", "8:茶", "9:豆腐"], total_options = number of cards

- listening: "Tap what you hear" - has a speaker button and a word bank below.
  DO NOT guess the answer. Just return the visible word bank options.
  actions = [] (will be handled by audio recognition)
  all_options = list ALL visible words/chips in the word bank (e.g. ["粥", "水", "这是", "豆腐", "米饭", "和"])
  total_options = number of words in the bank

- tracing: "Trace the character" — draw/trace a Chinese character by following the blue dashed line.
  Cannot be automated. actions = []
  all_options = [], total_options = 0

- no_question: No exercise visible (loading, result screen, etc). actions = []
  all_options = [], total_options = 0

IMPORTANT:
- For image_choice: look for number labels (1, 2, 3) on each card, use "press" with that number
- For word_bank: each "target" must be the Chinese characters (汉字) of the word, NOT the pinyin.
  Example: use "粥" not "zhōu", use "这是" not "zhèshì", use "豆腐" not "dòu fu"
- For multiple_choice: "target" must be the Chinese characters (汉字) of the option, or use "press" if number shortcuts are visible
- For typing: "value" is the full answer to type
- all_options should list the Chinese characters (汉字) of all visible options
- Be precise with the text - it must match what's on screen exactly
- Always fill all_options with ALL visible options and total_options with the count
"""


def human_sleep(min_s=0.3, max_s=1.0):
    """Sleep for a random duration to mimic human behavior."""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


def get_loopback_device():
    """Find a WASAPI loopback device for recording system audio on Windows."""
    if not HAS_AUDIO:
        return None

    try:
        devices = sd.query_devices()
        # Look for WASAPI loopback devices (Windows)
        for i, dev in enumerate(devices):
            name = dev["name"].lower()
            if dev["max_input_channels"] > 0 and (
                "loopback" in name
                or "stereo mix" in name
                or "what u hear" in name
                or "wave out" in name
            ):
                print(f"  Found loopback device: [{i}] {dev['name']}")
                return i

        # Fallback: try to find default output and use its loopback
        # On Windows with sounddevice, we can use wasapi loopback
        host_apis = sd.query_hostapis()
        for api in host_apis:
            if "wasapi" in api["name"].lower():
                default_output = api.get("default_output_device")
                if default_output is not None:
                    print(f"  Using WASAPI default output as loopback: [{default_output}]")
                    return default_output
    except Exception as e:
        print(f"  ⚠ Error finding loopback device: {e}")

    return None


def start_recording(duration=5.0, sample_rate=16000):
    """Start recording system audio (non-blocking). Returns (audio_data, device_info)."""
    if not HAS_AUDIO:
        return None

    try:
        device = get_loopback_device()

        # Start recording (non-blocking - sd.rec returns immediately)
        try:
            wasapi_loopback = sd.WasapiSettings(loopback=True)
            print(f"  🎙 Recording started ({duration}s) via WASAPI loopback...")
            audio_data = sd.rec(
                int(duration * sample_rate),
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                extra_settings=wasapi_loopback,
            )
            return (audio_data, sample_rate)
        except Exception:
            if device is not None:
                print(f"  🎙 Recording started ({duration}s) via device {device}...")
                audio_data = sd.rec(
                    int(duration * sample_rate),
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                    device=device,
                )
                return (audio_data, sample_rate)

        print("  ⚠ No loopback device available")
        return None

    except Exception as e:
        print(f"  ⚠ Audio recording start failed: {e}")
        return None


def finish_recording(recording_info):
    """Wait for recording to finish and save to temp WAV file."""
    if not recording_info:
        return None

    try:
        audio_data, sample_rate = recording_info
        sd.wait()  # Block until recording is done
        print("  🎙 Recording finished")

        tmp_path = os.path.join(tempfile.gettempdir(), "duo_listen.wav")
        sf.write(tmp_path, audio_data, sample_rate)
        return tmp_path

    except Exception as e:
        print(f"  ⚠ Audio recording failed: {e}")
        return None


def transcribe_audio(audio_path):
    """Transcribe audio file using OpenAI Whisper API."""
    try:
        print("  🗣 Transcribing with Whisper...")
        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="zh",  # Chinese for Duolingo Chinese course
            )
        text = result.text.strip()
        print(f"  Whisper result: '{text}'")
        return text
    except Exception as e:
        print(f"  ⚠ Whisper transcription failed: {e}")
        return None


def click_speaker(page):
    """Click the speaker/play button on a listening exercise."""
    selectors = [
        '[data-test="speaker-button"]',
        'button[aria-label*="speaker"]',
        'button[aria-label*="play"]',
        'button[aria-label*="audio"]',
    ]
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=500)
            return True
        except Exception:
            continue
    # Fallback: click the large blue speaker icon by its visual position
    try:
        page.locator("button").filter(has=page.locator("svg")).first.click(timeout=500)
        return True
    except Exception:
        return False


def handle_listening(page, result):
    """Handle listening exercises: start recording → play audio → Whisper → click word bank."""

    all_options = result.get("all_options", [])

    if not HAS_AUDIO:
        print("  ⚠ No audio support, skipping...")
        skip_if_stuck(page)
        return False

    # Step 1: START recording FIRST (non-blocking)
    recording = start_recording(duration=5.0)
    if not recording:
        print("  ⚠ Could not start recording, skipping...")
        skip_if_stuck(page)
        return False

    # Step 2: THEN click speaker button to play audio (while recording)
    time.sleep(0.3)  # small buffer before playing
    print("  🔊 Playing audio...")
    click_speaker(page)

    # Step 3: Wait for recording to finish
    audio_path = finish_recording(recording)
    if not audio_path:
        print("  ⚠ Could not record audio, skipping...")
        skip_if_stuck(page)
        return False

    # Step 3: Transcribe with Whisper
    transcript = transcribe_audio(audio_path)
    if not transcript:
        print("  ⚠ Transcription failed, skipping...")
        skip_if_stuck(page)
        return False

    # Step 4: Read actual word bank tokens from DOM
    tokens = get_all_word_tokens(page)
    if tokens:
        available_words = [t.get("display_text") or t.get("hanzi") or t.get("full_text", "") for t in tokens]
    else:
        available_words = all_options

    print(f"  Transcript: '{transcript}'")
    print(f"  Word bank: {available_words}")

    words_to_click = None
    try:
        r = client.responses.create(
            model="gpt-4o-mini",
            input=[{
                "role": "user",
                "content": (
                    f'A Duolingo listening exercise (Chinese for English speakers). '
                    f'The spoken sentence is:\n"{transcript}"\n\n'
                    f'Available words in the word bank (ONLY use these exact words): {available_words}\n\n'
                    f'Select and arrange words from the bank to form the sentence you heard.\n'
                    f'Chinese characters may have pinyin variants in the bank.\n'
                    f'Numbers in speech may appear as words (e.g. "12" → "twelve").\n'
                    f'Not all words need to be used. Use each word at most once.\n'
                    f'Reply with ONLY the words separated by " | " (pipe), nothing else.'
                ),
            }],
        )
        ordered = [w.strip() for w in r.output_text.strip().split("|") if w.strip()]

        valid = []
        remaining = list(available_words)
        for word in ordered:
            if word in remaining:
                valid.append(word)
                remaining.remove(word)
                continue
            for rw in remaining:
                if rw.lower() == word.lower():
                    valid.append(rw)
                    remaining.remove(rw)
                    break
        if valid:
            words_to_click = valid
            print(f"  GPT word order: {words_to_click}")
    except Exception as e:
        print(f"  ⚠ GPT ordering failed: {e}")

    # Fallback to simple matching if GPT failed
    if not words_to_click:
        words_to_click = match_words_to_transcript(transcript, available_words)

    if not words_to_click:
        print("  ⚠ Could not match words, skipping...")
        skip_if_stuck(page)
        return False

    print(f"  Click order: {words_to_click}")
    for word in words_to_click:
        human_sleep(0.3, 0.8)
        click_target(page, word)

    # Clean up temp file
    try:
        os.remove(audio_path)
    except Exception:
        pass

    return True


def match_words_to_transcript(transcript, options):
    """Match word bank options to the transcript in correct order."""
    if not options or not transcript:
        return []

    # Normalize transcript (remove punctuation, lowercase for comparison)
    clean = transcript.replace("。", "").replace("，", "").replace(",", "").replace(".", "").strip()

    # Try to find a valid ordering of options that forms the transcript
    # Greedy approach: scan transcript left to right, match longest option first
    remaining = clean
    result = []
    available = list(options)

    while remaining and available:
        matched = False
        # Try longest options first
        sorted_opts = sorted(available, key=len, reverse=True)
        for opt in sorted_opts:
            clean_opt = opt.strip()
            if remaining.startswith(clean_opt):
                result.append(opt)
                remaining = remaining[len(clean_opt):]
                available.remove(opt)
                matched = True
                break
        if not matched:
            # Skip one character (might be whitespace or punctuation)
            remaining = remaining[1:]

    return result


def analyze_screen(img):
    b64 = base64.b64encode(img).decode()

    r = client.responses.create(
        model="gpt-4o",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": PROMPT},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{b64}",
                    },
                ],
            }
        ],
    )

    raw = r.output_text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    return json.loads(raw)


def should_answer_wrong():
    """Randomly decide if we should deliberately answer wrong."""
    return random.random() < WRONG_ANSWER_CHANCE


def make_wrong_actions(result):
    """Generate wrong actions based on the exercise type."""

    q_type = result.get("type", "")
    all_options = result.get("all_options", [])
    total_options = result.get("total_options", 0)
    correct_answer = result.get("answer", "")

    if q_type in ("image_choice", "multiple_choice") and total_options >= 2:
        # Pick a random wrong option number
        correct_key = None
        for act in result.get("actions", []):
            if act.get("action") == "press":
                correct_key = act.get("key")
                break

        if correct_key and total_options >= 2:
            wrong_keys = [str(i) for i in range(1, total_options + 1) if str(i) != correct_key]
            wrong_key = random.choice(wrong_keys)
            return [{"action": "press", "key": wrong_key}]

        # For click-based multiple choice
        wrong_options = [opt for opt in all_options if opt != correct_answer]
        if wrong_options:
            wrong = random.choice(wrong_options)
            return [{"action": "click", "target": wrong}]

    elif q_type == "word_bank" and all_options:
        # Shuffle word order (wrong sentence)
        correct_actions = result.get("actions", [])
        if len(correct_actions) >= 2:
            shuffled = correct_actions[:]
            random.shuffle(shuffled)
            # Make sure it's actually different
            if shuffled != correct_actions:
                return shuffled
            # If shuffle gave same order, swap first two
            shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
            return shuffled

    elif q_type == "typing":
        # Introduce a typo
        if len(correct_answer) > 2:
            pos = random.randint(0, len(correct_answer) - 1)
            typo = correct_answer[:pos] + correct_answer[pos + 1:]  # delete a char
            return [{"action": "type", "target": "input", "value": typo}]

    return None  # Can't make wrong answer, just answer correctly


def refine_word_bank_actions(page, result):
    """For word_bank exercises, read actual DOM tokens and ask GPT to arrange them.
    Always runs to ensure both correct words AND correct order.
    """
    tokens = get_all_word_tokens(page)
    if not tokens:
        return

    available_words = [t.get("display_text") or t.get("hanzi") or t.get("full_text", "") for t in tokens]
    question = result.get("question", "")
    ai_answer = result.get("answer", "")

    print(f"  📋 Word bank tokens: {available_words}")

    print(f"  🔄 Arranging words using actual word bank...")
    try:
        prompt_parts = [
            'A Duolingo word bank exercise. You must click words from the word bank IN THE CORRECT ORDER '
            'to form a sentence or fill in blanks.',
            f'\nQuestion/sentence shown on screen: {question}',
        ]
        if ai_answer:
            prompt_parts.append(f'AI suggested answer: {ai_answer}')
        prompt_parts.append(
            f'\nAvailable words in the word bank (you can ONLY use these exact words): {available_words}'
        )
        prompt_parts.append(
            '\nArrange the words in the correct order. Not all words need to be used. '
            'Use each word at most once.\n'
            'CRITICAL RULES:\n'
            '- The order matters — each word fills the next blank in the sentence.\n'
            '- Words may be SPLIT across multiple tokens. For example, "o\'clock" might be two separate '
            'tokens: "o" and "\'clock". You MUST include ALL parts.\n'
            '- Chinese characters with pinyin may be separate tokens.\n'
            '- Compare your answer with the available tokens — every token you use must EXACTLY match '
            'one item in the available list.\n'
            'Reply with ONLY the words separated by " | " (pipe), nothing else.\n'
            'Example: word1 | word2 | word3'
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
        else:
            print(f"  ⚠ Refinement failed, keeping original actions")
    except Exception as e:
        print(f"  ⚠ Word bank refinement failed: {e}")


def execute_actions(page, result, force_wrong=False):
    """Execute the AI-determined actions on the page."""

    actions = result.get("actions", [])

    if force_wrong:
        wrong = make_wrong_actions(result)
        if wrong:
            actions = wrong
            print("  🎭 Deliberately answering WRONG (human simulation)")

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
            if is_matching and i % 2 == 0:
                human_sleep(0.3, 0.8)
            else:
                human_sleep(0.5, 1.2)

        if action == "press":
            print(f"  [{i+1}] Pressing key: '{key}'")
            page.keyboard.press(key)

        elif action == "click":
            print(f"  [{i+1}] Clicking: '{target}'")
            click_target(page, target, q_type=q_type)
            time.sleep(0.3)

        elif action == "type":
            print(f"  [{i+1}] Typing: '{value}'")
            type_answer(page, value)

    return True


def extract_hanzi_pinyin(text):
    """Extract Chinese characters (hanzi) and pinyin from mixed text.
    Examples:
        '汤tāng' → ('汤', 'tāng')
        'dòu fu\\n豆腐' → ('豆腐', 'dòu fu')
        'tāng\\n汤' → ('汤', 'tāng')
        '豆腐' → ('豆腐', '')
        'This' → ('This', '')
    """
    # First try splitting by newlines
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) >= 2:
        # Check which line has Chinese chars
        for line in lines:
            if re.search(r'[\u4e00-\u9fff]', line) and not re.search(r'[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]', line):
                hanzi = line
                pinyin = " ".join(l.strip() for l in lines if l.strip() != line)
                return hanzi, pinyin
        # If mixed, last line is usually hanzi
        return lines[-1], lines[0]

    # Single line - might be mixed like "汤tāng" or "dòu fu豆腐"
    # Extract Chinese characters
    hanzi_chars = re.findall(r'[\u4e00-\u9fff]+', text)
    if hanzi_chars:
        hanzi = "".join(hanzi_chars)
        # Everything else is pinyin
        pinyin = re.sub(r'[\u4e00-\u9fff]+', '', text).strip()
        return hanzi, pinyin

    # No Chinese characters (English text)
    return text, ""


def get_all_word_tokens(page):
    """Get all visible word bank tokens with their text content and elements.
    Returns list of dicts with full_text, hanzi, pinyin, locator.
    """
    tokens = []
    # Duolingo word bank tokens - try many selectors
    token_selectors = [
        '[data-test="challenge-tap-token"]',
        '[data-test="word-bank"] button',
        '[data-test="challenge-tap-token-text"]',
        'button[data-test*="tap-token"]',
        # Broader fallbacks for listening exercises
        '[class*="WordBank"] button',
        '[class*="wordBank"] button',
        '[class*="word-bank"] button',
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
                    hanzi, pinyin = extract_hanzi_pinyin(full_text)

                    tokens.append({
                        "full_text": full_text,
                        "hanzi": hanzi,
                        "pinyin": pinyin,
                        "locator": loc,
                    })
                except Exception:
                    continue

            if tokens:
                # print(f"    Found {len(tokens)} tokens via '{sel}': {[t['hanzi'] for t in tokens]}")
                return tokens
        except Exception:
            continue

    # Last resort: find ALL small buttons in the middle/bottom area of the page
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
                    # Filter: skip known UI buttons, keep short text (word tokens)
                    skip_texts = {"check", "skip", "continue", "can't listen now",
                                  "use keyboard", "start", "guidebook"}
                    if not text or text.lower() in skip_texts or len(text) > 20:
                        continue
                    hanzi, pinyin = extract_hanzi_pinyin(text)
                    tokens.append({
                        "full_text": text,
                        "hanzi": hanzi,
                        "pinyin": pinyin,
                        "locator": btn,
                    })
                except Exception:
                    continue
            if tokens:
                pass
        except Exception:
            pass

    return tokens


def click_word_token(page, text):
    """Click a word bank token matching the given text (handles pinyin+hanzi).
    Re-queries tokens every time because DOM changes after each click.
    """
    # Always re-query tokens (DOM changes after every click)
    tokens = get_all_word_tokens(page)

    if tokens:
        # Try matching: hanzi exact → pinyin exact
        for token in tokens:
            if token["hanzi"] == text or token["pinyin"] == text:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue

        # Case-insensitive match (for English words like "This" vs "this")
        text_lower = text.lower()
        for token in tokens:
            if token["hanzi"].lower() == text_lower:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue

        # Partial match: text contained in hanzi or vice versa
        for token in tokens:
            if text in token["hanzi"] or token["hanzi"] in text:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue

        # Match full_text contains target
        for token in tokens:
            if text.lower() in token["full_text"].lower():
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue

    # Fallback to generic click
    return click_target_generic(page, text)


def click_target(page, text, q_type=""):
    """Click an element matching the given text. Smart matching for Chinese (hanzi+pinyin)."""

    # For multiple_choice/checkbox, target only challenge option area
    if q_type in ("multiple_choice", "image_choice"):
        return click_challenge_option(page, text)

    # First try the smart word token matching (handles pinyin+hanzi)
    if click_word_token(page, text):
        return True

    return click_target_generic(page, text)


def click_challenge_option(page, text):
    """Click a challenge option precisely, with key press fallback."""
    TIMEOUT = 2000

    selectors = [
        f'[data-test="challenge-choice"]:has-text("{text}")',
        f'[data-test="challenge-judge-text"]:has-text("{text}")',
        f'div[role="radio"]:has-text("{text}")',
        f'label:has-text("{text}")',
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=TIMEOUT)
            return True
        except Exception:
            continue

    # Fallback: find option by text match and try clicking or pressing number key
    try:
        choices = page.locator('[data-test="challenge-choice"]')
        count = choices.count()
        for i in range(count):
            choice = choices.nth(i)
            choice_text = choice.inner_text(timeout=500).strip()
            if text.lower() in choice_text.lower():
                try:
                    choice.click(timeout=TIMEOUT)
                    return True
                except Exception:
                    key = str(i + 1)
                    print(f"  Click failed, pressing key '{key}' for option '{text}'")
                    page.keyboard.press(key)
                    return True
    except Exception:
        pass

    # Last resort: try pressing number keys
    try:
        choices = page.locator('[data-test="challenge-choice"]')
        count = choices.count()
        for i in range(count):
            choice_text = choices.nth(i).inner_text(timeout=300).strip()
            if text.lower() in choice_text.lower():
                key = str(i + 1)
                print(f"  Pressing key '{key}' for option '{text}'")
                page.keyboard.press(key)
                return True
    except Exception:
        pass

    # Fallback: try word token matching (some exercises use word bank buttons)
    if click_word_token(page, text):
        return True

    return click_target_generic(page, text)


def click_target_generic(page, text):
    """Generic click fallback using CSS selectors."""

    TIMEOUT = 300

    selectors = [
        f'[data-test="challenge-choice"]:has-text("{text}")',
        f'button:has-text("{text}")',
        f'div[role="button"]:has-text("{text}")',
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=TIMEOUT)
            return True
        except Exception:
            continue

    # Fallback: get_by_text
    for exact in [True, False]:
        try:
            loc = page.get_by_text(text, exact=exact).first
            loc.click(timeout=TIMEOUT)
            return True
        except Exception:
            continue

    print(f"  ⚠ Could not find: '{text}'")
    return False


def type_answer(page, text):
    """Type answer into the input field."""

    selectors = [
        '[data-test="challenge-text-input"]',
        'input[type="text"]',
        'textarea',
        '[contenteditable="true"]',
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.click(timeout=300)
            # Check if there's pre-filled text and only type the remaining part
            try:
                existing = loc.input_value(timeout=300)
            except Exception:
                existing = loc.inner_text(timeout=300) or ""
            to_type = text
            if existing and text.lower().startswith(existing.lower()):
                to_type = text[len(existing):]
                if to_type:
                    print(f"    Pre-filled: '{existing}', typing remaining: '{to_type}'")
                else:
                    print(f"    Already filled: '{existing}'")
                    return True
            elif existing:
                # Pre-filled text doesn't match answer, clear and retype
                loc.fill("")
            for char in to_type:
                loc.type(char, delay=random.randint(30, 120))
            return True
        except Exception:
            continue

    # Fallback: press keys directly
    try:
        page.keyboard.type(text, delay=random.randint(40, 100))
        return True
    except Exception:
        print(f"  ⚠ Could not type answer")
        return False


def click_button(page, texts):
    """Try to click a button matching any of the given texts."""
    for text in texts:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            btn.click(timeout=500)
            return True
        except Exception:
            continue
    return False


def check_answer_feedback(page):
    """Detect if the answer was correct or incorrect after clicking Check.
    Logs the result and returns (is_correct, correct_answer_or_None).
    """
    try:
        # First verify a feedback banner actually appeared
        banner_visible = False
        for sel in ['[data-test*="blame-incorrect"]', '[data-test*="blame-correct"]', '[data-test="blame"]']:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1000):
                    banner_visible = True
                    break
            except Exception:
                continue

        if not banner_visible:
            print(f"  ⚠ No feedback banner detected (Check button may not have been clicked)")
            return True, None

        # Check for incorrect banner
        try:
            el = page.locator('[data-test*="blame-incorrect"]').first
            if el.is_visible(timeout=500):
                feedback = el.inner_text(timeout=500).strip()
                correct_answer = None
                for keyword in ['Correct solution:', 'Correct answer:']:
                    if keyword in feedback:
                        after = feedback.split(keyword, 1)[1].strip()
                        lines = after.split('\n')
                        if lines:
                            correct_answer = lines[0].strip()
                            for noise in ['REPORT', 'CONTINUE']:
                                correct_answer = correct_answer.replace(noise, '').strip()
                        break
                if correct_answer:
                    print(f"  ❌ Incorrect! Correct answer: {correct_answer}")
                else:
                    print(f"  ❌ Incorrect!")
                return False, correct_answer
        except Exception:
            pass

        # Fallback: check body text for incorrect keywords
        try:
            body = page.inner_text("body", timeout=500)
            for keyword in ['Correct solution:', 'Correct answer:']:
                if keyword in body:
                    after = body.split(keyword, 1)[1].strip().split('\n')[0].strip()
                    for noise in ['REPORT', 'CONTINUE']:
                        after = after.replace(noise, '').strip()
                    if after:
                        print(f"  ❌ Incorrect! Correct answer: {after}")
                        return False, after
                    print(f"  ❌ Incorrect!")
                    return False, None
        except Exception:
            pass

        print(f"  ✅ Correct!")
        return True, None
    except Exception:
        return True, None


def handle_post_answer(page):
    """Click Check, Continue, or Next after answering."""

    human_sleep(0.5, 1.5)

    click_button(page, ["Check", "CHECK"])
    human_sleep(0.3, 0.8)

    check_answer_feedback(page)

    click_button(page, ["Continue", "CONTINUE"])
    human_sleep(0.5, 1.5)


def skip_if_stuck(page):
    """Click Skip if available (for listening exercises etc)."""
    try:
        click_button(page, ["Skip", "SKIP", "CAN'T LISTEN NOW"])
        return True
    except Exception:
        return False


def click_start_xp_button(page):
    """Click the 'START +XX XP' button in the lesson popup."""
    import re

    # Try multiple approaches to find the XP start button
    attempts = [
        # 1. Regex match on any element containing "START" and "XP"
        lambda: page.get_by_text(re.compile(r"START\s*\+\s*\d+\s*XP", re.IGNORECASE)).first,
        # 2. Any element with text containing "START +"
        lambda: page.get_by_text(re.compile(r"START\s*\+", re.IGNORECASE)).first,
        # 3. Button with has-text
        lambda: page.locator('button:has-text("START +")').first,
        # 4. Any clickable element (a, button, div[role=button]) with XP text
        lambda: page.locator('a:has-text("START +")').first,
        lambda: page.locator('div:has-text("START +")').last,
        # 5. data-test attribute for start button
        lambda: page.locator('[data-test="start-button"]').first,
    ]

    for attempt in attempts:
        try:
            el = attempt()
            el.click(timeout=3000)
            print(f"  Clicked 'START +XP' button")
            human_sleep(0.5, 1.5)
            return True
        except Exception:
            continue

    print("  ⚠ Could not find 'START +XP' button, trying keyboard Enter...")
    try:
        page.keyboard.press("Enter")
        human_sleep(0.5, 1.5)
        return True
    except Exception:
        return False


def get_hearts(page):
    """Get current heart count from the top-right corner. Returns int or -1 if can't detect."""
    try:
        # Hearts shown near the heart icon in top bar, look for the img with hearts
        # The heart count is typically in a span/div near the heart icon
        heart_loc = page.locator('[href="/hearts"] span, [data-test="hearts"] span, a[href*="heart"] span').first
        text = heart_loc.inner_text(timeout=2000).strip()
        return int(text)
    except Exception:
        pass
    # Fallback: screenshot top-right and parse with regex from page text
    try:
        body = page.inner_text("body", timeout=2000)
        # Look for heart icon followed by a number (usually "♥ 5" or just "5" near hearts)
        import re as _re
        # On the learn page, hearts show as a number near top-right
        # Try to find it via the specific heart element
        heart_el = page.locator('img[src*="heart"], svg[class*="heart"], [class*="heart"]').first
        sibling = heart_el.locator("..").inner_text(timeout=1000).strip()
        nums = _re.findall(r'\d+', sibling)
        if nums:
            return int(nums[-1])
    except Exception:
        pass
    return -1  # Can't detect


def check_no_hearts(page):
    """Check if the 'You need hearts' popup is showing. Returns True if out of hearts."""
    try:
        body_text = page.inner_text("body", timeout=1000)
        if "You need hearts" in body_text or "need hearts to start" in body_text:
            return True
    except Exception:
        pass
    return False


def start_practice_mode(page):
    """Navigate to target practice mode and click Continue to start."""
    print("  🏋️ Starting target practice mode...")
    page.goto("https://www.duolingo.com/practice-hub/target-practice")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    click_button(page, ["Continue", "CONTINUE", "Start", "START", "PRACTICE", "Practice"])
    human_sleep(0.5, 1.0)
    return True


def start_lesson(page):
    """Start a target practice lesson."""
    print("Looking for a lesson to start...")
    start_practice_mode(page)
    human_sleep(0.5, 1.0)

    # Click START / START +XP button if visible
    if click_start_xp_button(page):
        return True

    popup_texts = ["START", "Start", "START LESSON", "CONTINUE", "Continue",
                   "PRACTICE"]
    for text in popup_texts:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            btn.click(timeout=1000)
            print(f"  Started lesson via: '{text}'")
            human_sleep(0.5, 1.5)
            return True
        except Exception:
            continue

    return True


_profile_raw = os.getenv("DUO_PROFILE_URL", "")
PROFILE_URL = (
    _profile_raw if _profile_raw.startswith("http")
    else f"https://www.duolingo.com/profile/{_profile_raw}" if _profile_raw
    else ""
)


def get_xp(page):
    """Get current user XP by navigating to profile page and reading stats."""
    try:
        # Find profile URL from the page if not set
        profile_url = PROFILE_URL
        if not profile_url:
            # Try clicking profile link to find username
            try:
                profile_link = page.locator('a[href*="/profile/"]').first
                profile_url = "https://www.duolingo.com" + profile_link.get_attribute("href", timeout=2000)
            except Exception:
                # Fallback: navigate to profile via sidebar
                profile_url = "https://www.duolingo.com/profile"

        # Save current URL to return later
        current_url = page.url

        # Cache-bust: append timestamp to force fresh data
        cache_bust = f"?cb={int(time.time())}"
        page.goto(profile_url + cache_bust, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Scrape stats from profile page
        xp = 0
        streak = 0

        page_text = page.inner_text("body")

        # Find XP: prioritize "Total XP" / "Tổng điểm KN" label (not achievement text)
        xp_found = False

        # 1. English: number right before "Total XP"
        total_xp_match = re.search(r'([\d,]+)[\s\n]+Total XP', page_text)
        if total_xp_match:
            xp = int(total_xp_match.group(1).replace(",", ""))
            print(f"    XP from 'Total XP': {xp}")
            xp_found = True

        # 2. Vietnamese: number right before "Tổng điểm KN"
        if not xp_found:
            vi_match = re.search(r'([\d,]+)[\s\n]+Tổng điểm KN', page_text)
            if vi_match:
                xp = int(vi_match.group(1).replace(",", ""))
                print(f"    XP from 'Tổng điểm KN': {xp}")
                xp_found = True

        # 3. Vietnamese fallback: search nearby text
        if not xp_found and 'Tổng điểm KN' in page_text:
            idx = page_text.index('Tổng điểm KN')
            nearby = page_text[max(0, idx - 50):idx]
            nums = re.findall(r'([\d,]+)', nearby)
            if nums:
                xp = int(nums[-1].replace(",", ""))
                print(f"    XP from nearby 'Tổng điểm KN': {xp}")
                xp_found = True

        if not xp_found:
            print(f"    ⚠ Could not find XP on profile page")

        # Match streak: "1 day streak" (English) or "NNN\nNgày streak" (Vietnamese)
        streak_match = re.search(r'(\d+)\s*day\s*streak', page_text, re.IGNORECASE)
        if not streak_match:
            streak_match = re.search(r'(\d+)\s*\n?\s*Ngày streak', page_text, re.IGNORECASE)
        if streak_match:
            streak = int(streak_match.group(1))

        # Go back to previous page
        page.goto(current_url)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)

        return {"totalXp": xp, "streak": streak}

    except Exception as e:
        print(f"  ⚠ Could not fetch XP: {e}")
        return None


def print_xp_summary(label, xp_data):
    """Print XP summary."""
    if not xp_data:
        print(f"  {label}: Could not retrieve XP data")
        return
    print(f"  {label}:")
    print(f"    Total XP: {xp_data['totalXp']}")
    print(f"    Streak: {xp_data['streak']} days")


def login_with_jwt(context, page):
    """Login by injecting a pre-authenticated JWT token directly (best for CI)."""
    if not DUO_JWT:
        return False
    print("  Using DUO_JWT token for authentication...")
    context.add_cookies([{
        "name": "jwt_token",
        "value": DUO_JWT,
        "domain": ".duolingo.com",
        "path": "/",
    }])
    page.goto("https://www.duolingo.com/learn")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)
    if "/learn" in page.url:
        print(f"  JWT login successful! URL: {page.url}")
        return True
    print(f"  JWT login failed — redirected to: {page.url}")
    return False


def login_duolingo_via_browser(page):
    """Login via the browser's fetch API — cookies are set directly in the browser context."""
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
    if result.get("failure") or (result.get("message") and not result.get("username")):
        raise Exception(f"Browser API login rejected: {result.get('failure') or result.get('message')}")

    print(f"  Browser API login successful (username: {result.get('username')})")
    page.goto("https://www.duolingo.com/learn")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)

    if "/learn" in page.url:
        return True
    print(f"  Browser API login did not reach /learn. URL: {page.url}")
    return False


def login_duolingo(page):
    """Login via web form (for local use with visible browser)."""
    page.goto("https://www.duolingo.com/?isLoggingIn=true")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)

    # Click "I ALREADY HAVE AN ACCOUNT" to open login modal
    sign_in_selectors = [
        'button:has-text("I ALREADY HAVE AN ACCOUNT")',
        'button:has-text("I already have an account")',
        'button:has-text("SIGN IN")',
        'button:has-text("Sign in")',
        'button:has-text("LOG IN")',
        'button:has-text("Log in")',
        'a:has-text("I ALREADY HAVE AN ACCOUNT")',
        'a:has-text("SIGN IN")',
        'a:has-text("LOG IN")',
    ]
    for sel in sign_in_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                print(f"  Clicked: {sel}")
                page.wait_for_timeout(2000)
                break
        except Exception:
            continue

    # Find login form fields
    email_selectors = [
        '#web-ui1', 'input[data-test="email-input"]',
        'input[type="email"]', 'input[type="text"]',
    ]
    password_selectors = [
        '#web-ui2', 'input[data-test="password-input"]',
        'input[type="password"]',
    ]

    email_input = None
    for sel in email_selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(timeout=3000)
            email_input = loc
            break
        except Exception:
            continue

    password_input = None
    for sel in password_selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(timeout=3000)
            password_input = loc
            break
        except Exception:
            continue

    if not email_input or not password_input:
        try:
            page.screenshot(path="login_debug.png")
        except Exception:
            pass
        raise Exception(f"Could not find login fields. URL: {page.url}")

    email_input.fill(EMAIL)
    human_sleep(0.5, 1.0)
    password_input.fill(PASSWORD)
    human_sleep(0.5, 1.5)

    # Click login button
    for sel in ['button:has-text("LOG IN")', 'button:has-text("Log in")',
                'button:has-text("SIGN IN")', 'button[type="submit"]']:
        try:
            page.locator(sel).first.click(timeout=3000)
            print(f"  Clicked login button: {sel}")
            break
        except Exception:
            continue

    # Wait for redirect
    try:
        page.wait_for_url("**/learn**", timeout=30000)
        print("Login successful - redirected to learn page")
    except Exception:
        page.wait_for_timeout(5000)
        current_url = page.url
        if "/log-in" in current_url or "isLoggingIn" in current_url:
            try:
                page.screenshot(path="login_failed.png")
            except Exception:
                pass
            raise Exception(f"Login failed - still on login page: {current_url}")


def main():
    with sync_playwright() as p:

        headless = os.getenv("HEADLESS", "false").lower() == "true"
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

        video_dir = os.path.join(os.getcwd(), "playwright-videos")
        os.makedirs(video_dir, exist_ok=True)

        if os.path.exists(SESSION_FILE):
            print("Loading saved session...")
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                storage_state=SESSION_FILE,
                user_agent=ua,
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 800},
            )
        else:
            print("No session found → logging in")
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=ua,
                record_video_dir=video_dir,
                record_video_size={"width": 1280, "height": 800},
            )

        page = context.new_page()

        # Hide automation indicators
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            delete navigator.__proto__.webdriver;
        """)

        def is_logged_in_url(url):
            """Check if URL indicates successful login (on /learn, not on login/root)."""
            return "/learn" in url and "/login" not in url

        def do_login():
            """Try login methods: JWT → browser fetch API → web form."""
            # JWT token (fastest, most reliable for CI)
            try:
                if login_with_jwt(context, page):
                    return True
            except Exception as e:
                print(f"  JWT login failed: {e}")

            # Browser-based fetch login
            try:
                if login_duolingo_via_browser(page):
                    return True
            except Exception as e:
                print(f"  Browser API login failed: {e}")

            # Web form login (non-headless only)
            if not headless:
                print("  Falling back to web form login...")
                login_duolingo(page)
                return True

            return False

        if not os.path.exists(SESSION_FILE):
            if not do_login():
                raise Exception("All login methods failed")
        else:
            page.goto("https://www.duolingo.com/learn")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

        # Save session AFTER page loads (so localStorage/cookies are fully set)
        if not os.path.exists(SESSION_FILE):
            print("Saving session...")
            context.storage_state(path=SESSION_FILE)

        # Verify we're logged in (not redirected to login page)
        current_url = page.url
        print(f"Current URL after navigation: {current_url}")
        if not is_logged_in_url(current_url):
            print("⚠ Session expired or invalid, logging in again...")
            # Delete stale session
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
            if not do_login():
                raise Exception(f"Login failed after retry. Current URL: {page.url}")
            context.storage_state(path=SESSION_FILE)

        xp_before = None
        in_practice_mode = True
        start_practice_mode(page)

        global MAX_WRONG_PER_LESSON
        xp_after = None
        consecutive_no_question = 0
        question_count = 0
        wrong_count = 0  # Track deliberate wrong answers per lesson
        lesson_count = 0

        while True:
            try:
                human_sleep(2.0, 4.0)

                print("\n📸 Capturing screen...")
                img = page.screenshot(type="jpeg", quality=80)

                print("🤖 Analyzing with AI...")
                result = analyze_screen(img)

                q_type = result.get("type", "unknown")
                question = result.get("question", "")
                answer = result.get("answer", "")

                print(f"  Type: {q_type}")
                print(f"  Question: {question}")
                print(f"  Answer: {answer}")

                if q_type == "no_question":
                    consecutive_no_question += 1
                    print("  No question detected, waiting...")

                    # Detect if we're stuck on a login screen (not a real lesson)
                    q_lower = question.lower()
                    if any(kw in q_lower for kw in ["log in", "login", "sign in", "sign up", "create account"]):
                        print("  ⚠ Login/signup screen detected! Session may be invalid.")
                        if os.path.exists(SESSION_FILE):
                            os.remove(SESSION_FILE)
                        if not do_login():
                            raise Exception("Re-login failed after detecting login screen")
                        context.storage_state(path=SESSION_FILE)
                        consecutive_no_question = 0
                        continue

                    # Check if out of hearts → switch to practice
                    if check_no_hearts(page):
                        print("  💔 Out of hearts! Switching to practice...")
                        click_button(page, ["NO THANKS", "No thanks", "CLOSE", "Close", "✕"])
                        human_sleep(0.5, 1.0)
                        start_practice_mode(page)
                        in_practice_mode = True
                        consecutive_no_question = 0
                        continue

                    # Check if lesson is complete (URL changed back to /learn)
                    current_url = page.url
                    is_on_learn_page = ("/learn" in current_url or "/practice-hub" in current_url) and "/lesson" not in current_url

                    # Try clicking continue/next in case we're on a result screen
                    click_button(
                        page,
                        ["Continue", "CONTINUE", "Next", "START", "Start"],
                    )

                    # If stuck with no questions answered, something is wrong
                    if consecutive_no_question >= 5 and question_count == 0:
                        print("  ⚠ Stuck: no questions detected after 5 attempts. Taking screenshot...")
                        try:
                            page.screenshot(path="stuck_debug.png")
                        except Exception:
                            pass
                        raise Exception(f"No questions found. URL: {page.url}")

                    if is_on_learn_page or (consecutive_no_question >= 5 and question_count > 0):
                        if is_on_learn_page:
                            print("  ✅ Lesson complete! (back on learn page)")
                        else:
                            print("  ✅ Lesson seems done. Starting next lesson...")

                        lesson_count += 1
                        print(f"  📊 Lessons completed: {lesson_count}")

                        if MAX_LESSONS > 0 and lesson_count >= MAX_LESSONS:
                            print(f"\n🎉 Completed {lesson_count} lessons. Done!")
                            break

                        # Go straight to next practice
                        start_practice_mode(page)
                        in_practice_mode = True
                        consecutive_no_question = 0
                        wrong_count = 0
                        MAX_WRONG_PER_LESSON = 0
                        question_count = 0
                        context.storage_state(path=SESSION_FILE)
                        print("\n🆕 New practice started!")

                    human_sleep(0.5, 1.0)
                    continue

                consecutive_no_question = 0
                question_count += 1

                # Handle tracing exercises — skip (cannot automate mouse drawing)
                if q_type == "tracing":
                    print("  ✏️ Tracing exercise detected — skipping (cannot automate)")
                    click_button(page, ["Skip", "SKIP", "CAN'T USE KEYBOARD"])
                    human_sleep(0.5, 1.0)
                    click_button(page, ["Continue", "CONTINUE"])
                    human_sleep(0.5, 1.0)
                    continue

                # Handle listening exercises separately
                if q_type == "listening":
                    print("  🎧 Listening exercise detected")
                    human_sleep(0.3, 0.8)
                    executed = handle_listening(page, result)
                    if executed:
                        handle_post_answer(page)
                    continue

                # Decide if we should answer wrong (for human simulation)
                # Never in practice mode, never on matching/tap_pairs
                force_wrong = (
                    not in_practice_mode
                    and wrong_count < MAX_WRONG_PER_LESSON
                    and q_type not in ("matching", "tap_pairs")
                    and should_answer_wrong()
                )

                # For word_bank: read actual DOM tokens and refine actions
                if q_type == "word_bank" and not force_wrong:
                    refine_word_bank_actions(page, result)

                # Thinking time based on answer complexity
                num_actions = len(result.get("actions", []))
                if num_actions <= 1:
                    think_time = random.uniform(0.3, 1.0)
                elif num_actions <= 3:
                    think_time = random.uniform(0.5, 1.5)
                else:
                    think_time = random.uniform(0.8, 2.0)
                if force_wrong:
                    think_time = random.uniform(0.2, 0.8)
                print(f"  Thinking for {think_time:.1f}s...")
                time.sleep(think_time)

                # Execute the actions
                print("  Executing actions...")
                executed = execute_actions(page, result, force_wrong=force_wrong)

                if executed:
                    handle_post_answer(page)
                    if force_wrong:
                        wrong_count += 1
                        print(f"  ❌ Wrong answers so far: {wrong_count}/{MAX_WRONG_PER_LESSON}")
                        human_sleep(0.3, 0.8)
                        click_button(page, ["Continue", "CONTINUE"])
                        human_sleep(0.5, 1.5)
                else:
                    print("  No actions executed, skipping...")
                    skip_if_stuck(page)

            except json.JSONDecodeError as e:
                print(f"  ⚠ AI returned invalid JSON: {e}")
                human_sleep(0.5, 1.0)

            except KeyboardInterrupt:
                print(f"\nStopped by user after {question_count} questions, {lesson_count} lessons completed")
                break

            except Exception as e:
                print(f"  ⚠ Error: {e}")
                traceback.print_exc()
                human_sleep(0.5, 1.0)

        try:
            browser.close()
        except Exception:
            pass
        print("Done!")


main()
