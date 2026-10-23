"""Keyboard save and compile: one request each, no false conflict.

    python tests/ui_keys.py URL OUT_DIR       (URL with ?token=; the test edits and restores the main file)

Cmd/Ctrl-S used to be handled by both the editor and the page, so two saves raced: the second carried the old
sha, the server refused it, and the page showed "changed on disk". The same double handling hit Cmd/Ctrl-Enter.
"""
import json, sys
from playwright.sync_api import sync_playwright
URL, OUT = sys.argv[1], sys.argv[2]
problems, res = [], {}
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={'width': 1440, 'height': 900})
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    reqs = []
    pg.on('request', lambda r: reqs.append((r.method, r.url.split('?')[0].split('/api/')[-1])) if '/api/' in r.url else None)
    statuses = []
    pg.on('response', lambda r: statuses.append((r.request.method, r.url.split('?')[0].split('/api/')[-1], r.status)) if '/api/' in r.url else None)
    pg.goto(URL); pg.wait_for_selector('#files li', timeout=20000); pg.wait_for_timeout(1000)
    for attempt in range(3):
        pg.click('.CodeMirror-code'); pg.keyboard.press('Control+End'); pg.keyboard.type(f'\n% key test {attempt}')
        n0 = len(reqs)
        pg.keyboard.press('Control+s'); pg.wait_for_timeout(1500)
        saves = [r for r in reqs[n0:] if r == ('POST', 'file')]
        if len(saves) != 1: problems.append(f'Ctrl-S in the editor sent {len(saves)} saves (attempt {attempt})')
    banner = pg.evaluate("document.querySelector('#banner').classList.contains('show')")
    if banner: problems.append('the conflict banner is showing after saving')
    conflicts = [s for s in statuses if s[2] == 409]
    if conflicts: problems.append(f'{len(conflicts)} requests were refused with 409')
    pg.keyboard.type('\n% key test compile')
    n0 = len(reqs)
    pg.keyboard.press('Control+Enter'); pg.wait_for_selector('#strip.ok, #strip.err', timeout=120000); pg.wait_for_timeout(1500)
    compiles = [r for r in reqs[n0:] if r == ('POST', 'compile')]
    saves = [r for r in reqs[n0:] if r == ('POST', 'file')]
    if len(compiles) != 1: problems.append(f'Ctrl-Enter in the editor started {len(compiles)} compiles')
    if len(saves) != 1: problems.append(f'Ctrl-Enter saved {len(saves)} times')
    # outside the editor the page's own handler must still work
    pg.click('#files'); n0 = len(reqs)
    pg.keyboard.press('Control+Enter'); pg.wait_for_timeout(300); pg.wait_for_selector('#strip.ok, #strip.err', timeout=120000)
    pg.wait_for_timeout(800)
    if [r for r in reqs[n0:] if r == ('POST', 'compile')].__len__() != 1: problems.append('Ctrl-Enter outside the editor did not compile once')
    # restore the file: remove the test lines and save
    pg.click('.CodeMirror-code')
    pg.evaluate("""() => { const cm = document.querySelector('.CodeMirror').CodeMirror;
                          cm.setValue(cm.getValue().split('\\n').filter(l => !l.startsWith('% key test')).join('\\n')); }""")
    pg.keyboard.press('Control+s'); pg.wait_for_timeout(1200)
    res['banner_after_restore'] = pg.evaluate("document.querySelector('#banner').classList.contains('show')")
    pg.screenshot(path=f'{OUT}/keys.png')
    b.close()
problems += [f'pageerror: {e}' for e in errs]
res['problems'] = problems
print(json.dumps(res, indent=1))
sys.exit(1 if problems else 0)
