"""Send messages through the Assistant composer (with tests/fake_claude.py as the assistant) and count entries."""
import sys, json
from playwright.sync_api import sync_playwright
URL, OUT = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_selector('#files li'); pg.click('#tab-agent'); pg.wait_for_timeout(800)
    for text in ('first question about $F$', 'second question'):
        pg.fill('#ask', text); pg.keyboard.press('Enter'); pg.wait_for_timeout(2500)
    users = pg.locator('#chat li.user').all_inner_texts()
    replies = pg.locator('#chat li.assistant').count()
    pg.reload(); pg.wait_for_selector('#files li'); pg.click('#tab-agent'); pg.wait_for_timeout(1200)
    after_reload = pg.locator('#chat li.user').count()
    pg.screenshot(path=f'{OUT}/assistant_chat.png')
    print(json.dumps(dict(user_entries=len(users), first_lines=[u.split('\n')[0] for u in users], assistant_replies=replies,
                          user_entries_after_reload=after_reload, state=pg.inner_text('#agentstate'), page_errors=errs), indent=1))
    b.close()
