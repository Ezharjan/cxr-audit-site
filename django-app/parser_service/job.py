"""Container-only entry point: Docker supplies the filesystem/network boundary."""
import json
import sys
from pathlib import Path

# Isolated Python excludes the script directory from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from parser_worker import limits, parse

if __name__ == '__main__':
    try:
        limits()
        result = parse(Path(sys.argv[1]))
        Path(sys.argv[2]).write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    except Exception:
        sys.exit(2)
