"""Public committer identity only. GitHub credentials belong to host APIs."""
from dataclasses import dataclass
import re
import shlex


@dataclass(frozen=True)
class GitHubIdentity:
    login: str
    user_id: int

    @classmethod
    def parse(cls, raw):
        if type(raw) is not dict or set(raw)!={'login','user_id'}:
            raise ValueError('invalid_github_identity')
        login,number = raw['login'],raw['user_id']
        if (type(login) is not str or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?',login)
                or type(number) is not int or not 1<=number<2**63):
            raise ValueError('invalid_github_identity')
        return cls(login,number)

    @property
    def email(self):
        return f'{self.user_id}+{self.login}@users.noreply.github.com'

    @property
    def script(self):
        return ('set -eu\ngit config --system user.name '+shlex.quote(self.login)+
            '\ngit config --system user.email '+shlex.quote(self.email)+'\n')
