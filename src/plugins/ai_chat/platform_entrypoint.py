"""Start the official QQ application."""
from __future__ import annotations
import os


def serve():
    import uvicorn
    from .application_runtime import create_application
    port=int(os.environ.get('PORT','8080'))
    if not 1<=port<=65535:raise ValueError('invalid_application_port')
    uvicorn.run(create_application(),host=os.environ.get('HOST','127.0.0.1'),port=port,
        timeout_graceful_shutdown=10,access_log=False)
