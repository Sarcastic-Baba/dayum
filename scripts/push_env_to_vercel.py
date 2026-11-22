"""Copy keys from .env to the linked Vercel project without echoing any value.

Each value is passed to `vercel env add` on stdin, never on a command line, and nothing
is printed except key names and ok/failed. Existing values are replaced.

Usage:  python scripts/push_env_to_vercel.py [KEY ...]      (default: every non-empty key)
Needs:  `vercel login` (or VERCEL_TOKEN) and `vercel link` in this folder.
"""

import shutil
import subprocess
import sys

from dotenv import dotenv_values

TARGETS = ("production", "preview", "development")


def main():
    vercel = shutil.which("vercel") or sys.exit("vercel CLI not found on PATH")
    env = dotenv_values(".env")
    keys = sys.argv[1:] or [k for k, v in env.items() if v]
    failed = 0
    for key in keys:
        value = env.get(key)
        if not value:
            print(f"skip   {key}: missing or empty in .env")
            continue
        for target in TARGETS:
            subprocess.run([vercel, "env", "rm", key, target, "--yes"],
                           capture_output=True, text=True)
            p = subprocess.run([vercel, "env", "add", key, target], input=value,
                               capture_output=True, text=True)
            ok = p.returncode == 0
            failed += not ok
            detail = "" if ok else " — " + (p.stderr or p.stdout).strip().splitlines()[-1][:140]
            print(f"{'ok    ' if ok else 'FAILED'} {key:24s} {target}{detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
