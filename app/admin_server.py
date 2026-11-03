from __future__ import annotations

import uvicorn

from app.admin_app import admin_app
from app.config import settings

if __name__ == "__main__":
    uvicorn.run(admin_app, host=settings.admin_host, port=settings.admin_port, reload=False)
