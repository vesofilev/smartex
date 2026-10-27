"""New projects: the New project button, the API's checks, the announcement to other open editors, the CLI,
the MCP tool and the registry.

    python tests/ui_projects.py URL OUT_DIR SMARTEX_CLI [REGISTRY]

URL is the editor address with ?token=. Start the server with a scratch --projects-dir (the test makes
folders there) and with at least one project that has a main file. To check that created projects are
recorded for the next start, also give the server a scratch --registry FILE and pass FILE as REGISTRY.
"""
import json, os, subprocess, sys, time, urllib.parse, uuid
from pathlib import Path
from playwright.sync_api import sync_playwright

URL, OUT, CLI = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
REGISTRY = Path(sys.argv[4]) if len(sys.argv) > 4 else None
OUT.mkdir(parents=True, exist_ok=True)
u = urllib.parse.urlparse(URL)
BASE, TOKEN = f'{u.scheme}://{u.netloc}', urllib.parse.parse_qs(u.query)['token'][0]
HERE = Path(__file__).resolve().parent.parent
tag = uuid.uuid4().hex[:6]
problems, res = [], {}
ENV = dict(os.environ, SMARTEX_URL=BASE, SMARTEX_TOKEN=TOKEN)


def cli(*args):
    r = subprocess.run([CLI, '--client', 'agent:test', *args], capture_output=True, text=True, env=ENV, timeout=60)
    return r.returncode, r.stdout, r.stderr


# the API refuses bad names and duplicates
for bad in ['', '../escape', '.hidden', 'a/b', 'x' * 65, 'with space']:
    code, _, err = cli('new-project', bad)
    if code == 0 or '"status": 400' not in err:
        problems.append(f'the name {bad!r} was not refused with 400: {err.strip()[:120]}')
code, out, err = cli('new-project', f'proj_cli_{tag}')
if code:
    problems.append(f'CLI new-project failed: {err}')
cli_path = json.loads(out)['path'] if not code else ''
res['cli_project'] = cli_path
code, _, err = cli('new-project', f'proj_cli_{tag}')
if code == 0 or '"status": 409' not in err:
    problems.append('a second project with the same name was not refused with 409')

with sync_playwright() as p:
    b = p.chromium.launch()
    errs = []
    a = b.new_page(viewport={'width': 1440, 'height': 900})
    other = b.new_page(viewport={'width': 1200, 'height': 800})
    for pg in (a, other):
        pg.on('pageerror', lambda e: errs.append(str(e)))
    a.goto(URL); a.wait_for_selector('#files li', timeout=20000); a.wait_for_timeout(800)
    start_project = a.eval_on_selector('#project', 's => s.value')
    other.goto(URL); other.wait_for_selector('#files li', timeout=20000); other.wait_for_timeout(800)
    first_text = a.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue().length")
    res['start_project'] = start_project

    # the button: prompt, create, switch to the new project, empty and locked editor
    name = f'proj_ui_{tag}'
    a.once('dialog', lambda d: d.accept(name))
    a.click('#newproject')
    try:
        a.wait_for_function(f"document.querySelector('#project').value === '{name}'", timeout=10000)
    except Exception:
        problems.append('the page did not switch to the new project')
    a.wait_for_timeout(600)
    st = a.evaluate("""() => { const cm = document.querySelector('.CodeMirror').CodeMirror;
        return { text: cm.getValue(), ro: cm.getOption('readOnly'), files: [...document.querySelectorAll('#files li')].map(l => l.textContent),
                 options: [...document.querySelectorAll('#project option')].map(o => o.value), toast: document.querySelector('#toast').textContent,
                 current: document.querySelector('#current').textContent, nopdf: getComputedStyle(document.querySelector('#nopdf')).display } }""")
    res['after_button'] = {k: v for k, v in st.items() if k != 'text'}
    if st['text']: problems.append('the editor still shows text from the previous project')
    if st['ro'] != 'nocursor': problems.append(f'the editor of an empty project is not locked (readOnly={st["ro"]!r})')
    if st['files'] != ['No files yet. Create one with New file.']: problems.append(f'file list of the new project: {st["files"]}')
    if 'Created' not in st['toast']: problems.append(f'no confirmation toast: {st["toast"]!r}')
    if f'proj_cli_{tag}' not in st['options']: problems.append('the project made with the CLI is missing from the menu')
    a.screenshot(path=str(OUT / 'project_new.png'))

    # the other editor hears about it and stays where it was
    try:
        other.wait_for_function(f"[...document.querySelectorAll('#project option')].some(o => o.value === '{name}')", timeout=8000)
    except Exception:
        problems.append('the other open editor did not get the new project in its menu')
    o = other.evaluate("({ value: document.querySelector('#project').value, toast: document.querySelector('#toast').textContent })")
    if o['value'] != start_project: problems.append(f'the other editor moved to {o["value"]}')
    # a project made by an agent is announced with its name
    code, _, err = cli('new-project', f'proj_agent_{tag}')
    try:
        other.wait_for_function(f"document.querySelector('#toast').textContent.includes('agent:test created the project proj_agent_{tag}')", timeout=8000)
    except Exception:
        problems.append('the other editor showed no notice for the project an agent created')
    other.screenshot(path=str(OUT / 'project_announced.png'))

    # the empty project becomes a working one: New file, type, compile
    a.once('dialog', lambda d: d.accept('main.tex'))
    a.click('#newfile'); a.wait_for_timeout(800)
    ro = a.evaluate("document.querySelector('.CodeMirror').CodeMirror.getOption('readOnly')")
    if ro is not False: problems.append(f'the editor stayed locked after New file (readOnly={ro!r})')
    a.click('.CodeMirror-code')
    a.keyboard.type('\\documentclass{article}\n\\begin{document}\nA new project.\n\\end{document}\n')
    a.click('#compile')
    a.wait_for_selector('#strip.ok, #strip.err', timeout=120000); a.wait_for_timeout(1500)
    res['compile'] = a.eval_on_selector('#strip', 'e => e.className')
    if res['compile'] != 'ok': problems.append(f'compiling the new project gave {res["compile"]}')
    a.screenshot(path=str(OUT / 'project_compiled.png'))

    # back to the first project: its file opens and can be edited
    a.select_option('#project', start_project); a.wait_for_timeout(1500)
    back = a.evaluate("({ n: document.querySelector('.CodeMirror').CodeMirror.getValue().length, ro: document.querySelector('.CodeMirror').CodeMirror.getOption('readOnly') })")
    if back['n'] != first_text or back['ro'] is not False: problems.append(f'after switching back: {back}, expected {first_text} characters, editable')

    # a narrow window: the header still fits
    n = b.new_page(viewport={'width': 390, 'height': 800}); n.on('pageerror', lambda e: errs.append(str(e)))
    n.goto(URL); n.wait_for_selector('#files li', timeout=20000); n.wait_for_timeout(800)
    sw = n.evaluate('document.documentElement.scrollWidth')
    if sw > 390: problems.append(f'sideways scroll at 390 px: {sw}')
    n.screenshot(path=str(OUT / 'project_narrow.png'))
    b.close()

