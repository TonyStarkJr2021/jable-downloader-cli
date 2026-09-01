from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn

from jable_web.app import create_app


def main() -> None:
    config_path = Path(os.environ.get("JABLE_WEB_CONFIG", "/etc/jable-downloader/web.json"))
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    application = create_app(web_config_path=config_path)
    uvicorn.run(
        application,
        host=str(config.get("host", "0.0.0.0")),
        port=int(config["port"]),
        workers=1,
        proxy_headers=False,
        server_header=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
