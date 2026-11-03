from __future__ import annotations

import uvicorn

from app.admin_app import admin_app
from app.admin_tls import tls_options
from app.config import settings


def main() -> None:
    try:
        options = tls_options(settings.admin_ssl_certfile, settings.admin_ssl_keyfile)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    uvicorn.run(
        admin_app,
        host=settings.admin_host,
        port=settings.admin_port,
        reload=False,
        **options,
    )


if __name__ == "__main__":
    main()
