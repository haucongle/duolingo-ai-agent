# Duolingo AI Agent

An AI-powered agent that automatically completes Duolingo lessons using GPT-4o Vision and OpenAI Whisper. The agent **takes screenshots like a human**, understands the exercise, and **clicks/types the answer automatically** — no HTML parsing needed.

## Supported Courses

| Script | Course | UI Language |
|--------|--------|-------------|
| `duolingo_cn_for_en.py` | Chinese (Mandarin) for English speakers | English |
| `duolingo_en_for_vi.py` | English for Vietnamese speakers | Vietnamese |

## Features

- **Fully automatic** - Detects exercise type, answers questions, starts new lessons, and loops continuously
- **Multiple exercise types** - Image choice, multiple choice, word bank, typing, matching pairs, listening, tap pairs, tracing (skip)
- **Chinese character support** - Handles mixed hanzi + pinyin text (e.g. "汤tāng") with smart extraction (CN script)
- **Vietnamese UI support** - Handles Vietnamese interface strings and additional exercise types (VI script)
- **Answer caching** - Learns correct answers from Duolingo feedback to improve accuracy (VI script)
- **Listening exercises** - Captures system audio via WASAPI loopback and transcribes with OpenAI Whisper
- **Audio exercises** - Audio matching, audio fill-in-the-blank, and listen-and-type (VI script)
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

### Chinese for English (`duolingo_cn_for_en.py`)

| Type | How it answers |
|------|---------------|
| **Image choice** | Presses the number key of the correct image (1, 2, or 3) |
| **Multiple choice** | Clicks the correct option or presses number shortcut |
| **Word bank** | Clicks words in the correct order (handles hanzi/pinyin tokens) |
| **Typing** | Types the answer in the text field |
| **Matching pairs** | Uses keyboard shortcuts (1-5 left, 6-0 right) |
| **Listening** | Records system audio via Whisper (zh) then clicks/types answer |
| **Tap pairs** | Clicks matching pairs sequentially |
| **Tracing** | Skipped automatically |

### English for Vietnamese (`duolingo_en_for_vi.py`)

All of the above, plus:

| Type | How it answers |
|------|---------------|
| **Checkbox** | Reading comprehension — checks all correct answers |
| **Audio matching** | Matches English audio to Vietnamese text (or vice versa) |
| **Audio fill-in-the-blank** | Listens to audio, fills in the missing word |
| **Listen and type** | Listens to audio and types the full sentence |
| **Speaking** | Skipped automatically (no microphone) |

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

# Chinese for English speakers
DUO_EMAIL=your_email
DUO_PASSWORD=your_password
DUO_PROFILE_URL=your_duolingo_username

# English for Vietnamese speakers
VI_DUO_EMAIL=your_email
VI_DUO_PASSWORD=your_password
VI_DUO_PROFILE_URL=your_duolingo_username
```

## Usage

### Chinese for English speakers

```bash
python duolingo_cn_for_en.py
```

### English for Vietnamese speakers

```bash
python duolingo_en_for_vi.py
```

The agent will log in, check XP, and start completing lessons automatically. Press **Ctrl+C** at any time to stop and see your XP summary.

### Environment Variables

| Variable | Script | Description | Default |
|----------|--------|-------------|---------|
| `OPENAI_API_KEY` | Both | OpenAI API key | Required |
| `DUO_EMAIL` | CN | Duolingo email (Chinese course) | Required |
| `DUO_PASSWORD` | CN | Duolingo password (Chinese course) | Required |
| `DUO_PROFILE_URL` | CN | Duolingo username for XP tracking | Optional |
| `VI_DUO_EMAIL` | VI | Duolingo email (English course) | Required |
| `VI_DUO_PASSWORD` | VI | Duolingo password (English course) | Required |
| `VI_DUO_PROFILE_URL` | VI | Duolingo username for XP tracking | Optional |
| `DUO_JWT` | Both | Pre-authenticated JWT token (recommended for CI) | Optional |
| `MAX_LESSONS` | Both | Number of lessons to complete (0 = unlimited) | `0` |
| `HEADLESS` | Both | Run browser without UI (`true`/`false`) | `false` |

### Getting Your JWT Token

Duolingo's login API may block automated requests (reCAPTCHA, rate limits). The most reliable way to authenticate in CI is using a JWT token from your browser session.

1. Log in to [Duolingo](https://www.duolingo.com/) in your browser
2. Open the browser DevTools console (F12 > Console)
3. Run this script to copy your JWT token:

```js
document.cookie.split(';').find(cookie => cookie.includes('jwt_token')).split('=')[1]
```

4. Copy the output — that's your JWT token

> **Note:** JWT tokens expire periodically. If the agent stops working, repeat the steps above to get a fresh token.

### GitHub Actions (Daily Automated Practice)

1. Go to your repo **Settings > Secrets and variables > Actions**
2. Add these secrets:
   - `DUO_EMAIL` — your Duolingo email
   - `DUO_PASSWORD` — your Duolingo password
   - `DUO_JWT` — your JWT token (recommended, see [Getting Your JWT Token](#getting-your-jwt-token))
   - `OPENAI_API_KEY` — your OpenAI API key
   - `DUO_PROFILE_URL` — your Duolingo username (optional, for XP tracking)
3. The workflow runs daily at 7:00 AM UTC, or trigger manually from the **Actions** tab
4. Each run executes 2-3 rounds with 5-20 lessons each, with random sleep intervals between rounds

> **Note:** Listening exercises are automatically skipped in CI (no audio device). The agent will click "CAN'T LISTEN NOW" and continue.

## Tech Stack

- [Playwright](https://playwright.dev/python/) - Browser automation
- [OpenAI GPT-4o](https://platform.openai.com/docs/) - Vision model for screenshot analysis
- [OpenAI Whisper](https://platform.openai.com/docs/guides/speech-to-text) - Audio transcription for listening exercises
- [sounddevice](https://python-sounddevice.readthedocs.io/) + [soundfile](https://pysoundfile.readthedocs.io/) - System audio capture via WASAPI loopback
- [python-dotenv](https://github.com/theskumar/python-dotenv) - Environment variable management

## Project Structure

```
duolingo-ai-agent/
├── .github/
│   └── workflows/
│       └── duolingo.yml          # GitHub Actions daily practice
├── duolingo_cn_for_en.py         # Chinese for English speakers
├── duolingo_en_for_vi.py         # English for Vietnamese speakers
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment variable template
└── README.md
```
