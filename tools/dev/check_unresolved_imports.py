"""List src imports that no longer resolve to a module (run from the repo root).

Usage: python tools/dev/check_unresolved_imports.py [paths...]
Catches function-local imports too; files read by path (e.g. sandbox_pdf.py) are not covered.
"""
import ast, sys
from pathlib import Path
mods = {}
for p in list(Path('src').rglob('*.py')) + [Path('bot.py')]:
    n = '.'.join(p.with_suffix('').parts); n = n[:-9] if n.endswith('.__init__') else n
    mods[n] = p
def exists(n): return n in mods
roots = sys.argv[1:] or ['src', 'tests', 'bot.py', 'migrations', 'tools']
files = []
for r in roots:
    rp = Path(r); files += [rp] if rp.is_file() else list(rp.rglob('*.py'))
bad = {}
for p in files:
    cur = '.'.join(p.with_suffix('').parts); is_pkg = p.name == '__init__.py'
    if is_pkg: cur = cur[:-9]
    try: tree = ast.parse(p.read_text())
    except SyntaxError as e: bad.setdefault(str(p), []).append(f'SYNTAX {e}'); continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith('src.') and not exists(a.name): bad.setdefault(str(p), []).append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = cur.split('.') if is_pkg else cur.split('.')[:-1]
                pkg = pkg[:len(pkg)-(node.level-1)]
                base = '.'.join(pkg + ([node.module] if node.module else []))
            else: base = node.module or ''
            if not base.startswith('src'): continue
            if not exists(base): bad.setdefault(str(p), []).append(base); continue
            if mods[base].name == '__init__.py':
                for a in node.names:
                    full = base + '.' + a.name
                    if not exists(full) and a.name not in mods[base].read_text(): bad.setdefault(str(p), []).append(full)
for f, b in sorted(bad.items()): print(f, '->', ', '.join(sorted(set(x.replace('src.plugins.ai_chat.', '~')for x in b))))
print(len(bad), 'files with unresolved imports')
