"""Allow ``python -m worker`` as documented in the README."""
from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
