"""Shared chat domain and application modules, with no platform startup effects.

The explicit NoneBot entrypoint is ``src.plugins.ai_chat.onebot_plugin``.
Importing public types must not install matchers, initialize a driver or construct
runtime services; the OneBot entrypoint performs that composition when loaded.
"""
