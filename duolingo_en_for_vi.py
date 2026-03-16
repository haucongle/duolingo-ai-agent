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

EMAIL = os.getenv("VI_DUO_EMAIL")
PASSWORD = os.getenv("VI_DUO_PASSWORD")
DUO_JWT = os.getenv("DUO_JWT")  # Optional: pre-authenticated JWT token

SESSION_FILE = "vi_duo_session.json"

# Chance of deliberately answering wrong (0.0 - 1.0)
WRONG_ANSWER_CHANCE = 0.10
# Max deliberate wrong answers per lesson (random 0-2)
MAX_WRONG_PER_LESSON = random.randint(0, 2)
# Max lessons to complete (0 = unlimited). Set via env MAX_LESSONS.
MAX_LESSONS = int(os.getenv("MAX_LESSONS", "0"))

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT = """You are an AI agent that solves Duolingo exercises automatically.
This is an English course for Vietnamese speakers. The UI may be in Vietnamese.

Look at this Duolingo screenshot and determine:
1. The type of exercise
2. The correct answer
3. The exact action(s) needed to answer
4. ALL available options (for deliberate wrong answers)

Respond ONLY with valid JSON (no markdown, no explanation) using this format:

{
  "type": "image_choice | multiple_choice | word_bank | typing | matching | audio_matching | audio_fill_blank | listen_and_type | speaking | listening | tap_pairs | no_question",
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
  all_options = ["coffee", "tofu", "rice"], total_options = 3

- multiple_choice: Text options to click. actions = [{"action": "click", "target": "exact option text"}]
  If options have number shortcuts (1, 2, 3...), prefer: actions = [{"action": "press", "key": "1"}]
  all_options = list of all option texts, total_options = number of options

- word_bank: Click words in the correct order from the word bank.
  actions = [{"action": "click", "target": "word1"}, {"action": "click", "target": "word2"}, ...]
  all_options = list of all available words in the bank, total_options = number of words

- typing: Type the answer in the text field.
  actions = [{"action": "type", "target": "input", "value": "the answer"}]
  all_options = [], total_options = 0

- matching / tap_pairs: "Select the matching pairs" / "Chọn cặp từ" - cards with number shortcuts.
  There are TWO sub-types:

  (A) Text-Text matching: both sides show text. Left side has number shortcuts 1-4, right side 5-8.
      actions = [{"action": "press", "key": "1"}, {"action": "press", "key": "7"}, ...]
      all_options = ["1:This is", "2:tea", "5:Đây là", "6:trà", "7:cháo", "8:đậu phụ"], total_options = number of cards

  (B) Audio-Text matching ("Chọn cặp từ"): left side has AUDIO waveforms with numbers (1-4),
      right side has Vietnamese text with numbers (5-8).
      You CANNOT hear the audio. Return type="audio_matching" instead.
      List ONLY the right-side Vietnamese text options.
      actions = [] (will be handled by audio recognition)
      all_options = ["5:kéo", "6:chăn", "7:đủ", "8:sáu mươi"], total_options = number of right-side cards
      left_keys = ["1", "2", "3", "4"] (the number keys for the left audio cards)

  How to tell them apart: if the left cards show speaker icons / audio waveforms instead of text,
  it is audio_matching. If both sides show readable text, it is matching/tap_pairs.

- audio_matching: Audio-to-text matching exercise (see matching type B above).
  actions = [] (will be handled by audio recognition)
  all_options = list of "number:text" for the RIGHT side only (e.g. ["5:kéo", "6:chăn", "7:đủ", "8:sáu mươi"])
  left_keys = ["1", "2", "3", "4"] (number keys of left audio cards)
  total_options = number of right-side cards

- audio_fill_blank: "Nghe và tìm từ còn thiếu" with AUDIO OPTION CARDS below.
  A sentence with a blank (___) is shown, plus numbered audio waveform cards (1, 2, ...) as answer choices.
  You CANNOT hear the audio options. Return type="audio_fill_blank".
  actions = [] (will be handled by audio recognition)
  sentence = "The famous writer was ___ in the 19th century." (the sentence with the blank)
  all_options = ["1", "2"] (the number keys of the audio option cards)
  total_options = number of audio option cards

- listen_and_type: "Nhập từ còn thiếu" / "Type the missing word" - listen to audio and TYPE the answer.
  A sentence with a blank (___) is shown, plus a TEXT INPUT field to type in, and speaker buttons to hear the audio.
  There are NO numbered audio option cards — just speaker buttons and a text input.
  You CANNOT hear the audio. Return type="listen_and_type".
  actions = [] (will be handled by audio recognition + typing)
  sentence = "I don't have any ___." (the sentence with the blank, use ___ for the blank)
  all_options = [], total_options = 0

  How to tell audio_fill_blank vs listen_and_type apart:
  - audio_fill_blank: has numbered audio OPTION CARDS (1, 2) below the sentence → select one
  - listen_and_type: has a TEXT INPUT field below the sentence → type the missing word

- listening: "Tap what you hear" - has a speaker button and a word bank below.
  DO NOT guess the answer. Just return the visible word bank options.
  actions = [] (will be handled by audio recognition)
  all_options = list ALL visible words/chips in the word bank (e.g. ["the", "is", "water", "and", "rice", "this"])
  total_options = number of words in the bank

- speaking: "Đọc câu này" / "Read this sentence" - requires microphone to speak.
  Has a "NHẤN ĐỂ ĐỌC" (tap to speak) button. Cannot be solved by a bot.
  actions = [] (will be skipped)
  all_options = [], total_options = 0

- no_question: No exercise visible (loading, result screen, etc). actions = []
  all_options = [], total_options = 0

IMPORTANT:
- For image_choice: look for number labels (1, 2, 3) on each card, use "press" with that number
- For word_bank: each "target" must be the exact visible text of the word/chip on screen.
  Options may be in English or Vietnamese depending on the exercise direction.
- For multiple_choice: "target" must be the exact text of the option, or use "press" if number shortcuts are visible
- For typing: "value" is the full answer to type (in English or Vietnamese as required by the exercise)
- all_options should list the exact visible text of all options on screen
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
                language="en",  # English for Duolingo English course
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

    # Step 4: Match transcript to word bank options and click them in order
    print(f"  Matching '{transcript}' to word bank: {all_options}")

    # Build click order: find which options appear in the transcript, in order
    words_to_click = match_words_to_transcript(transcript, all_options)

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


def match_english_to_vietnamese(english_word, vietnamese_options):
    """Use GPT to match an English word/phrase to the correct Vietnamese option."""
    try:
        options_str = ", ".join(f'"{opt}"' for opt in vietnamese_options)
        r = client.responses.create(
            model="gpt-4o-mini",
            input=[{
                "role": "user",
                "content": (
                    f'The English word/phrase is: "{english_word}"\n'
                    f'The Vietnamese options are: [{options_str}]\n'
                    f'Which Vietnamese option is the correct translation? '
                    f'Reply with ONLY the exact Vietnamese text, nothing else.'
                ),
            }],
        )
        match = r.output_text.strip().strip('"').strip("'")
        print(f"    GPT match: '{english_word}' → '{match}'")
        return match
    except Exception as e:
        print(f"    ⚠ GPT matching failed: {e}")
        return None


def handle_audio_matching(page, result):
    """Handle audio-text matching: click left audio card → record → transcribe → match Vietnamese right card."""

    all_options = result.get("all_options", [])  # e.g. ["5:kéo", "6:chăn", "7:đủ", "8:sáu mươi"]
    left_keys = result.get("left_keys", [])      # e.g. ["1", "2", "3", "4"]

    if not left_keys:
        # Fallback: assume left keys are 1..N where N = len(all_options)
        left_keys = [str(i) for i in range(1, len(all_options) + 1)]

    # Parse right-side options: "5:kéo" → {key: "5", text: "kéo"}
    right_options = []
    for opt in all_options:
        if ":" in opt:
            key, text = opt.split(":", 1)
            right_options.append({"key": key.strip(), "text": text.strip()})

    if not right_options:
        print("  ⚠ No right-side options parsed, skipping...")
        skip_if_stuck(page)
        return False

    vietnamese_texts = [r["text"] for r in right_options]
    matched_right_keys = set()

    for left_key in left_keys:
        print(f"\n  🔊 Playing audio card [{left_key}]...")

        # Step 1: Start recording
        recording = start_recording(duration=4.0)

        # Step 2: Press the left card number to play its audio
        time.sleep(0.3)
        page.keyboard.press(left_key)
        human_sleep(0.5, 1.0)

        if not recording:
            print(f"    ⚠ No audio support, using GPT vision fallback...")
            # Take a screenshot after clicking to see if any visual hint appears
            img = page.screenshot(type="jpeg", quality=80)
            # Can't do much without audio, skip this card
            continue

        # Step 3: Wait for recording to finish
        audio_path = finish_recording(recording)
        if not audio_path:
            print(f"    ⚠ Recording failed for card [{left_key}]")
            continue

        # Step 4: Transcribe
        transcript = transcribe_audio(audio_path)
        try:
            os.remove(audio_path)
        except Exception:
            pass

        if not transcript:
            print(f"    ⚠ Transcription failed for card [{left_key}]")
            continue

        # Step 5: Match English transcript to Vietnamese option using GPT
        remaining_vietnamese = [t for t in vietnamese_texts if t not in matched_right_keys]
        matched_text = match_english_to_vietnamese(transcript, remaining_vietnamese)

        if matched_text:
            # Find the right-side key for this text
            for r in right_options:
                if r["text"] == matched_text and r["text"] not in matched_right_keys:
                    print(f"    Pressing right card [{r['key']}] for '{matched_text}'")
                    page.keyboard.press(r["key"])
                    matched_right_keys.add(r["text"])
                    human_sleep(0.3, 0.8)
                    break
            else:
                # Fuzzy match: try case-insensitive partial match
                for r in right_options:
                    if r["text"] not in matched_right_keys and (
                        matched_text.lower() in r["text"].lower()
                        or r["text"].lower() in matched_text.lower()
                    ):
                        print(f"    Pressing right card [{r['key']}] for '{r['text']}' (fuzzy)")
                        page.keyboard.press(r["key"])
                        matched_right_keys.add(r["text"])
                        human_sleep(0.3, 0.8)
                        break

    return len(matched_right_keys) > 0


def handle_audio_fill_blank(page, result):
    """Handle 'Nghe và tìm từ còn thiếu' — sentence with blank + audio answer options.
    Strategy: play the main sentence audio first to get full sentence via Whisper,
    then figure out the missing word by comparing with the sentence shown on screen.
    If that fails, click each audio option, transcribe, and pick the one that fits the blank.
    """
    sentence = result.get("sentence", "") or result.get("question", "")
    option_keys = result.get("all_options", [])  # e.g. ["1", "2"]

    if not option_keys:
        print("  ⚠ No audio options found, skipping...")
        skip_if_stuck(page)
        return False

    print(f"  Sentence: {sentence}")
    print(f"  Audio options: {option_keys}")

    # Strategy: click the main sentence speaker to hear full sentence,
    # then transcribe each option and use GPT to pick the right one.

    # Step 1: Try to get the full sentence by clicking the sentence speaker icon
    full_transcript = None
    if HAS_AUDIO:
        recording = start_recording(duration=5.0)
        if recording:
            time.sleep(0.3)
            # Click the speaker icon in the sentence area
            click_speaker(page)
            audio_path = finish_recording(recording)
            if audio_path:
                full_transcript = transcribe_audio(audio_path)
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    if full_transcript:
        print(f"  Full sentence audio: '{full_transcript}'")

    # Step 2: Transcribe each audio option
    option_transcripts = {}
    for key in option_keys:
        if not HAS_AUDIO:
            break
        print(f"  🔊 Playing option [{key}]...")
        recording = start_recording(duration=3.0)
        if not recording:
            continue
        time.sleep(0.3)
        page.keyboard.press(key)
        human_sleep(0.5, 0.8)
        audio_path = finish_recording(recording)
        if audio_path:
            transcript = transcribe_audio(audio_path)
            if transcript:
                option_transcripts[key] = transcript
            try:
                os.remove(audio_path)
            except Exception:
                pass

    print(f"  Option transcripts: {option_transcripts}")

    if not option_transcripts:
        print("  ⚠ Could not transcribe any options, skipping...")
        skip_if_stuck(page)
        return False

    # Step 3: Use GPT to pick the correct option
    correct_key = None
    if full_transcript and sentence:
        # We have both the full spoken sentence and the written sentence with blank
        # Ask GPT which option fills the blank
        try:
            options_str = ", ".join(f'{k}: "{v}"' for k, v in option_transcripts.items())
            r = client.responses.create(
                model="gpt-4o-mini",
                input=[{
                    "role": "user",
                    "content": (
                        f'A Duolingo exercise shows this sentence with a blank: "{sentence}"\n'
                        f'The full spoken sentence is: "{full_transcript}"\n'
                        f'The audio options are: {options_str}\n'
                        f'Which option number fills the blank correctly? '
                        f'Reply with ONLY the number (e.g. "1" or "2"), nothing else.'
                    ),
                }],
            )
            correct_key = r.output_text.strip()
            print(f"  GPT picked option: {correct_key}")
        except Exception as e:
            print(f"  ⚠ GPT selection failed: {e}")

    if not correct_key or correct_key not in option_transcripts:
        # Fallback: if we have the full transcript, find which option word appears in it
        # but not in the visible sentence
        if full_transcript and sentence:
            clean = lambda s: re.sub(r'[^\w\s\']', '', s.lower())
            full_clean = clean(full_transcript)
            sentence_clean = clean(sentence.replace("___", "").replace("_", ""))
            for key, transcript in option_transcripts.items():
                word = clean(transcript).strip()
                if word and word in full_clean and word not in sentence_clean:
                    correct_key = key
                    print(f"  Fallback match: option [{key}] '{transcript}' found in full sentence")
                    break

    if not correct_key or correct_key not in option_transcripts:
        # Last resort: pick first option
        correct_key = option_keys[0]
        print(f"  ⚠ Could not determine answer, guessing option [{correct_key}]")

    # Step 4: Select the answer by pressing the key
    print(f"  Selecting option [{correct_key}]: '{option_transcripts.get(correct_key, '?')}'")
    page.keyboard.press(correct_key)
    human_sleep(0.3, 0.8)

    return True


def handle_listen_and_type(page, result):
    """Handle 'Nhập từ còn thiếu' — listen to audio, find missing word, type it."""
    sentence = result.get("sentence", "") or result.get("question", "")
    print(f"  Sentence: {sentence}")

    # Step 1: Listen to the full sentence via speaker button
    full_transcript = None
    if HAS_AUDIO:
        recording = start_recording(duration=5.0)
        if recording:
            time.sleep(0.3)
            click_speaker(page)
            audio_path = finish_recording(recording)
            if audio_path:
                full_transcript = transcribe_audio(audio_path)
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    if not full_transcript:
        print("  ⚠ Could not transcribe audio, skipping...")
        skip_if_stuck(page)
        return False

    print(f"  Full sentence: '{full_transcript}'")

    # Step 2: Find the missing word by comparing transcript with the sentence
    missing_word = None

    # Use GPT to extract the missing word
    if sentence:
        try:
            r = client.responses.create(
                model="gpt-4o-mini",
                input=[{
                    "role": "user",
                    "content": (
                        f'A Duolingo exercise shows this sentence with a blank: "{sentence}"\n'
                        f'The full spoken sentence is: "{full_transcript}"\n'
                        f'What is the missing word or phrase that fills the blank? '
                        f'Reply with ONLY the missing word(s), nothing else.'
                    ),
                }],
            )
            missing_word = r.output_text.strip().strip('"').strip("'").rstrip(".")
            print(f"  Missing word: '{missing_word}'")
        except Exception as e:
            print(f"  ⚠ GPT extraction failed: {e}")

    if not missing_word:
        # Fallback: simple diff — find words in transcript not in sentence
        clean = lambda s: re.sub(r'[^\w\s\']', '', s.lower().replace("___", ""))
        sentence_words = clean(sentence).split()
        transcript_words = clean(full_transcript).split()
        # Remove each sentence word from transcript words (preserving order, handling duplicates)
        remaining = list(transcript_words)
        for sw in sentence_words:
            if sw in remaining:
                remaining.remove(sw)
        if remaining:
            missing_word = " ".join(remaining)
            print(f"  Fallback missing word: '{missing_word}'")

    if not missing_word:
        print("  ⚠ Could not find missing word, skipping...")
        skip_if_stuck(page)
        return False

    # Step 3: Type the missing word
    print(f"  Typing missing word: '{missing_word}'")
    type_answer(page, missing_word)

    return True


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

        # Random delay between actions (human-like)
        if i > 0:
            if is_matching and i % 2 == 0:
                # Longer pause between pairs (thinking about next pair)
                human_sleep(0.3, 0.8)
            else:
                human_sleep(0.5, 1.2)

        if action == "press":
            print(f"  [{i+1}] Pressing key: '{key}'")
            page.keyboard.press(key)

        elif action == "click":
            print(f"  [{i+1}] Clicking: '{target}'")
            click_target(page, target)
            # Extra wait after click for DOM to update (word moves to answer area)
            time.sleep(0.3)

        elif action == "type":
            print(f"  [{i+1}] Typing: '{value}'")
            type_answer(page, value)

    return True


def extract_display_text(text):
    """Extract the primary display text from a word token.
    For English/Vietnamese, just return the visible text as-is.
    If multi-line, return the first non-empty line as primary.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        return lines[0], ""
    return text, ""


