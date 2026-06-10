from __future__ import annotations

import os

import uvicorn

from fuckclassroom.web import create_app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("FUCKCLASSROOM_HOST", "127.0.0.1")
    port = int(os.getenv("FUCKCLASSROOM_PORT", "8765"))
    uvicorn.run("main:app", host=host, port=port)
