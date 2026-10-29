from dotenv import load_dotenv


if __name__ == "__main__":
    load_dotenv()
    from src.plugins.ai_chat.platform_entrypoint import serve
    serve()
