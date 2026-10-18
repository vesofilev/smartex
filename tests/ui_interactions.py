"""Interaction checks: agent edits reload live, notes appear, errors are clickable, conflicts are caught."""
import json, subprocess, sys, os
from playwright.sync_api import sync_playwright
URL, OUT, CLI = sys.argv[1], sys.argv[2], sys.argv[3]
env = dict(os.environ, SMARTEX_URL=URL.split('/?')[0], SMARTEX_CLIENT='agent:uitest')
cli = lambda *a, stdin=None: subprocess.run([CLI, *a], env=env, capture_output=True, text=True, input=stdin)
res = {}
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_selector('#files li'); pg.wait_for_timeout(1200)
    pg.click('#files li:has-text("introduction.tex")'); pg.wait_for_timeout(500)
    res['pos_on_open'] = pg.inner_text('#pos')
    # 1. an agent edits the open file -> editor reloads, toast names the agent
    cli('edit', 'introduction.tex', '--old', 'It is well known', '--new', 'It is widely known')
    pg.wait_for_timeout(2500)
    res['live_reload'] = 'It is widely known' in pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
    res['toast'] = pg.inner_text('#toast') if pg.is_visible('#toast') else None
    # 2. an agent leaves a note -> margin marker + sidebar entry
    cli('note', 'introduction.tex', '3', 'Consider citing the universal approximation theorem here.')
    pg.wait_for_timeout(2500)
    res['note_marker'] = pg.locator('.note-mark').count(); res['note_sidebar'] = pg.locator('#notes li .where').all_inner_texts()
    pg.screenshot(path=f'{OUT}/interact_note.png')
    # 3. a person types (unsaved) and an agent edits the same file -> conflict banner, no silent overwrite
    pg.click('.CodeMirror'); pg.keyboard.press('Control+End'); pg.keyboard.type(' % my unsaved edit')
    cli('edit', 'introduction.tex', '--old', 'It is widely known', '--new', 'It is commonly known')
    pg.wait_for_timeout(2500)
    res['conflict_banner'] = pg.is_visible('#banner'); res['banner_text'] = pg.inner_text('#bannertext') if res['conflict_banner'] else None
    pg.screenshot(path=f'{OUT}/interact_conflict.png')
    pg.click('#reloaddisk'); pg.wait_for_timeout(800)
    # 4. a LaTeX error -> clickable issue that jumps to the line
    cli('edit', 'ernst_chapter.tex', '--old', 'If one defines:', '--new', 'If one defines: \\undefinedmacro')
    pg.click('#compile'); pg.wait_for_selector('#strip.ok, #strip.err', timeout=60000); pg.wait_for_timeout(600)
    res['strip_after_error'] = pg.inner_text('#strip'); issues = pg.locator('#issues li.error')
    res['error_items'] = issues.all_inner_texts()[:2]
    if issues.count():
        issues.first.click(); pg.wait_for_timeout(600)
        res['after_click_file'] = pg.inner_text('#current'); res['after_click_pos'] = pg.inner_text('#pos')
    pg.screenshot(path=f'{OUT}/interact_error.png')
    res['page_errors'] = errs
    b.close()
print(json.dumps(res, indent=1))
