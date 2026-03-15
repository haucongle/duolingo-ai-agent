# Duolingo AI Vision Agent

An AI-powered agent that automatically solves Duolingo exercises using GPT-4o Vision. Instead of parsing HTML, the agent **takes screenshots like a human**, understands the question, and **clicks/types the answer automatically**.

## Features

- **Fully automatic answering** - AI detects the question type and interacts with the page to answer
- **Supports multiple exercise types** - Multiple choice, word bank, typing, matching, listening
- **Playwright browser automation** - Launches a Chromium browser and handles login automatically
- **GPT-4o Vision analysis** - Sends screenshots to GPT-4o to identify questions and determine correct answers
- **Session persistence** - Saves login session to avoid re-authentication on subsequent runs
- **No HTML parsing** - Works purely through visual analysis, making it resilient to UI changes
- **Smart button handling** - Automatically clicks Check, Continue, and Skip buttons
- **Error recovery** - Handles invalid AI responses and unexpected states gracefully

## Demo

![demo](demo.gif)

## How It Works

1. The agent opens Duolingo in a browser and logs in (or loads a saved session)
2. You navigate to a lesson manually and press Enter
3. The agent enters a fully automatic loop:
   - Captures a screenshot of the current screen
   - Sends it to GPT-4o Vision, which returns a structured JSON response with:
     - The exercise type (multiple choice, word bank, typing, etc.)
     - The correct answer
     - The exact actions to perform (click targets, text to type)
   - Executes the actions on the page (clicking words, typing answers)
   - Clicks Check/Continue buttons to proceed to the next question
4. Repeats until the lesson is complete

## Supported Exercise Types

| Type | How it answers |
|------|---------------|
| **Multiple choice** | Clicks the correct option |
| **Word bank** | Clicks words in the correct order |
| **Typing** | Types the answer in the text field |
| **Matching / Tap pairs** | Clicks matching pairs |
| **Listening** | Types what was said |

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
```

## Usage

```bash
python duolingo.py
```

On first run, the agent will log in and save the session. On subsequent runs, it reuses the saved session.

Once Duolingo loads, start a lesson manually, then press **Enter** in the terminal. The AI will take over and solve the exercises automatically.

Press **Ctrl+C** at any time to stop the agent.

## Tech Stack

- [Playwright](https://playwright.dev/python/) - Browser automation
- [OpenAI GPT-4o](https://platform.openai.com/docs/) - Vision model for screenshot analysis
- [python-dotenv](https://github.com/theskumar/python-dotenv) - Environment variable management
