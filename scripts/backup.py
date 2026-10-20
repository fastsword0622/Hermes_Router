#!/usr/bin/env python3
"""Consistent SQLite backups while the Router is stopped; never copy live WAL files."""
import argparse
from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.data_dir.resolve() == args.output.resolve():
        parser.error("backup directory must differ from data directory")
    with (args.data_dir / "router-owner.lock").open("a+") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            parser.error("stop the Router before backup; owner is still active")
        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        names = ("tasks.sqlite3", "checkpoints.sqlite3", "development.sqlite3", "routing-checkpoints.sqlite3")
        if any((args.output / name).exists() for name in names):
            parser.error("backup target already contains database files; choose a new directory")
        for name in names:
            source = args.data_dir / name
            if source.exists():
                with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(args.output / name)) as dst:
                    src.backup(dst)
                print("backed up " + name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
