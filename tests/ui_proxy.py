"""smartex behind a reverse proxy: sign-in, cookie rules, refusals, and the editor working under a sub-path.

    python tests/ui_proxy.py http://127.0.0.1:PUBLIC_PORT OUT_DIR [NON_LOOPBACK_IP]

PUBLIC_PORT is the server's --public listener. The script runs its own small proxy that behaves like the
nginx block in README.md: it serves smartex under /smartex/, strips that prefix, and sends Host,
X-Forwarded-Proto and X-Forwarded-Prefix. Start the server with an --allow-from that does NOT include
NON_LOOPBACK_IP (an address of this machine other than 127.0.0.1); the script then checks that a direct
connection from that address is refused. The token is read from ~/.smartex_token.
"""
import http.client
import http.server
import json
import re
import sys
import threading
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

UPSTREAM, OUT = sys.argv[1].rstrip('/'), Path(sys.argv[2])
NON_LOOPBACK = sys.argv[3] if len(sys.argv) > 3 else None
TOKEN = (Path.home() / '.smartex_token').read_text().strip()
OUT.mkdir(parents=True, exist_ok=True)
up = urllib.parse.urlparse(UPSTREAM)
problems, passed = [], []


def check(cond, what):
    (passed if cond else problems).append(what)


class Proxy(http.server.BaseHTTPRequestHandler):
    """Like `location /smartex/ { proxy_pass http://upstream/; proxy_set_header X-Forwarded-Prefix /smartex; ... }`."""
    proto = 'http'

    def log_message(self, *a):
        pass

    def relay(self):
        if self.path == '/smartex':
            self.send_response(301); self.send_header('Location', '/smartex/'); self.send_header('Content-Length', '0'); self.end_headers()
            return
        if not self.path.startswith('/smartex/'):
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()
            return
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('connection', 'keep-alive')}
        headers.update({'X-Forwarded-Proto': self.proto, 'X-Forwarded-Prefix': '/smartex',
                        'X-Real-IP': self.client_address[0], 'X-Forwarded-For': self.client_address[0]})
        c = http.client.HTTPConnection(up.hostname, up.port, timeout=120)
        c.request(self.command, self.path[len('/smartex'):], body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ('transfer-encoding', 'connection', 'content-length', 'server', 'date'):
                self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = relay


proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
proxy.daemon_threads = True
threading.Thread(target=proxy.serve_forever, daemon=True).start()
PX = f'http://127.0.0.1:{proxy.server_address[1]}'
px_host = f'127.0.0.1:{proxy.server_address[1]}'


def req(method, path, body=None, headers=None, base=PX, source=None):
    u = urllib.parse.urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=60, source_address=(source, 0) if source else None)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


# ---------------------------------------------------------------- HTTP rules
s, h, b = req('GET', '/smartex/')
check(s == 200 and b'Sign in' in b, f'unsigned page request shows the sign-in page (got {s})')
s, h, b = req('GET', '/smartex/api/files')
check(s == 403, f'unsigned API request is refused (got {s})')
s, h, b = req('POST', '/smartex/login', b'token=wrong', {'Content-Type': 'application/x-www-form-urlencoded'})
check(s == 403 and b'not right' in b, f'wrong token gives the sign-in page with an error (got {s})')
check('Set-Cookie' not in h, 'wrong token sets no cookie')
s, h, b = req('POST', '/smartex/login', urllib.parse.urlencode({'token': TOKEN}).encode(),
              {'Content-Type': 'application/x-www-form-urlencoded'})
cookie = h.get('Set-Cookie', '')
check(s == 303 and h.get('Location') == './', f'right token redirects to ./ (got {s}, {h.get("Location")})')
check('Path=/smartex/' in cookie and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie,
      f'cookie is HttpOnly, SameSite=Strict and scoped to /smartex/ ({cookie.split(";", 1)[-1].strip()})')
check('Secure' not in cookie, 'cookie is not Secure over plain http')
# over https (as nginx sends it) the cookie must be Secure; ask the upstream directly with the proxy's headers
s, h2, b = req('POST', '/login', urllib.parse.urlencode({'token': TOKEN}).encode(),
               {'Content-Type': 'application/x-www-form-urlencoded', 'X-Forwarded-Proto': 'https',
                'X-Forwarded-Prefix': '/smartex'}, base=UPSTREAM)
check('Secure' in h2.get('Set-Cookie', ''), 'cookie is Secure when the proxy says https')
s, h2, b = req('POST', '/login', urllib.parse.urlencode({'token': TOKEN}).encode(),
               {'Content-Type': 'application/x-www-form-urlencoded', 'X-Forwarded-Prefix': '/a;b'}, base=UPSTREAM)
