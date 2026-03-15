import base64
import os
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from openai import OpenAI

load_dotenv()

EMAIL = os.getenv("DUO_EMAIL")
PASSWORD = os.getenv("DUO_PASSWORD")

SESSION_FILE = "duo_session.json"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PROMPT = """
Look at this Duolingo screen.

Describe:
1. What the question is
2. What the possible answers are
3. What the likely correct answer is
"""


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
                        "image_url": f"data:image/jpeg;base64,{b64}"
                    }
                ]
            }
        ]
    )

    return r.output_text


def login_duolingo(page):

    page.goto("https://www.duolingo.com/log-in")

    page.wait_for_load_state("domcontentloaded")

    email_input = page.locator('#web-ui1')
    password_input = page.locator('#web-ui2')

    email_input.wait_for(timeout=20000)

    email_input.fill(EMAIL)
    password_input.fill(PASSWORD)

    page.locator('button:has-text("Log in")').click()

    page.wait_for_timeout(5000)

    print("Login step done")


def main():

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        # nếu có session thì load
        if os.path.exists(SESSION_FILE):

            print("Loading saved session...")

            context = browser.new_context(
                viewport={"width":1280,"height":800},
                storage_state=SESSION_FILE,
                user_agent = "Mozilla/5.0"
            )

        else:

            print("No session found → logging in")

            context = browser.new_context(
                viewport={"width":1280,"height":800}
            )

        page = context.new_page()

        # nếu chưa có session thì login
        if not os.path.exists(SESSION_FILE):

            login_duolingo(page)

            print("Saving session...")

            context.storage_state(path=SESSION_FILE)

        page.goto("https://www.duolingo.com/learn")

        input("Start a lesson manually, then press ENTER")

        while True:
            print("Capturing screen...")

            img = page.screenshot(type="jpeg", quality=70)

            print("Sending to AI...")

            analysis = analyze_screen(img)

            print("\n--- AI ANALYSIS ---")
            print(analysis)

            time.sleep(5)


main()