"""Explicit Unix test-account entry point. Never invoked by application startup."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from .credentials import Credentials,DEFAULT_API_BASE,SANDBOX_API_BASE
from .gateway import GatewayConnection
from .group_moderation import GroupModerationProtocol
from .http import RestClient
from .probe import _silence_transport_logs
from .probe_moderation import ModerationProbeOptions,run_moderation_probe
from .token import TokenManager


def _verify_fd(fd,*,directory=False):
    info = os.fstat(fd)
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise ValueError('Moderation files and report directory must be private and operator-owned')
    if sys.platform=='darwin':
        # Inspect the opened object, not a replaceable pathname. BSD ls -e shows
        # extended ACLs that may grant access independently of mode bits.
        result = subprocess.run(['/bin/ls','-ldeL','/dev/fd/'+str(fd)],pass_fds=(fd,),capture_output=True,text=True,timeout=5)
        lines = result.stdout.splitlines()
        if result.returncode or len(lines)!=1 or not lines[0].split() or '+' in lines[0].split()[0]:
            raise ValueError('Moderation storage ACL is not private or could not be verified')
    elif not sys.platform.startswith('linux'):
        raise ValueError('Moderation storage ACL support is unavailable')


def _private_text(path):
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        _verify_fd(fd)
        with os.fdopen(fd,'r',encoding='utf-8',closefd=False) as stream:value = stream.read(16385)
        if len(value)>16384:raise ValueError('Moderation input is too large')
        return value.strip()
    finally:os.close(fd)


def _options(args):
    if os.name!='posix':raise ValueError('Moderation probe reports require supported Unix storage')
    credentials = Credentials(_private_text(args.app_id_file),_private_text(args.app_secret_file))
    scope = json.loads(_private_text(args.scope_file))
    if (type(scope) is not dict or set(scope)!={'app_id','group','member','independent_test_account','exclusive_moderation'}
        or scope['app_id']!=credentials.app_id or scope['independent_test_account'] is not True or scope['exclusive_moderation'] is not True):
        raise ValueError('Scope must declare this independent test account and exclusive single-target experiment')
    options = ModerationProbeOptions(report=args.report,app_id=credentials.app_id,group=scope['group'],member=scope['member'],
        checks=tuple(args.checks.split(',')),duration=args.duration,environment='sandbox' if args.sandbox else 'production')
    options.validate()
    fd = os.open(args.report.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:_verify_fd(fd,directory=True)
    finally:os.close(fd)
    return credentials,options


def argument_parser():
    parser = argparse.ArgumentParser(description='F7 独立测试机器人/单成员群管探测；默认只读，不启动 Gaoji。')
    for option in ('app-id-file','app-secret-file','scope-file','report'):
        parser.add_argument('--'+option,type=Path,required=True)
    parser.add_argument('--checks',default='read',help='明确选择逗号分隔的 read,mute,recall；mute仅60秒，recall需要新消息')
    parser.add_argument('--duration',type=float,default=120,help='原批次期限，最多300秒')
    parser.add_argument('--sandbox',action='store_true',help='明确选择固定沙箱地址，不从错误推断或自动切换')
    parser.add_argument('--confirm-moderation-batch',action='store_true',help='明确批准scope文件中的独立账号/唯一目标/本批步骤')
    return parser


async def async_main(argv=None):
    args = argument_parser().parse_args(argv)
    _silence_transport_logs()
    credentials,options = _options(args)
    token = TokenManager(credentials,base_url=SANDBOX_API_BASE if args.sandbox else DEFAULT_API_BASE)
    rest = RestClient(token)
    def confirm(prompt):return args.confirm_moderation_batch or input(prompt+' 输入 YES：')=='YES'
    try:
        report = await run_moderation_probe(options,GroupModerationProtocol(rest),GatewayConnection(rest),confirm=confirm)
        return 0 if report['status'] in {'completed','cancelled'} else 1
    finally:
        await rest.aclose(); await token.aclose()


def main():
    try:return asyncio.run(async_main())
    except KeyboardInterrupt:
        print('探测已停止；不重试或自动补解除，请检查受保护报告和原60秒期限。')
        return 130
    except Exception as error:
        print('群管探测失败：'+type(error).__name__+'；检查scope、私有文件、网络和报告，不打印原始异常。')
        return 1


if __name__=='__main__':raise SystemExit(main())
