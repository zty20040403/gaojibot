"""Modules unreachable from the given entry modules (run from the repo root).

Usage: OUT=dead.txt python tools/dev/dead_modules.py src.plugins.ai_chat.platform_entrypoint [more roots...]
Writes the dead list to $OUT and the live list to $OUT.live. Before deleting, also grep for
files read by path (vm_sandbox.py injects sandbox_pdf.py) and for tests that import a module.
"""
import ast, sys, os
from pathlib import Path
root = Path('.')
mods = {}
for p in Path('src').rglob('*.py'):
    name = '.'.join(p.with_suffix('').parts)
    if name.endswith('.__init__'): name = name[:-9]
    mods[name] = p
for p in Path('migrations').rglob('*.py'):
    mods['migrations.' + '.'.join(p.relative_to('migrations').with_suffix('').parts)] = p
def resolve(cur, node):
    out = []
    if isinstance(node, ast.Import):
        for a in node.names: out.append(a.name)
    else:
        if node.level:
            pkg = cur.split('.')
            if not str(mods[cur]).endswith('__init__.py'): pkg = pkg[:-1]
            pkg = pkg[:len(pkg)-node.level+1] if node.level>1 else pkg
            base = '.'.join(pkg + ([node.module] if node.module else []))
        else: base = node.module or ''
        out.append(base)
        for a in node.names: out.append(base + '.' + a.name)
    res = []
    for o in out:
        parts = o.split('.')
        while parts:
            n = '.'.join(parts)
            if n in mods: res.append(n); break
            parts.pop()
    return res
graph = {}
for m, p in mods.items():
    try: tree = ast.parse(p.read_text())
    except Exception: graph[m] = set(); continue
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)): deps.update(resolve(m, node))
    # parent packages
    parts = m.split('.')
    for i in range(1, len(parts)):
        if '.'.join(parts[:i]) in mods: deps.add('.'.join(parts[:i]))
    graph[m] = deps
roots = sys.argv[1:]
seen, stack = set(), [r for r in roots if r in mods]
missing=[r for r in roots if r not in mods]
while stack:
    m = stack.pop()
    if m in seen: continue
    seen.add(m); stack.extend(graph.get(m, ()))
dead = sorted(m for m in mods if m not in seen and not m.startswith('migrations'))
lines = sum(len(mods[m].read_text().splitlines()) for m in dead)
live = sum(len(mods[m].read_text().splitlines()) for m in seen if m.startswith('src'))
print('missing roots', missing)
print('live src modules', len([m for m in seen if m.startswith('src')]), 'lines', live)
print('dead modules', len(dead), 'lines', lines)
out = os.environ.get('OUT')
if out: Path(out).write_text('\n'.join(dead))
if out: Path(out+'.live').write_text('\n'.join(sorted(seen)))
