"""Independent link control namespace; codes stay transient and out of repr."""
from dataclasses import dataclass,field
import re

_CHALLENGE = re.compile(r'LC-[0-9a-f]{32}\Z')
_LINK = re.compile(r'[0-9a-f]{32}\Z')
_CODE = re.compile(r'[0-9]{6}\Z')


@dataclass(frozen=True)
class LinkCommand:
    action: str
    reference: str = ''
    code: str = field(default='',repr=False)
    valid: bool = True


def parse_link_command(text):
    if not isinstance(text,str):return None
    leading = text.lstrip()[:32]
    prefix = leading.split(None,1)[0].casefold() if leading else ''
    if prefix not in {'/link','!link','/unlink','!unlink'}:return None
    if len(text)>512:return LinkCommand('invalid',valid=False)
    parts = text.strip().split()[1:]
    if prefix in {'/unlink','!unlink'}:
        return LinkCommand('revoke',reference=parts[0]) if len(parts)==1 and _LINK.fullmatch(parts[0]) else LinkCommand('invalid',valid=False)
    if not parts:return LinkCommand('create')
    if parts==['list']:return LinkCommand('list')
    if len(parts)==1 and _CODE.fullmatch(parts[0]):return LinkCommand('confirm',code=parts[0])
    if len(parts)==2 and parts[0] in {'status','cancel'} and _CHALLENGE.fullmatch(parts[1]):
        return LinkCommand(parts[0],reference=parts[1])
    return LinkCommand('invalid',valid=False)
