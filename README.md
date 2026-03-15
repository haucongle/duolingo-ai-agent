# Duolingo AI Vision Agent

An AI-powered agent that reads Duolingo exercises directly from the screen using GPT-4o Vision. Instead of parsing HTML, the agent **takes screenshots like a human** and suggests answers in real time.

## Features

- **Playwright browser automation** - Launches a Chromium browser and handles login automatically
- **GPT-4o Vision analysis** - Sends screenshots to GPT-4o to identify questions, options, and correct answers
- **Session persistence** - Saves login session to avoid re-authentication on subsequent runs
- **No HTML parsing** - Works purely through visual analysis, making it resilient to UI changes

## Demo

![demo](demo.gif)

## How It Works

1. The agent opens Duolingo in a browser and logs in (or loads a saved session)
2. You navigate to a lesson manually and press Enter
3. The agent captures screenshots every 5 seconds
4. Each screenshot is sent to GPT-4o Vision, which identifies:
   - The question being asked
   - The available answer options
   - The most likely correct answer

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

Once Duolingo loads, start a lesson manually, then press **Enter** in the terminal to begin AI analysis.

## Tech Stack

- [Playwright](https://playwright.dev/python/) - Browser automation
- [OpenAI GPT-4o](https://platform.openai.com/docs/) - Vision model for screenshot analysis
- [python-dotenv](https://github.com/theskumar/python-dotenv) - Environment variable management
