import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
