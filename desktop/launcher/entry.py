"""The one entry point for every way of starting JARVIS (`python -m desktop.launcher`, `scripts/windows/jarvis_launcher.pyw`, a Task Scheduler action,
a Run entry, the Startup folder, start_jarvis.ps1): bootstrap the process, run `cli.main`, and turn any unexpected exception into a logged, diagnosable exit."""

import sys

from desktop.launcher import bootstrap


def run(argv: list[str] | None = None) -> int:
    bootstrap.prepare_process()
    source = _source_from(argv if argv is not None else sys.argv[1:])
    bootstrap.install_excepthooks(source)
    try:
        from desktop.launcher.cli import main
    except Exception as exc:  # noqa: BLE001 - settings are validated at import: an invalid configuration ends here, cleanly
        from pydantic import ValidationError

        if not isinstance(exc, ValidationError):
            bootstrap.write_crash("startup_failure", exc, startup_source=source)
            return 1
        fields = ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}))
        message = f"JARVIS configuration is invalid ({fields}). Fix .env and start again. Nothing was started."
        try:
            log = bootstrap.PROJECT_ROOT / "logs" / "jarvis.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except OSError:
            pass
        bootstrap.write_crash("startup_failure: " + message, None, startup_source=source)
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2
    try:
        return main(argv)
    except SystemExit as exc:  # argparse / explicit exits keep their code
        return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        return 0
    except BaseException as exc:  # noqa: BLE001 - last line of defence: log it, never vanish silently
        bootstrap.write_crash("unexpected_exception", exc, startup_source=source)
        try:
            from backend.core.logging import get_logger

            get_logger("desktop.launcher").exception("JARVIS_UNEXPECTED_EXIT SHUTDOWN_REASON=unexpected_exception")
        except Exception:  # noqa: BLE001
            pass
        return 1


def _source_from(argv: list[str]) -> str:
    for i, a in enumerate(argv):
        if a == "--startup-source" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--startup-source="):
            return a.split("=", 1)[1]
    return "manual"
