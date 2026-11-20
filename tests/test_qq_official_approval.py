"""Retired approval input remains isolated from ordinary chat."""
import unittest

from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.adapters.qq_official.input_normalization import normalize_input, retired_approval_input


class InputPrivacyTests(unittest.TestCase):
 def test_trusted_mentions_and_embedded_approval_privacy(self):
  cases=[('123456',[],'control'),('<@!TEST_FAKE> 123456',[],'quarantine'),('/confirm AP-ABCDEF123456 123456',[],'control'),
   ('<@!TEST_BOT> /confirm AP-ABCDEF123456 123456',[{'id':'TEST_BOT','is_you':True}],'control'),
   ('<@!TEST_BOT> /confirm AP-ABCDEF123456 123456',[],'quarantine'),
   ('quoted /confirm AP-ABCDEF123456 123456',[],'quarantine'),('ordinary chat',[],None)]
  for text,mentions,kind in cases:
   with self.subTest(kind=kind):
    dispatch=parse_dispatch({'op':0,'t':'C2C_MESSAGE_CREATE','s':1,'d':{'id':'TEST_MESSAGE','content':text,
       'author':{'user_openid':'TEST_OWNER'},'mentions':mentions}})
    self.assertEqual(retired_approval_input(normalize_input(dispatch),text,private=True),kind)
  dispatch=parse_dispatch({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':1,'d':{'id':'TEST_MESSAGE',
    'group_openid':'TEST_GROUP','content':'/confirm AP-ABCDEF123456 123456','author':{'member_openid':'TEST_MEMBER'}}})
  self.assertEqual(retired_approval_input(normalize_input(dispatch),dispatch.message.content,private=False),'quarantine')
