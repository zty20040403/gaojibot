"""Wake-only admission: capture stop/budget epochs before guest or body waits."""
from ... import daily_budgets
from .program_actions import ProgramDenied


class WakeAdmission:
    def __init__(self,actions):
        self.actions,self.sessions,self.transaction,self.clock = actions,actions.sessions,actions.transaction,actions.clock

    def _capture(self,cursor,current):
        stop = self.actions._capture(cursor,current)
        try:budget = daily_budgets.require(cursor,current.endpoint,now=int(self.clock()))
        except PermissionError as error:raise ProgramDenied(str(error)) from None
        return stop,budget

    def authenticate(self,token):
        with self.transaction() as cursor:
            current = self.sessions.authenticate_cursor(cursor,token)
            return current,self._capture(cursor,current)

    def capture(self,current):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            return self._capture(cursor,current)

    def check(self,cursor,current,generation):
        if type(generation) is not tuple or len(generation)!=2 or any(type(value) is not int or value<0 for value in generation):
            raise ProgramDenied('invalid_program_generation',400)
        if generation[0]!=self.actions._capture(cursor,current):
            raise ProgramDenied('conversation_stopped_or_replaced')
        try:daily_budgets.require(cursor,current.endpoint,now=int(self.clock()),generation=generation[1])
        except PermissionError as error:raise ProgramDenied(str(error)) from None

    def require_generation(self,current,generation):
        with self.transaction() as cursor:
            self.sessions.check(cursor,current)
            self.check(cursor,current,generation)
