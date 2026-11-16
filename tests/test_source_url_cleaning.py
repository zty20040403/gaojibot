"""Source URL cleaning must not leak tracking IDs or alter fetch authorization."""
import unittest

from src.plugins.ai_chat.web_search import _citation_index, _code_fences, clean_source_url


class SourceURLCleaningTests(unittest.TestCase):
    def test_social_domains_and_dns_root_dot_keep_only_page_and_time(self):
        for host in ('www.bilibili.com', 'WWW.BILIBILI.COM.', 'www.bilibili.com%2E',
                     'www%2ebilibili.com', 'www。bilibili.com', 'sub.b23.tv', 'douyin.com'):
            with self.subTest(host=host):
                url = f'https://{host}:8443/video/TEST?buvid=TEST&mid=TEST&share_id=TEST&p=2&t=3#TEST'
                self.assertEqual(clean_source_url(url), f'https://{host}:8443/video/TEST?p=2&t=3#TEST')
        self.assertEqual(clean_source_url('https://bilibili.com.evil.test/?buvid=TEST'),
                         'https://bilibili.com.evil.test/?buvid=TEST')

    def test_code_spans_match_fence_type_length_and_closing_line(self):
        samples = [
            '```python\ns = "```"\nx = items[2]\n```\n',
            '````python\n```\nx = items[2]\n````\n',
            '~~~python\nx = items[2]\n~~~\n',
            '  ```python\r\nx = items[2]\r\n  ```\r\n',
        ]
        for block in samples:
            with self.subTest(block=block):
                text = block + '正文[3]。'
                self.assertEqual([text[start:end] for start,end in _code_fences(text)], [block])
        text = '代码 ```python\nitems[2]\n``` 后面[3]。'
        self.assertEqual([text[start:end] for start,end in _code_fences(text)], ['```python\nitems[2]\n```'])
        text = '正文[3]\n```python\nitems[2]\n'
        self.assertEqual([text[start:end] for start,end in _code_fences(text)], ['```python\nitems[2]\n'])

    def test_citation_index_is_bounded_before_integer_conversion(self):
        self.assertEqual(_citation_index('9'*5000, 3), 0)
        self.assertEqual(_citation_index('0'*5000+'1', 3), 1)
        self.assertEqual(_citation_index('٠'*5000+'١', 3), 1)
        self.assertEqual(_citation_index('0', 3), 0)

    def test_generic_tracking_and_malformed_urls(self):
        self.assertEqual(clean_source_url('https://shop.example/item?id=9&utm_source=x&share_id=1&spm=2&fbclid=3'),
                         'https://shop.example/item?id=9')
        self.assertEqual(clean_source_url('http://[::1'), 'http://[::1')
