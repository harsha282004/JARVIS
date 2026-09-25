import sys


def _run() -> int:
    try:
        from desktop.launcher.cli import main
    except Exception as exc:  # noqa: BLE001 - settings are validated at import: an invalid configuration ends here, cleanly
        from pydantic import ValidationError

        if not isinstance(exc, ValidationError):
            raise
        fields = ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}))
        message = f"JARVIS configuration is invalid ({fields}). Fix .env and start again. Nothing was started."
        try:
            from pathlib import Path

            log = Path(__file__).resolve().parents[2] / "logs" / "jarvis.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except OSError:
            pass
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2
    return main()


sys.exit(_run())
