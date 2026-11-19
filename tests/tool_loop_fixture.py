from types import SimpleNamespace as NS


def tool(name, **schema):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object', **schema}}}


def batch(*names, start=0):
    calls = [NS(id=f'call-{i}', function=NS(name=name, arguments='{}')) for i, name in enumerate(names, start)]
    return NS(choices=[NS(message=NS(content='', tool_calls=calls))])


def final(text='done'):
    return NS(choices=[NS(message=NS(content=text, tool_calls=[]))])
