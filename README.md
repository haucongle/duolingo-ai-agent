# Duolingo AI Vision Agent

AI reads Duolingo screen using GPT vision.

## Features

- Playwright browser automation
- Screenshot → GPT vision analysis
- Auto session save
- No HTML parsing

## Setup

Install dependencies

pip install -r requirements.txt

Install playwright browser

playwright install

Create `.env`

OPENAI_API_KEY=...
DUO_EMAIL=...
DUO_PASSWORD=...

Run

python duolingo.py