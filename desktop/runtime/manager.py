"""RuntimeManager: owns the lifecycle of the Phase 1 VoiceEngine.

Windows Runtime (tray/launcher) -> RuntimeManager -> VoiceEngine.

The tray and launcher only call the public methods here; they never touch
the VoiceEngine directly. The engine is built and driven on a worker thread
so the tray stays responsive while models load or a cycle is in progress.

Locking: `_command_lock` serializes lifecycle commands (start/pause/...),
which may block while joining the worker. `_state_lock` guards only short
state reads/writes and is the only lock the worker thread takes, so a
command joining the worker can never deadlock with it.
"""

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from backend.core.llm.base import LLMProviderError
from backend.core.logging import get_logger
from desktop.runtime.state import RuntimeState, RuntimeStatus
from voice.engine import VoiceEngine

logger = get_logger(__name__)

DEFAULT_STOP_TIMEOUT_SECONDS = 15.0

StatusListener = Callable[[RuntimeStatus], None]


class RuntimeManager:
    def __init__(
        self,
        engine_factory: Callable[[], VoiceEngine],
        stop_timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS,
        start_paused: Callable[[], bool] | None = None,
    ):
        self._engine_factory = engine_factory
        # Asked once the engine is built: True means "do not open the microphone now" (a saved PRIVATE/PAUSED mode survives a restart).
        self._start_paused = start_paused
        self._stop_timeout = stop_timeout
        self._command_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._state = RuntimeState.STOPPED
        self._engine: VoiceEngine | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_error: str | None = None
        self._started_at = datetime.now(timezone.utc)
        self._listeners: list[StatusListener] = []

    # ---- observation -------------------------------------------------

    @property
    def state(self) -> RuntimeState:
        with self._state_lock:
            return self._state

    def status(self) -> RuntimeStatus:
        with self._state_lock:
            state, engine, last_error = self._state, self._engine, self._last_error
        return RuntimeStatus(
            state=state,
            voice_state=engine.state if engine is not None else None,
            started_at=self._started_at,
            last_error=last_error,
            microphone_active=(
                engine.microphone_active
                if engine is not None and state is RuntimeState.RUNNING
                else False
            ),
        )

    def add_listener(self, listener: StatusListener) -> None:
        """Register a callback invoked (from any thread) after each state change."""
        self._listeners.append(listener)

    # ---- commands ----------------------------------------------------

    def start(self) -> bool:
        """Build the VoiceEngine and start listening. Valid from STOPPED/ERROR."""
        with self._command_lock:
            if not self._set_state(
                RuntimeState.STARTING, expected={RuntimeState.STOPPED, RuntimeState.ERROR}
            ):
                logger.info("start ignored in state %s", self.state)
                return False
            with self._state_lock:
                self._last_error = None
            logger.info("VoiceEngine starting")
            self._launch(build_engine=True)
            return True

    def pause(self) -> bool:
        """Stop listening and release the microphone; the runtime stays alive."""
        with self._command_lock:
            if self.state is not RuntimeState.RUNNING:
                logger.info("pause ignored in state %s", self.state)
                return False
            if not self._stop_worker():
                self._fail_stop("Voice worker did not stop while pausing")
                return False
            self._set_state(RuntimeState.PAUSED)
            logger.info("Paused; microphone released")
            return True

    def resume(self) -> bool:
        """Resume listening after pause, reusing the already-loaded engine."""
        with self._command_lock:
            if not self._set_state(RuntimeState.RUNNING, expected={RuntimeState.PAUSED}):
                logger.info("resume ignored in state %s", self.state)
                return False
            logger.info("Resuming")
            self._launch(build_engine=False)
            return True

    def restart(self) -> bool:
        """Tear down the engine and start a fresh one."""
        with self._command_lock:
            if self.state not in (RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.ERROR):
                logger.info("restart ignored in state %s", self.state)
                return False
            logger.info("Restarting")
            if not self._stop_worker():
                self._fail_stop("Voice worker did not stop while restarting")
                return False
            self._discard_engine()
            self._set_state(RuntimeState.STOPPED)
            return self.start()

    def shutdown(self) -> None:
        """Stop everything and release audio resources. Safe to call repeatedly."""
        with self._command_lock:
            if self.state is RuntimeState.STOPPED:
                return
            logger.info("Shutdown requested")
            self._set_state(RuntimeState.STOPPING)
            if not self._stop_worker():
                with self._state_lock:
                    self._last_error = "Voice worker did not stop within the timeout"
                logger.error("Voice worker did not stop within %ss during shutdown", self._stop_timeout)
            self._discard_engine()
            self._set_state(RuntimeState.STOPPED)
            logger.info("VoiceEngine stopped")

    def request_activation(self) -> bool:
        """Start a conversation as if the wake word had been heard. Only while RUNNING (never opens a paused/private microphone)."""
        with self._state_lock:
            engine, state = self._engine, self._state
        if engine is None or state is not RuntimeState.RUNNING:
            logger.info("Manual activation ignored in state %s", state)
            return False
        engine.request_activation()
        return True

    def interrupt_speech(self) -> bool:
        """Stop whatever JARVIS is saying right now (tray / dashboard "Stop speaking"). False if there is no engine."""
        with self._state_lock:
            engine = self._engine
        if engine is None:
            return False
        engine.interrupt()
        return True

    def handle_system_resume(self) -> None:
        """Called after Windows wakes from sleep: reacquire the microphone.

        Audio devices can be invalid after sleep, so a running engine's worker
        is restarted (the microphone is reopened on the next cycle). A paused
        or stopped runtime is left as it is.
        """
        with self._command_lock:
            if self.state is not RuntimeState.RUNNING:
                logger.info("System resume ignored in state %s", self.state)
                return
            logger.info("System resumed from sleep; reacquiring microphone")
            if not self._stop_worker():
                self._fail_stop("Voice worker did not stop after system resume")
                return
            self._launch(build_engine=False)

    # ---- internals ---------------------------------------------------

    def _launch(self, build_engine: bool) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(self._stop_event, build_engine),
            name="jarvis-voice",
            daemon=True,
        )
        self._thread.start()

    def _stop_worker(self) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self._stop_event.set()
        thread.join(self._stop_timeout)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    def _discard_engine(self) -> None:
        with self._state_lock:
            self._engine = None

    def _fail_stop(self, message: str) -> None:
        with self._state_lock:
            self._last_error = message
        logger.error(message)
        self._set_state(RuntimeState.ERROR)

    def _run(self, stop: threading.Event, build_engine: bool) -> None:
        """Worker thread body: build the engine (first launch) and drive it."""
        try:
            if build_engine:
                engine = self._engine_factory()
                with self._state_lock:
                    if stop.is_set() or self._state is not RuntimeState.STARTING:
                        return
                    self._engine = engine
                if self._start_paused is not None and self._start_paused():
                    self._set_state(RuntimeState.PAUSED, expected={RuntimeState.STARTING})
                    logger.info("VoiceEngine built; starting paused because of the saved privacy mode (microphone not opened)")
                    return
                self._set_state(RuntimeState.RUNNING, expected={RuntimeState.STARTING})
                logger.info("VoiceEngine started")

            engine = self._engine
            while engine is not None and not stop.is_set():
                try:
                    engine.run_once(stop.is_set)
                except LLMProviderError as exc:
                    # Transient (e.g. Ollama not running): keep listening, but surface it.
                    with self._state_lock:
                        self._last_error = f"LLM request failed: {exc}"
                    logger.error("LLM request failed, continuing to listen: %s", exc)
        except Exception as exc:  # noqa: BLE001 - worker boundary: record, expose ERROR, re-log with traceback
            logger.exception("Voice worker crashed")
            with self._state_lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            if self._set_state(
                RuntimeState.ERROR, expected={RuntimeState.STARTING, RuntimeState.RUNNING}
            ):
                self._discard_engine()

    def _set_state(
        self, new: RuntimeState, expected: set[RuntimeState] | None = None
    ) -> bool:
        with self._state_lock:
            if expected is not None and self._state not in expected:
                return False
            old, self._state = self._state, new
        if old is not new:
            logger.info("Runtime state %s -> %s", old, new)
            self._notify()
        return True

    def _notify(self) -> None:
        status = self.status()
        for listener in list(self._listeners):
            try:
                listener(status)
            except Exception:  # noqa: BLE001 - a broken listener must not kill the runtime
                logger.exception("Runtime status listener failed")
