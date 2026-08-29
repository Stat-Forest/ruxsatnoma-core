import sys

from app.workers.runner import main

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg not in (None, "outbox", "scheduler"):
        raise SystemExit("usage: python -m app.workers [outbox|scheduler]")
    main(only=arg)