check('Path=/;' in h2.get('Set-Cookie', ''), 'a malformed prefix falls back to Path=/')
jar = cookie.split(';', 1)[0]
s, h, b = req('GET', '/smartex/api/files', headers={'Cookie': jar})
check(s == 200, f'signed-in API read works (got {s})')
pres = json.dumps({'path': 'main.tex', 'line': 1}).encode()
s, h, b = req('POST', '/smartex/api/presence', pres, {'Cookie': jar, 'Content-Type': 'application/json'})
check(s == 403, f'signed-in POST without X-Client is refused (got {s})')
s, h, b = req('POST', '/smartex/api/presence', pres, {'Cookie': jar, 'Content-Type': 'application/json',
                                                      'X-Client': 'human:test', 'Origin': 'https://evil.example'})
check(s == 403, f'signed-in POST from another origin is refused (got {s})')
s, h, b = req('POST', '/smartex/api/presence', pres, {'Cookie': jar, 'Content-Type': 'application/json',
                                                      'X-Client': 'human:test', 'Origin': PX})
check(s == 200, f'signed-in POST from the page itself works (got {s})')
s, h, b = req('GET', '/smartex/api/files', headers={'X-Token': TOKEN})
check(s == 200, f'agents with X-Token still work through the proxy (got {s})')
check(h.get('X-Frame-Options') == 'DENY' and h.get('Referrer-Policy') == 'no-referrer', 'framing and referrer headers are set')
if NON_LOOPBACK:
    try:
        s, h, b = req('GET', '/api/files', headers={'X-Token': TOKEN}, base=f'http://{NON_LOOPBACK}:{up.port}', source=NON_LOOPBACK)
        check(s == 403, f'direct connection from {NON_LOOPBACK}, not in --allow-from, is refused even with the token (got {s})')
    except OSError as e:
        problems.append(f'could not test the allow-list from {NON_LOOPBACK}: {e}')

# ---------------------------------------------------------------- the browser, through the proxy
errors = []
with sync_playwright() as p:
    br = p.chromium.launch()
    ctx = br.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
    pg = ctx.new_page()
    # the deliberate wrong-token attempt below answers 403 on purpose; Chromium logs that page load, nothing else may fail
    pg.on('console', lambda m: errors.append(f'console.{m.type}: {m.text} ({m.location.get("url")})')
          if m.type == 'error' and not (m.location.get('url') or '').endswith('/smartex/login') else None)
    pg.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    api_paths = []
    pg.on('request', lambda r: api_paths.append(urllib.parse.urlparse(r.url).path) if '/api/' in r.url else None)
    pg.goto(PX + '/smartex')
    check(pg.url.endswith('/smartex/'), f'bare /smartex redirects to /smartex/ ({pg.url})')
    pg.wait_for_selector('input[name=token]')
    pg.screenshot(path=str(OUT / 'proxy_signin.png'))
    pg.fill('input[name=token]', 'wrong')
    pg.click('button[type=submit]')
    pg.wait_for_selector('.err')
    check('not right' in pg.inner_text('.err'), 'the browser shows the wrong-token message')
    pg.fill('input[name=token]', TOKEN)
    pg.click('button[type=submit]')
    pg.wait_for_selector('#files li', timeout=20000)
    check(pg.url == PX + '/smartex/', f'after sign-in the address has no token ({pg.url})')
    files = pg.eval_on_selector_all('#files li', 'els => els.map(e => e.textContent.trim())')
    check(len(files) > 0 and not any('[object' in f for f in files), f'file list loads ({len(files)} files)')
    pg.click('#compile')
    pg.wait_for_selector('#strip.ok, #strip.err', timeout=120000)
    pg.wait_for_timeout(1500)
    loaded = pg.evaluate("""() => [...document.querySelectorAll('#pages img')].slice(0, 2)
                              .map(i => i.complete && i.naturalWidth > 0)""")
    check(loaded and all(loaded), f'PDF pages render through the proxy ({loaded})')
    href = pg.get_attribute('#openpdf', 'href') or ''
    check(href.startswith('api/pdf?') and 'token=' not in href, f'"Open PDF" link is relative and has no token ({href[:40]})')
    r = pg.request.get(PX + '/smartex/' + href)
    check(r.status == 200 and r.headers.get('content-type', '').startswith('application/pdf'), f'"Open PDF" downloads the PDF ({r.status})')
    pg.screenshot(path=str(OUT / 'proxy_editor.png'))
    pg.click('#tab-agent')
    pg.wait_for_timeout(1500)
    pg.screenshot(path=str(OUT / 'proxy_assistant.png'))
    bad = sorted({a for a in api_paths if not a.startswith('/smartex/api/')})
    check(not bad, f'every API request stays under /smartex/ ({len(api_paths)} requests{", outside: " + ", ".join(bad) if bad else ""})')
    pg.reload()
    pg.wait_for_selector('#files li', timeout=20000)
    check(True, 'a reload stays signed in (cookie)')
    ctx.close(); br.close()
problems += errors

print(json.dumps(dict(passed=len(passed), failed=len(problems)), indent=None))
for x in passed:
    print('  ok  ', x)
for x in problems:
    print('  FAIL', x)
proxy.shutdown()
sys.exit(1 if problems else 0)
