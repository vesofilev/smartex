import sys, json, urllib.request
from playwright.sync_api import sync_playwright
URL, OUT = sys.argv[1], sys.argv[2]; TOK = URL.split('token=')[1]
def apiget(path):
    req = urllib.request.Request(URL.split('/?')[0] + path, headers={'X-Token': TOK}); return json.load(urllib.request.urlopen(req))
res = {}
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(URL); pg.wait_for_selector('#files li'); pg.wait_for_timeout(800)
    res['nopdf_before_compile'] = pg.is_visible('#nopdf')
    # cursor deep in the Kerr subsection of the continuation file, then compile
    pg.click('#files li:has-text("ernst_chapter_continuation.tex")'); pg.wait_for_timeout(400)
    target = pg.evaluate("""() => { const cm = document.querySelector('.CodeMirror').CodeMirror; let L = 0;
        cm.eachLine(h => { if (/The Kerr solution in these variables/.test(h.text)) L = cm.getLineNumber(h); });
        cm.setCursor({line: L + 6, ch: 0}); return L + 7; }""")
    pg.click('#compile'); pg.wait_for_selector('#strip.ok, #strip.err', timeout=60000); pg.wait_for_timeout(2500)
    sync = apiget(f'/api/synctex/view?path=ernst_chapter_continuation.tex&line={target}')
    vis = pg.evaluate("""() => { const box = document.querySelector('#sheet'); const mid = box.scrollTop + box.clientHeight * 0.3;
        for (const d of document.querySelectorAll('.page')) if (d.offsetTop <= mid && mid <= d.offsetTop + d.offsetHeight) return {page: +d.dataset.page, scrollTop: Math.round(box.scrollTop)};
        return {page: null, scrollTop: Math.round(box.scrollTop)}; }""")
    res['cursor_line'] = target; res['synctex_says_page'] = sync['page']; res['viewer_shows_page'] = vis['page']; res['scrollTop'] = vis['scrollTop']
    res['pages_rendered'] = pg.locator('.page').count()
    res['marker_shown'] = pg.locator('.page .marker.show').count()
    pg.screenshot(path=f'{OUT}/sync_after_compile.png')
    # inverse search: click the title on page 1
    pg.evaluate("document.querySelector('#sheet').scrollTop = 0"); pg.wait_for_timeout(300)
    box = pg.locator('.page[data-page="1"]').bounding_box()
    pg.mouse.click(box['x'] + box['width'] * 0.5, box['y'] + box['height'] * 0.085); pg.wait_for_timeout(800)
    res['click_title_opens'] = pg.inner_text('#current'); res['click_title_pos'] = pg.inner_text('#pos')
    # Ctrl+J from the new cursor position (main.tex title) should scroll back near the top of page 1
    pg.evaluate("document.querySelector('#sheet').scrollTop = 99999"); pg.click('.CodeMirror'); pg.keyboard.press('Control+j'); pg.wait_for_timeout(500)
    res['after_ctrl_j_scrollTop'] = pg.evaluate("Math.round(document.querySelector('#sheet').scrollTop)")
    pg.click('#zoomin'); pg.wait_for_timeout(800); res['zoom_label'] = pg.inner_text('#zoomlevel')
    pg.click('#zoomout'); pg.wait_for_timeout(500)
    res['page_errors'] = errs
    b.close()
print(json.dumps(res, indent=1))
