"""Σημείο εκκίνησης για το PyInstaller (PriceTracker.exe)."""
import sys

from pricetracker.app import main

if __name__ == "__main__":
    sys.exit(main())
