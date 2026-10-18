"""A multi-step assistant turn: the status names the current step, clicking it opens the live steps."""
import sys, json
from playwright.sync_api import sync_playwright
URL, OUT = sys.argv[1], sys.argv[2]
res = {}
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_selector('#files li'); pg.click('#tab-agent'); pg.wait_for_timeout(600)
    pg.fill('#ask', 'slow: check the paper'); pg.keyboard.press('Enter'); pg.wait_for_timeout(1800)
    res['status_mid_run'] = pg.inner_text('#agentstate')
    res['status_clickable'] = 'clickable' in (pg.get_attribute('#agentstate', 'class') or '')
    pg.click('#agentstate'); pg.wait_for_timeout(300)
    res['steps_open_after_click'] = pg.evaluate("(() => { const d = [...document.querySelectorAll('#chat li.steps details')].pop(); return !!(d && d.open); })()")
    res['summary_mid_run'] = pg.inner_text('#chat li.steps summary')
    pg.screenshot(path=f'{OUT}/steps_mid_run.png')
    pg.wait_for_timeout(3500)
    res['status_after'] = pg.inner_text('#agentstate')
    res['summary_after'] = pg.inner_text('#chat li.steps summary')
    res['step_heads'] = pg.locator('#chat .step .head').all_inner_texts()
    res['thinking_shown'] = pg.locator('#chat .step.thinking').count()
    res['error_result_shown'] = pg.locator('#chat .step pre.err').count()
    pg.screenshot(path=f'{OUT}/steps_done.png')
    pg.reload(); pg.wait_for_selector('#files li'); pg.click('#tab-agent'); pg.wait_for_timeout(1000)
    res['after_reload_summary'] = pg.inner_text('#chat li.steps summary')
    res['after_reload_heads'] = len(pg.locator('#chat .step .head').all_inner_texts())
    res['page_errors'] = errs
    b.close()
print(json.dumps(res, indent=1))
