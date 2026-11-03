#!/usr/bin/env python3
"""Install one forced-command public key; preserve all unrelated SSH keys."""
import argparse
import base64
import os
import re
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", default="192.168.0.30")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F:.]+", args.controller):
        raise SystemExit("Use a controller IP address")
    fields = sys.stdin.read().strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise SystemExit("An ED25519 public key is required")
    base64.b64decode(fields[1], validate=True)
    line = 'from="' + args.controller + '",restrict,command="/usr/bin/python3 /opt/vm-platform/worker.py --ssh" ' + fields[0] + " " + fields[1] + " vm-platform-controller\n"
    path = Path.home() / ".ssh" / "authorized_keys"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(path.parent), 0o700)
    existing = path.read_text() if path.exists() else ""
    matching = [x for x in existing.splitlines() if fields[1] in x.split()]
    if matching and matching != [line.rstrip("\n")]:
        raise SystemExit("Same key exists with different restrictions; refusing to change it")
    if not matching:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write(line)
    os.chmod(str(path), 0o600)
    print("Restricted controller key installed; unrelated keys preserved")

if __name__ == "__main__":
    main()
