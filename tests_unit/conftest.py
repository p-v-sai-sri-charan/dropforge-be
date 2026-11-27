"""Fast unit tests: run the API routes in-process against an in-memory Mongo (mongomock), no server or real
database needed. `pytest tests_unit` (the older suites in tests/ hit a running server)."""
import os
import sys
from pathlib import Path

# must be set before any backend module (core.py) is imported
os.environ.update(MONGO_URL="mongodb://localhost:1", DB_NAME="unit", JWT_SECRET="x" * 40, ADMIN_PHONES="+910000000001")
os.environ.pop("REDIS_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
