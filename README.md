# Duolingo AI Agent - Chinese for English Speakers

An AI-powered agent that automatically completes Duolingo Chinese (Mandarin) lessons using GPT-4o Vision and OpenAI Whisper. The agent **takes screenshots like a human**, understands the exercise, and **clicks/types the answer automatically** — no HTML parsing needed.

## Features

- **Fully automatic** - Detects exercise type, answers questions, starts new lessons, and loops continuously
- **Multiple exercise types** - Image choice, multiple choice, word bank, typing, matching pairs, listening
- **Chinese character support** - Handles mixed hanzi + pinyin text (e.g. "汤tāng") with smart extraction
- **Listening exercises** - Captures system audio via loopback and transcribes with OpenAI Whisper
- **Human-like behavior** - Random delays between actions, deliberate wrong answers (0-2 per lesson) to avoid detection
- **Heart/lives management** - Monitors hearts and auto-switches to practice mode when running low
- **XP tracking** - Reports XP gained before and after each session
- **Session persistence** - Saves login session to avoid re-authentication
- **GitHub Actions support** - Run daily practice automatically via CI/CD
- **Graceful shutdown** - Press Ctrl+C to stop and see XP summary

## How It Works

1. Logs in to Duolingo (or loads a saved session)
2. Checks hearts — if low, switches to free practice mode
3. Starts a lesson automatically
4. Enters a fully automatic loop:
   - Captures a screenshot of the current screen
   - Sends it to GPT-4o Vision, which returns structured JSON with exercise type, correct answer, and actions
   - Executes the actions (clicking words, typing answers, pressing keyboard shortcuts)
   - Clicks Check/Continue to proceed
5. After each lesson, starts the next one until `MAX_LESSONS` is reached

## Supported Exercise Types

| Type | How it answers |
|------|---------------|
| **Image choice** | Clicks the correct image (1, 2, or 3) |
| **Multiple choice** | Clicks the correct option |
| **Word bank** | Clicks words in the correct order (handles hanzi/pinyin tokens) |
| **Typing** | Types the answer in the text field |
| **Matching pairs** | Uses keyboard shortcuts (1-5 left, 6-0 right) |
| **Listening** | Records system audio → Whisper transcription → clicks/types answer |

## Prerequisites

- Python 3.8+
- An [OpenAI API key](https://platform.openai.com/api-keys) with GPT-4o access
- A Duolingo account

## Installation

1. Clone the repository:

```bash
git clone https://github.com/your-username/duolingo-ai-agent.git
cd duolingo-ai-agent
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Install the Playwright browser:

```bash
playwright install chromium
```

4. Create a `.env` file from the example:

```bash
cp .env.example .env
```

5. Fill in your credentials in `.env`:

```
OPENAI_API_KEY=your_openai_key
DUO_EMAIL=your_email
DUO_PASSWORD=your_password
DUO_PROFILE_URL=your_duolingo_username
```

## Usage

### Local

```bash
python duolingo_cn_for_en.py
```

The agent will log in, check XP, and start completing lessons automatically.

**Environment variables:**

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key | Required |
| `DUO_EMAIL` | Duolingo email | Required |
| `DUO_PASSWORD` | Duolingo password | Required |
| `DUO_PROFILE_URL` | Duolingo username (for XP tracking) | Optional |
| `MAX_LESSONS` | Number of lessons to complete (0 = unlimited) | `0` |
| `HEADLESS` | Run browser without UI (`true`/`false`) | `false` |

Press **Ctrl+C** at any time to stop and see your XP summary.

### GitHub Actions (Daily Automated Practice)

1. Go to your repo **Settings > Secrets and variables > Actions**
2. Add these secrets: `DUO_EMAIL`, `DUO_PASSWORD`, `OPENAI_API_KEY`, `DUO_PROFILE_URL`
3. The workflow runs daily at 7:00 AM UTC, or trigger manually from the **Actions** tab
4. Adjust `max_lessons` when triggering manually (default: 3)

> **Note:** Listening exercises are automatically skipped in CI (no audio device). The agent will click "CAN'T LISTEN NOW" and continue.

## Tech Stack

- [Playwright](https://playwright.dev/python/) - Browser automation
- [OpenAI GPT-4o](https://platform.openai.com/docs/) - Vision model for screenshot analysis
- [OpenAI Whisper](https://platform.openai.com/docs/guides/speech-to-text) - Audio transcription for listening exercises
- [sounddevice](https://python-sounddevice.readthedocs.io/) + [soundfile](https://pysoundfile.readthedocs.io/) - System audio capture via WASAPI loopback
- [python-dotenv](https://github.com/theskumar/python-dotenv) - Environment variable management