# the MCP tool
proc = subprocess.Popen([sys.executable, str(HERE / 'smartex_mcp.py'), '--url', BASE, '--token', TOKEN, '--client', 'agent:mcp-test'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def rpc(i, method, params=None):
    proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params or {}}) + '\n'); proc.stdin.flush()
    return json.loads(proc.stdout.readline())
rpc(1, 'initialize', {'protocolVersion': '2024-11-05'})
tools = {t['name']: t for t in rpc(2, 'tools/list')['result']['tools']}
if 'create_project' not in tools: problems.append('the MCP server has no create_project tool')
elif 'project' in tools['create_project']['inputSchema']['properties']: problems.append('create_project takes a project argument')
r = rpc(3, 'tools/call', {'name': 'create_project', 'arguments': {'name': f'proj_mcp_{tag}'}})['result']
if r.get('isError'): problems.append(f'create_project over MCP failed: {r}')
r = rpc(4, 'tools/call', {'name': 'create_file', 'arguments': {'project': f'proj_mcp_{tag}', 'path': 'main.tex', 'content': '% made over MCP\n'}})['result']
if r.get('isError'): problems.append(f'create_file in the new project over MCP failed: {r}')
r = rpc(5, 'tools/call', {'name': 'create_project', 'arguments': {'name': '../x'}})['result']
if not r.get('isError'): problems.append('create_project accepted ../x')
proc.stdin.close(); proc.wait(10)
names = [x['name'] for x in json.loads(cli('projects')[1])]
for nm in (f'proj_cli_{tag}', f'proj_ui_{tag}', f'proj_agent_{tag}', f'proj_mcp_{tag}'):
    if nm not in names: problems.append(f'{nm} is not served')
mcp_main = [x for x in json.loads(cli('projects')[1]) if x['name'] == f'proj_mcp_{tag}']
if mcp_main and mcp_main[0]['main'] != 'main.tex': problems.append(f'the MCP-made project has main {mcp_main[0]["main"]}')

# the registry lists every created folder, so the next start serves them
if REGISTRY:
    listed = REGISTRY.read_text()
    for nm in (f'proj_cli_{tag}', f'proj_ui_{tag}', f'proj_agent_{tag}', f'proj_mcp_{tag}'):
        if f'/{nm}\n' not in listed: problems.append(f'{nm} is not in the registry')

problems += [f'pageerror: {e}' for e in errs]
res['problems'] = problems
print(json.dumps(res, indent=1))
sys.exit(1 if problems else 0)
