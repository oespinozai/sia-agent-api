#!/usr/bin/env python3
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
cmd = [sys.executable, str(ROOT / "main.py"), "watch"]
result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
if result.returncode != 0:
    sys.stderr.write(result.stderr or result.stdout)
    sys.exit(result.returncode)
output = (result.stdout or "").strip()
if output:
    print(output)
