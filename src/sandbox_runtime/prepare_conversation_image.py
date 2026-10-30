"""Preinstall the official conversation VM image before switching the service.

Run under the service's Unix user and XDG_RUNTIME_DIR so libvirt uses the same
session. The bot can keep serving while this separate template is prepared.
"""
import argparse
import asyncio

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    manager = VmSandboxManager(image=args.image, root=args.root)
    print(await manager.prepare_conversation_image())


if __name__ == '__main__':
    asyncio.run(main())
