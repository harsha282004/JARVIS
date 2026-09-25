"""ApiServer: serves the local dashboard/health API inside the JARVIS process (uvicorn on a background thread).

A Lifecycle component for JarvisApplication: `start()` launches the server and waits briefly for it to bind (a port that is already in
use is logged and the rest of JARVIS carries on); `stop()` asks it to exit and joins the thread, so no server thread outlives JARVIS.
"""

import threading
import time

import uvicorn

from backend.core.logging import get_logger

logger = get_logger(__name__)


class ApiServer:
    def __init__(self, host: str, port: int):
        self._host, self._port = host, port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from backend.main import app  # imported here: the FastAPI app reads settings on import

        config = uvicorn.Config(app, host=self._host, port=self._port, log_config=None, access_log=False, lifespan="off")
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # the launcher owns signals
        self._thread = threading.Thread(target=self._server.run, name="jarvis-api", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._server.started and self._thread.is_alive():
            time.sleep(0.05)
        if not self._server.started:
            logger.error("Local API did not start on %s:%s (is the port in use?)", self._host, self._port)
        else:
            logger.info("Local API listening on http://%s:%s/dashboard", self._host, self._port)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
