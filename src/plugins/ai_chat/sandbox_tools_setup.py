"""Cloud-init and the explicit deployment upgrade use the same VM tools."""
from pathlib import Path

PACKAGES = ['chromium', 'python3-venv', 'ffmpeg', 'poppler-utils']
INSTALL = '''set -eu
python3 -m venv --system-site-packages /opt/gaoji-tools
/opt/gaoji-tools/bin/pip install 'playwright>=1.59,<2' trafilatura 'faster-whisper>=1.2,<2' 'av<19'
/opt/gaoji-tools/bin/python -c "import shutil,site; shutil.copy('/usr/local/share/gaoji/sandbox_browser.py', site.getsitepackages()[0]+'/gaoji_browser.py')"
ln -sfn /opt/gaoji-tools/bin/python /usr/local/bin/gaoji-python
cat > /usr/local/bin/webread <<'EOF'
#!/bin/sh
exec /opt/gaoji-tools/bin/python /usr/local/share/gaoji/sandbox_webread.py "$@"
EOF
chmod 755 /usr/local/bin/webread
cat > /usr/local/bin/gaoji-transcribe <<'EOF'
#!/bin/sh
exec /opt/gaoji-tools/bin/python /usr/local/share/gaoji/sandbox_transcribe.py "$@"
EOF
chmod 755 /usr/local/bin/gaoji-transcribe
'''


def files():
    directory = Path(__file__).parent
    return [{'path': '/usr/local/share/gaoji/' + name, 'permissions': '0644', 'owner': 'root:root',
        'content': (directory / name).read_text()} for name in (
            'sandbox_browser.py', 'sandbox_webread.py', 'sandbox_transcribe.py')] + [{
                'path': '/etc/apt/apt.conf.d/80-gaoji-transport',
                'permissions': '0644', 'owner': 'root:root',
                'content': 'Acquire::http::Timeout "15";\nAcquire::https::Timeout "15";\n',
            }]


async def upgrade(manager, owner, sandbox_id):
    """Run once during the unified deployment for existing conversation VMs."""
    await manager.start_owned(owner, sandbox_id)
    import base64
    import json
    payload = base64.b64encode(json.dumps(files()).encode()).decode()
    script = ("import base64,json,pathlib\n"
        f"for item in json.loads(base64.b64decode({payload!r})):\n"
        " p=pathlib.Path(item['path']);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(item['content'])\n")
    code, _, error = await manager._guest_exec(sandbox_id, '/usr/bin/python3', ['-c', script])
    if code:
        raise RuntimeError(error.decode(errors='replace'))
    command = 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ' + ' '.join(PACKAGES) + '\n' + INSTALL
    code, _, error = await manager._guest_exec(sandbox_id, '/bin/sh', ['-lc', command], timeout=900)
    if code:
        raise RuntimeError(error.decode(errors='replace')[-2000:])