def get_all_word_tokens(page):
    """Get all visible word bank tokens with their text content and elements.
    Returns list of dicts with full_text, display_text, secondary, locator.
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
                print(f"    Found {len(tokens)} tokens via '{sel}': {[t['display_text'] for t in tokens]}")
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
            if tokens:
                print(f"    Found {len(tokens)} tokens via button scan: {[t['display_text'] for t in tokens]}")
        except Exception:
            pass

    return tokens


def click_word_token(page, text):
    """Click a word bank token matching the given text.
    Re-queries tokens every time because DOM changes after each click.
    """
    # Always re-query tokens (DOM changes after every click)
    tokens = get_all_word_tokens(page)

    if tokens:
        # Try exact match first
        for token in tokens:
            if token["display_text"] == text or token["secondary"] == text:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue

        # Case-insensitive match (for English words like "This" vs "this")
        text_lower = text.lower()
        for token in tokens:
            if token["display_text"].lower() == text_lower:
                try:
                    token["locator"].click(timeout=500)
                    return True
                except Exception:
                    continue

        # Partial match: text contained in display_text or vice versa
        for token in tokens:
            if text in token["display_text"] or token["display_text"] in text:
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


def click_target(page, text):
    """Click an element matching the given text."""

    # First try the smart word token matching
    if click_word_token(page, text):
        return True

    return False


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
            existing = loc.input_value(timeout=300)
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
            # Type character by character with random delays (human-like)
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


def handle_post_answer(page):
    """Click Check, Continue, or Next after answering."""

    human_sleep(0.5, 1.5)

    # Click CHECK / KIỂM TRA button
    click_button(page, ["Check", "KIỂM TRA", "CHECK", "Kiểm tra"])
    human_sleep(0.3, 0.8)

    # Click CONTINUE / TIẾP TỤC button (appears after check)
    click_button(page, ["Continue", "CONTINUE", "TIẾP TỤC", "Tiếp tục"])
    human_sleep(0.5, 1.5)


def skip_if_stuck(page):
    """Click Skip if available (for listening exercises etc)."""
    try:
        click_button(page, ["TẠM THỜI KHÔNG NÓI ĐƯỢC", "Tạm thời không nói được",
                            "CAN'T SPEAK NOW", "Skip", "SKIP", "BỎ QUA",
                            "CAN'T LISTEN NOW"])
        return True
    except Exception:
        return False


def click_start_xp_button(page):
    """Click the 'START +XX XP' button in the lesson popup."""
    import re

    # Try multiple approaches to find the XP start button (Vietnamese: "BẮT ĐẦU +XX KN")
    attempts = [
        # 1. Vietnamese: "BẮT ĐẦU +XX KN"
        lambda: page.get_by_text(re.compile(r"BẮT ĐẦU\s*\+\s*\d+\s*KN", re.IGNORECASE)).first,
        lambda: page.get_by_text(re.compile(r"BẮT ĐẦU\s*\+", re.IGNORECASE)).first,
        lambda: page.locator('button:has-text("BẮT ĐẦU +")').first,
        lambda: page.locator('a:has-text("BẮT ĐẦU +")').first,
        lambda: page.locator('div:has-text("BẮT ĐẦU +")').last,
        # 2. English fallback: "START +XX XP"
        lambda: page.get_by_text(re.compile(r"START\s*\+\s*\d+\s*XP", re.IGNORECASE)).first,
        lambda: page.get_by_text(re.compile(r"START\s*\+", re.IGNORECASE)).first,
        lambda: page.locator('button:has-text("START +")').first,
        lambda: page.locator('a:has-text("START +")').first,
        lambda: page.locator('div:has-text("START +")').last,
        # 3. data-test attribute for start button
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

    # Last resort: screenshot and log for debugging
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
    """Navigate to practice mode (doesn't cost hearts)."""
    print("  🏋️ Starting practice mode (free, no hearts needed)...")
    page.goto("https://www.duolingo.com/practice")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    return True


def start_lesson(page):
    """Auto-detect and start the next available lesson."""

    print("Looking for a lesson to start...")
    human_sleep(0.3, 0.8)

    # Check if out of hearts
    if check_no_hearts(page):
        print("  💔 Out of hearts! Switching to practice mode...")
        # Close popup first
        click_button(page, ["NO THANKS", "No thanks", "CLOSE", "Close", "✕"])
        human_sleep(0.5, 1.0)
        start_practice_mode(page)
        return True

    # Step 1: Click the "START" label above the active lesson icon
    start_texts = ["BẮT ĐẦU", "Bắt đầu", "START", "Start"]
    clicked_start = False

    for text in start_texts:
        try:
            loc = page.get_by_text(text, exact=True).first
            loc.click(timeout=2000)
            print(f"  Clicked '{text}' on learn page")
            clicked_start = True
            human_sleep(0.3, 1.0)
            break
        except Exception:
            continue

    if not clicked_start:
        # Fallback: try data-test selectors for the active node
        fallback_selectors = [
            '[data-test="skill-path"] [aria-current="true"]',
            '[data-test="start-button"]',
        ]
        for sel in fallback_selectors:
            try:
                loc = page.locator(sel).first
                loc.click(timeout=1500)
                print(f"  Clicked: {sel}")
                clicked_start = True
                human_sleep(0.3, 1.0)
                break
            except Exception:
                continue

    if not clicked_start:
        print("  Could not find START button, trying practice mode...")
        start_practice_mode(page)
        return True

    # Step 2: Click "START +XX XP" button in the popup
    if click_start_xp_button(page):
        return True

    # Step 3: Fallback - try other popup buttons
    popup_texts = ["BẮT ĐẦU", "Bắt đầu", "START", "Start", "START LESSON",
                   "TIẾP TỤC", "CONTINUE", "Continue", "LUYỆN TẬP", "PRACTICE"]
    for text in popup_texts:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            btn.click(timeout=1000)
            print(f"  Started lesson via: '{text}'")
            human_sleep(0.5, 1.5)
            return True
        except Exception:
            continue

    # Might already be in the lesson
    return True


_profile_raw = os.getenv("VI_DUO_PROFILE_URL", "")
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

        # Debug: print relevant section of profile text
        if 'Tổng điểm KN' in page_text:
            idx = page_text.index('Tổng điểm KN')
            print(f"    Profile text near XP: ...{repr(page_text[max(0,idx-60):idx+20])}...")

        # Find XP: match "NNN XP" (English) or "NNN\nTổng điểm KN" (Vietnamese)
        all_xp = re.findall(r'([\d,]+)\s*XP', page_text)
        # Vietnamese: number before "Tổng điểm KN" (may have newlines/spaces between)
        vi_xp = re.findall(r'([\d,]+)[\s\n]+Tổng điểm KN', page_text)
        all_xp.extend(vi_xp)
        if not all_xp and 'Tổng điểm KN' in page_text:
            # Grab all numbers near "Tổng điểm KN" — look within 50 chars before it
            idx = page_text.index('Tổng điểm KN')
            nearby = page_text[max(0, idx - 50):idx]
            nums = re.findall(r'([\d,]+)', nearby)
            if nums:
                all_xp.append(nums[-1])  # closest number before the label
        if all_xp:
            xp_values = [int(v.replace(",", "")) for v in all_xp]
            print(f"    XP values found on profile: {xp_values}")
            # Take the largest value (total XP)
            xp = max(xp_values)

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

        # Check XP before starting
        print("\n📊 Checking XP before practice...")
        xp_before = get_xp(page)
        print_xp_summary("Before", xp_before)

        # Check hearts and decide: lesson or practice
        hearts = get_hearts(page)
        if hearts >= 0:
            print(f"  ❤️ Hearts: {hearts}/5")
        in_practice_mode = False
        if 0 <= hearts < 5:
            start_practice_mode(page)
            in_practice_mode = True
        else:
            start_lesson(page)

        global MAX_WRONG_PER_LESSON
        xp_after = None
        consecutive_no_question = 0
        question_count = 0
        wrong_count = 0  # Track deliberate wrong answers per lesson
        lesson_count = 0

        while True:
            try:
                # Random thinking pause before capturing (human-like)
                human_sleep(0.3, 0.8)

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
                    is_on_learn_page = "/learn" in current_url and "/lesson" not in current_url

                    # Try clicking continue/next in case we're on a result screen
                    click_button(
                        page,
                        ["TIẾP TỤC", "Tiếp tục", "Continue", "CONTINUE", "Next",
                         "BẮT ĐẦU", "Bắt đầu", "START", "Start"],
                    )

                    # If stuck with no questions answered, something is wrong
                    if consecutive_no_question >= 5 and question_count == 0:
                        print("  ⚠ Stuck: no questions detected after 5 attempts. Taking screenshot...")
                        try:
                            page.screenshot(path="stuck_debug.png")
                        except Exception:
                            pass
                        raise Exception(f"No questions found. URL: {page.url}")

                    if is_on_learn_page or (consecutive_no_question >= 3 and question_count > 0):
                        if is_on_learn_page:
                            print("  ✅ Lesson complete! (back on learn page)")
                        else:
                            print("  ✅ Lesson seems done. Starting next lesson...")

                        # Navigate to learn page if not already there
                        if not is_on_learn_page:
                            page.goto("https://www.duolingo.com/learn")
                            page.wait_for_load_state("domcontentloaded")
                            page.wait_for_timeout(1500)

                        # Start next lesson
                        lesson_count += 1
                        print(f"  📊 Lessons completed: {lesson_count}")

                        if MAX_LESSONS > 0 and lesson_count >= MAX_LESSONS:
                            # Final XP check
                            xp_after = get_xp(page)
                            print(f"\n🎉 Completed {lesson_count} lessons. Done!")
                            print_xp_summary("After", xp_after)
                            if xp_before and xp_after:
                                gained = xp_after['totalXp'] - xp_before['totalXp']
                                print(f"  ⚡ XP gained this session: +{gained}")
                            break

                        # Check hearts before starting next
                        hearts = get_hearts(page)
                        if hearts >= 0:
                            print(f"  ❤️ Hearts: {hearts}/5")
                        if 0 <= hearts < 5:
                            start_practice_mode(page)
                            in_practice_mode = True
                        else:
                            start_lesson(page)
                            in_practice_mode = False
                        consecutive_no_question = 0
                        wrong_count = 0
                        MAX_WRONG_PER_LESSON = random.randint(0, 2)  # Randomize for new lesson
                        question_count = 0
                        # Save fresh session
                        context.storage_state(path=SESSION_FILE)
                        print("\n🆕 New lesson started!")

                    human_sleep(0.3, 1.0)
                    continue

                consecutive_no_question = 0
                question_count += 1

                # Handle audio matching exercises (Chọn cặp từ with audio)
                if q_type == "audio_matching":
                    print("  🔊 Audio matching exercise detected (Chọn cặp từ)")
                    human_sleep(0.3, 0.8)
                    executed = handle_audio_matching(page, result)
                    if executed:
                        handle_post_answer(page)
                    continue

                # Handle speaking exercises (Đọc câu này) — skip, no mic
                if q_type == "speaking":
                    print("  🎤 Speaking exercise detected (Đọc câu này) — skipping (no mic)")
                    skip_if_stuck(page)
                    human_sleep(0.5, 1.0)
                    handle_post_answer(page)
                    continue

                # Handle audio fill-in-the-blank (Nghe và tìm từ còn thiếu)
                if q_type == "audio_fill_blank":
                    print("  🔊 Audio fill-blank exercise detected (Nghe và tìm từ còn thiếu)")
                    human_sleep(0.3, 0.8)
                    executed = handle_audio_fill_blank(page, result)
                    if executed:
                        handle_post_answer(page)
                    continue

                # Handle listen-and-type (Nhập từ còn thiếu)
                if q_type == "listen_and_type":
                    print("  🔊 Listen-and-type exercise detected (Nhập từ còn thiếu)")
                    human_sleep(0.3, 0.8)
                    executed = handle_listen_and_type(page, result)
                    if executed:
                        handle_post_answer(page)
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
                        # After wrong answer, Duolingo shows correct answer
                        # Need to click Continue again
                        human_sleep(0.3, 0.8)
                        click_button(page, ["Continue", "CONTINUE"])
                        human_sleep(0.5, 1.5)
                else:
                    print("  No actions executed, skipping...")
                    skip_if_stuck(page)

                # Occasional longer pause (like checking phone, etc)
                if random.random() < 0.05:
                    pause = random.uniform(1.5, 3.0)
                    print(f"  📱 Taking a short break ({pause:.1f}s)...")
                    time.sleep(pause)

            except json.JSONDecodeError as e:
                print(f"  ⚠ AI returned invalid JSON: {e}")
                human_sleep(0.3, 1.0)

            except KeyboardInterrupt:
                print(f"\nStopped by user after {question_count} questions, {lesson_count} lessons completed")
                # Try to get final XP (browser still alive at this point)
                try:
                    if not xp_after:
                        xp_after = get_xp(page)
                    if xp_before and xp_after:
                        gained = xp_after['totalXp'] - xp_before['totalXp']
                        print(f"  ⚡ XP gained this session: +{gained}")
                except Exception:
                    pass
                break

            except Exception as e:
                print(f"  ⚠ Error: {e}")
                traceback.print_exc()
                human_sleep(0.3, 1.0)

        try:
            browser.close()
        except Exception:
            pass
        print("Done!")


main()
