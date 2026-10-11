#!/usr/bin/env python3
"""Nightly backup of the dashboard's SQLite databases to a separate disk.

Each run writes <dest>/<YYYY-MM-DD>/ with a consistent copy of every database
(SQLite online backup API: safe while the service is writing, and the copy is a
single self-contained file even when data still sits in the -wal journal), plus
config.py and a manifest.json with sizes and a quick_check result per copy.
Only the newest --keep dated directories are kept; nothing else in <dest> is
touched.

--mountpoint makes the run refuse (exit 2) when that path is not a mounted
filesystem, so a missing data disk never fills up the SD card instead.

Stdlib only, so it runs with the system python3. Exit codes: 0 ok, 1 some copy
failed, 2 data disk not mounted, 3 not enough free space.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import time

DATABASES = ("printer_history.db", "users.db", "ams_history.db")
EXTRA_FILES = ("config.py",)
DATED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def log(msg):
    print(msg, flush=True)


def backup_db(src_path, dst_path):
    """Copy one database through the online backup API and check the copy."""
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True, timeout=60)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.execute("pragma journal_mode=delete")  # a single file, no -wal beside it
        check = dst.execute("pragma quick_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    os.chmod(dst_path, 0o600)
    return check


def prune(dest, keep):
    dated = sorted(n for n in os.listdir(dest)
                   if DATED_DIR.match(n) and os.path.isdir(os.path.join(dest, n)))
    for name in dated[:-keep] if keep > 0 else []:
        shutil.rmtree(os.path.join(dest, name), ignore_errors=True)
        log("removed old backup %s" % name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True, help="directory with the databases (the app dir)")
    ap.add_argument("--dest", required=True, help="backup root, e.g. /mnt/ssd/forge-backups")
    ap.add_argument("--keep", type=int, default=14, help="dated backups to keep")
    ap.add_argument("--mountpoint", help="refuse to run unless this path is a mounted filesystem")
    args = ap.parse_args(argv)
    # users.db holds password hashes: keep every backup file owner-only.
    os.umask(0o077)

    if args.mountpoint and not os.path.ismount(args.mountpoint):
        log("ERROR: %s is not mounted — backup skipped (not writing to the system disk)" % args.mountpoint)
        return 2

    sources = [n for n in DATABASES + EXTRA_FILES if os.path.isfile(os.path.join(args.src, n))]
    need = sum(os.path.getsize(os.path.join(args.src, n)) for n in sources)
    os.makedirs(args.dest, exist_ok=True)
    free = shutil.disk_usage(args.dest).free
    if free < 2 * need:
        log("ERROR: not enough free space on %s: %d MB free, need %d MB" % (args.dest, free // 2**20, 2 * need // 2**20))
        return 3

    day = datetime.date.today().isoformat()
    final = os.path.join(args.dest, day)
    work = os.path.join(args.dest, ".tmp-%s-%d" % (day, os.getpid()))
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    started = time.time()
    manifest = {"date": day, "files": {}}
    failed = 0
    for name in DATABASES:
        src_path = os.path.join(args.src, name)
        if not os.path.isfile(src_path):
            continue
        t0 = time.time()
        try:
            check = backup_db(src_path, os.path.join(work, name))
            size = os.path.getsize(os.path.join(work, name))
            manifest["files"][name] = {"bytes": size, "quick_check": check, "seconds": round(time.time() - t0, 1)}
            log("%s: %.1f MB, quick_check=%s, %.1f s" % (name, size / 2**20, check, time.time() - t0))
            if check != "ok":
                failed += 1
        except sqlite3.Error as e:
            failed += 1
            manifest["files"][name] = {"error": str(e)}
            log("ERROR: %s: %s" % (name, e))
    for name in EXTRA_FILES:
        src_path = os.path.join(args.src, name)
        if os.path.isfile(src_path):
            shutil.copy2(src_path, os.path.join(work, name))
            os.chmod(os.path.join(work, name), 0o600)
            manifest["files"][name] = {"bytes": os.path.getsize(src_path)}
    manifest["seconds"] = round(time.time() - started, 1)
    with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    # Swap the finished copy in only now, so a crash mid-run never leaves a
    # half-written dated directory that looks like a good backup.
    if os.path.isdir(final):
        shutil.rmtree(final)
    os.replace(work, final)
    prune(args.dest, args.keep)
    log("backup %s done in %.1f s, %d failed" % (final, time.time() - started, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
