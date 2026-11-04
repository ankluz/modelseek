import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    import httpx2

    sys.modules["httpx"] = httpx2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
