"""Drive smartex in headless Chromium: screenshots + automatic layout and error checks."""
import json, sys, time
from playwright.sync_api import sync_playwright
URL, OUT = sys.argv[1], sys.argv[2]
problems = []

def check_layout(pg, label):
    r = pg.evaluate("""() => {
      const box = s => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width}; };
      const pane = box('#editorpane'), pdf = box('#pdfpane');
      let spill = 0, widest = 0;
      for (const ln of document.querySelectorAll('.CodeMirror-line')) { const b = ln.getBoundingClientRect(); widest = Math.max(widest, b.right);
        if (pane && b.right > pane.r + 1 && b.width > 0) spill++; }
      const files = [...document.querySelectorAll('#files li')].map(li => li.textContent);
      // what is actually drawn at the centre of the right pane?
      const probe = pdf ? document.elementFromPoint((pdf.l + pdf.r) / 2, (pdf.t + pdf.b) / 2) : null;
      return {pane, pdf, spill, widest, files, probe: probe ? (probe.id || probe.className || probe.tagName) : null,
              probeInEditor: !!(probe && probe.closest('#editorpane')), docW: document.documentElement.scrollWidth, winW: innerWidth};
    }""")
    if r['spill']: problems.append(f'{label}: {r["spill"]} editor lines spill past the editor pane (widest right edge {r["widest"]:.0f}px, pane ends {r["pane"]["r"]:.0f}px)')
    if r['probeInEditor']: problems.append(f'{label}: editor content is drawn over the right pane ({r["probe"]})')
    if any('[object' in f for f in r['files']): problems.append(f'{label}: file list shows {r["files"][:2]}')
    if r['docW'] > r['winW'] + 1: problems.append(f'{label}: page scrolls horizontally ({r["docW"]}px wide in a {r["winW"]}px window)')
    return r

with sync_playwright() as p:
    b = p.chromium.launch()
    for scheme in ('dark', 'light'):
        ctx = b.new_context(viewport={'width': 1440, 'height': 900}, color_scheme=scheme)
        pg = ctx.new_page()
        errs = []
        pg.on('console', lambda m: errs.append(f'console.{m.type}: {m.text}') if m.type in ('error', 'warning') else None)
        pg.on('pageerror', lambda e: errs.append(f'pageerror: {e}'))
        pg.goto(URL); pg.wait_for_selector('#files li', timeout=20000); pg.wait_for_timeout(1500)
        r = check_layout(pg, f'{scheme} first load')
        theme = pg.evaluate("document.documentElement.dataset.theme")
        pg.screenshot(path=f'{OUT}/{scheme}_main.png')
        if scheme == 'dark':
            pg.click('#theme'); pg.wait_for_timeout(400); pg.screenshot(path=f'{OUT}/dark_after_toggle.png')
            toggled = pg.evaluate("document.documentElement.dataset.theme")
            pg.click('#theme'); pg.wait_for_timeout(200)
            pg.click('#tab-agent'); pg.wait_for_timeout(600); pg.screenshot(path=f'{OUT}/assistant_tab.png')
            check_layout(pg, 'assistant tab'); pg.click('#tab-paper')
            pg.click('#compile'); pg.wait_for_selector('#strip.ok, #strip.err', timeout=60000); pg.wait_for_timeout(800)
            strip = pg.inner_text('#strip'); pg.screenshot(path=f'{OUT}/after_compile.png')
            pg.set_viewport_size({'width': 820, 'height': 1000}); pg.wait_for_timeout(500)
            check_layout(pg, 'narrow window'); pg.screenshot(path=f'{OUT}/narrow.png', full_page=True)
            print(json.dumps(dict(theme_on_load=theme, theme_after_toggle=toggled, strip=strip, files=r['files'], probe=r['probe'])))
        else:
            print(json.dumps(dict(theme_on_load=theme)))
        for e in errs: problems.append(f'{scheme}: {e}')
        ctx.close()
    b.close()
print('PROBLEMS:' if problems else 'no problems detected')
for x in problems: print(' -', x)
