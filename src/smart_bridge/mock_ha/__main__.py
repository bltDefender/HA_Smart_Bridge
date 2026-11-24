import os

import uvicorn

from .server import create_app

if __name__ == "__main__":
    port = int(os.environ.get("MOCK_HA_PORT", "8123"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")  # noqa: S104
