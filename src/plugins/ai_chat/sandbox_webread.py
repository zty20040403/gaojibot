"""webread URL [--offset N]: read public HTML or PDF inside the isolated VM."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
from urllib.request import Request, urlopen

import trafilatura


def read(url, offset=0, *, directory=Path('/work/.gaoji/web')):
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / (hashlib.sha256(url.encode()).hexdigest() + '.txt')
    if not offset or not cache.exists():
        if not url.startswith(('http://', 'https://')):
            raise ValueError('webread 只接受 HTTP(S) URL。')
        with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; webread)'}), timeout=25) as response:
            content = response.read(20 * 1024 * 1024 + 1)
            mime = response.headers.get_content_type()
        if len(content) > 20 * 1024 * 1024:
            raise ValueError('网页或 PDF 超过 20 MiB，请下载到文件后分段处理。')
        if mime == 'application/pdf' or content.startswith(b'%PDF-'):
            with tempfile.TemporaryDirectory() as temporary:
                pdf = Path(temporary) / 'input.pdf'
                pdf.write_bytes(content)
                result = subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, timeout=30, check=True)
                text = result.stdout.decode('utf-8')
        elif mime in {'text/html', 'application/xhtml+xml'}:
            text = trafilatura.extract(content, output_format='markdown', include_links=True, include_tables=True)
            if not text:
                raise ValueError('没有提取到正文；请用浏览器打开页面。')
        else:
            text = content.decode('utf-8', errors='replace')
        cache.write_text(text)
    text = cache.read_text()
    end = min(offset + 12000, len(text))
    return f'{url}\n字符 {offset}–{end}/{len(text)}；全文 {cache}\n' + text[offset:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    if args.offset < 0:
        parser.error('--offset 不能为负数')
    print(read(args.url, args.offset))


if __name__ == '__main__':
    main()
