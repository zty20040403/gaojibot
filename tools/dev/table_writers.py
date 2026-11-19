"""Find literal INSERT/UPDATE targets passed to the host's SQL cursors."""
import ast
from pathlib import Path
import re


TARGET = re.compile(r'^\s*(?i:INSERT\s+INTO|UPDATE)\s+(?:"([A-Za-z_]\w*)"|([A-Za-z_]\w*))(?=[\s(])')


def writer_tables(source: Path) -> set[str]:
    tables = set()
    for file in source.rglob('*.py'):
        for call in ast.walk(ast.parse(file.read_text(), filename=str(file))):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr in {'execute', 'executemany'} and call.args):
                continue
            sql = call.args[0]
            while isinstance(sql, ast.BinOp) and isinstance(sql.op, ast.Add):
                sql = sql.left
            if isinstance(sql, ast.JoinedStr) and sql.values:
                sql = sql.values[0]
            if isinstance(sql, ast.Constant) and isinstance(sql.value, str) and (match := TARGET.match(sql.value)):
                tables.add(match[1] or match[2].lower())
    return tables
