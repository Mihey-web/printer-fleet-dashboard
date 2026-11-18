#!/usr/bin/env python3
"""Nightly backup of the dashboard's SQLite databases to a separate disk.

Each run writes <dest>/<YYYY-MM-DD>/ with a consistent copy of every database
(SQLite online backup API: safe while the service is writing, and the copy is a
single self-contained file even when data still sits in the -wal journal), plus
config.py, data/job_previews/ (the job previews and job cards the print history
refers to; a printer no longer has them) and a manifest.json with sizes and a
quick_check result per copy. Only the newest --keep dated directories are kept;
nothing else in <dest> is touched.

The job previews never change (each file is named by the hash of its content), so a
file the previous backup already has, unchanged, becomes a hard link to it: a night
costs only the new previews, and pruning an old backup frees only what no newer one
links. On a disk without hard links every file is copied.

--mountpoint makes the run refuse (exit 2) when that path is not a mounted
filesystem, so a missing data disk never fills up the SD card instead.

Stdlib only, so it runs with the system python3. Exit codes: 0 ok, 1 some copy
failed, 2 data disk not mounted, 3 not enough free space.
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import sys
import time

DATABASES = ("printer_history.db", "users.db", "ams_history.db")
EXTRA_FILES = ("config.py",)
# Folders copied file by file (not their subfolders); a name starting with "." is a write in
# progress (job_previews' .preview-* / .project-* temporaries) and is skipped.
FOLDERS = ("data/job_previews",)
DATED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def log(msg):
    print(msg, flush=True)


def backup_db(src_path, dst_path):
    """Copy one database through the online backup API and check the copy."""
    # as_uri() escapes '#' / '?' / '%': a raw "file:%s" read "/x #1/a.db" as
    # "/x " + a fragment and backed up a fresh empty file, with exit 0.
    src = sqlite3.connect(pathlib.Path(src_path).absolute().as_uri() + "?mode=ro", uri=True, timeout=60)
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


def folder_files(src_dir):
    """[(name, os.stat_result)] of the regular files directly in src_dir, None without it."""
    try:
        names = sorted(os.listdir(src_dir))
    except (FileNotFoundError, NotADirectoryError):
        return None
    out = []
    for name in names:
        if name.startswith("."):
            continue
        try:
            st = os.lstat(os.path.join(src_dir, name))
        except FileNotFoundError:
            continue                                   # removed meanwhile (the panel's cleanup)
        if stat.S_ISREG(st.st_mode):
            out.append((name, st))
    return out


def unchanged(old_path, st):
    """The previous backup's file is the same file: same size and modification time."""
    try:
        old = os.lstat(old_path)
    except OSError:
        return False
    return stat.S_ISREG(old.st_mode) and old.st_size == st.st_size and abs(old.st_mtime - st.st_mtime) < 1


def links_work(directory):
    """True when the backup disk takes hard links (ext4 does, FAT / exFAT do not)."""
    probe = os.path.join(directory, ".link-probe-%d" % os.getpid())
    try:
        with open(probe, "wb"):
            pass
        os.link(probe, probe + "-2")
        os.unlink(probe + "-2")
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def backup_folder(src_dir, dst_dir, prev_dir, files, link):
    """Copy `files` of src_dir into dst_dir; with `link`, a file prev_dir has unchanged is a
    hard link to it instead. -> manifest entry {files, bytes, linked, copied, errors}."""
    os.makedirs(dst_dir)
    info = {"files": 0, "bytes": 0, "linked": 0, "copied": 0, "errors": 0}
    for name, st in files:
        src_path, dst_path = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        old = os.path.join(prev_dir, name) if prev_dir else None
        try:
            if link and old and unchanged(old, st):
                try:
                    os.link(old, dst_path)
                    info["linked"] += 1
                except OSError:
                    link = False                       # no hard links after all: copy the rest
            if not os.path.exists(dst_path):
                shutil.copy2(src_path, dst_path)
                os.chmod(dst_path, 0o600)
                info["copied"] += 1
        except FileNotFoundError:
            continue                                   # removed meanwhile (an orphan the panel pruned)
        except OSError as e:
            info["errors"] += 1
            log("ERROR: %s: %s" % (src_path, e))
            continue
        info["files"] += 1
        info["bytes"] += st.st_size
    return info


def newest_backup(dest):
    """The newest dated backup directory already in dest (the hard links' source), or None."""
    dated = sorted(n for n in os.listdir(dest)
                   if DATED_DIR.match(n) and os.path.isdir(os.path.join(dest, n)))
    return os.path.join(dest, dated[-1]) if dated else None


def prune(dest, keep):
    dated = sorted(n for n in os.listdir(dest)
                   if DATED_DIR.match(n) and os.path.isdir(os.path.join(dest, n)))
    for name in dated[:-keep] if keep > 0 else []:
        shutil.rmtree(os.path.join(dest, name), ignore_errors=True)
        log("removed old backup %s" % name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True,
                    help="directory with the databases and data/ (the app dir, or FORGE_DATA_DIR when set)")
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
    previous = newest_backup(args.dest)
    link = links_work(args.dest)
    folders = {}
    for rel in FOLDERS:
        files = folder_files(os.path.join(args.src, *rel.split("/")))
        if files is None:
            continue
        folders[rel] = files
        prev_dir = os.path.join(previous, *rel.split("/")) if previous else None
        # what this night really adds: the files no hard link covers
        need += sum(st.st_size for name, st in files
                    if not (link and prev_dir and unchanged(os.path.join(prev_dir, name), st)))
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
    for rel, files in folders.items():
        parts = rel.split("/")
        t0 = time.time()
        info = backup_folder(os.path.join(args.src, *parts), os.path.join(work, *parts),
                             os.path.join(previous, *parts) if previous else None, files, link)
        info["seconds"] = round(time.time() - t0, 1)
        manifest.setdefault("folders", {})[rel] = info
        log("%s/: %d files, %.1f MB (%d linked, %d copied), %.1f s"
            % (rel, info["files"], info["bytes"] / 2**20, info["linked"], info["copied"], info["seconds"]))
        failed += info["errors"]
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
