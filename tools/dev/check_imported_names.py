"""List `from src.x import name` where module x no longer defines name (run from the repo root).

Usage: python tools/dev/check_imported_names.py [paths...]
"""
import ast, sys
from pathlib import Path
mods={}
for p in list(Path('src').rglob('*.py')):
    n='.'.join(p.with_suffix('').parts); n=n[:-9] if n.endswith('.__init__') else n; mods[n]=p
defs={}
def topnames(m):
    if m in defs: return defs[m]
    t=ast.parse(mods[m].read_text()); names=set(); star=False
    for node in t.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): names.add(node.name)
        elif isinstance(node,ast.Assign):
            for tg in node.targets:
                for e in ast.walk(tg):
                    if isinstance(e,ast.Name): names.add(e.id)
        elif isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name): names.add(node.target.id)
        elif isinstance(node,ast.TypeAlias): names.add(node.name.id)
        elif isinstance(node,(ast.Import,ast.ImportFrom)):
            for a in node.names: names.add((a.asname or a.name).split('.')[0])
        elif isinstance(node,(ast.If,ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub,(ast.FunctionDef,ast.ClassDef)): names.add(sub.name)
                if isinstance(sub,(ast.Import,ast.ImportFrom)):
                    for a in sub.names: names.add((a.asname or a.name).split('.')[0])
                if isinstance(sub,ast.Assign):
                    for tg in sub.targets:
                        if isinstance(tg,ast.Name): names.add(tg.id)
        if isinstance(node,ast.FunctionDef) and node.name=='__getattr__': star=True
    defs[m]=(names,star); return defs[m]
roots=sys.argv[1:] or ['src']
files=[]
for r in roots:
    rp=Path(r); files+= [rp] if rp.is_file() else list(rp.rglob('*.py'))
for p in files:
    cur='.'.join(p.with_suffix('').parts); pkg=p.name=='__init__.py'
    if pkg: cur=cur[:-9]
    for node in ast.walk(ast.parse(p.read_text())):
        if not isinstance(node,ast.ImportFrom): continue
        if node.level:
            parts=cur.split('.') if pkg else cur.split('.')[:-1]
            parts=parts[:len(parts)-(node.level-1)]
            base='.'.join(parts+([node.module] if node.module else []))
        else: base=node.module or ''
        if base not in mods: continue
        names,star=topnames(base)
        for a in node.names:
            if a.name=='*' or star: continue
            if a.name not in names and (base+'.'+a.name) not in mods:
                print(f'{p}:{node.lineno}: {base.replace("src.plugins.ai_chat.","~")} has no {a.name}')
