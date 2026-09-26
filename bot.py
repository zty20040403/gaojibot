from dotenv import load_dotenv

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


load_dotenv()

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

if nonebot.load_plugin("src.plugins.ai_chat") is None:
    raise SystemExit("Required ai_chat plugin failed to load; refusing to start an empty bot.")


if __name__ == "__main__":
    nonebot.run(timeout_graceful_shutdown=10)
