from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

MOODLE_URL = "https://moodle.epita.fr"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    context = browser.new_context(storage_state="moodle_state.json")

    page = context.new_page()
    page.goto(f"{MOODLE_URL}/my/", wait_until="networkidle")

    if "cri.epita.fr" in page.url or "/login/" in page.url:
        raise RuntimeError("La session a expiré.")

    soup = BeautifulSoup(page.content(), "html.parser")

    for link in soup.select("a[href*='/course/view.php?id=']"):
        title = link.get_text(" ", strip=True)
        url = link.get("href")
        print(title, "->", url)

    browser.close()
