#!/usr/bin/env python3
"""Recense et snapshot tout le non-stock (= hors image Linux de base / hors dpkg).

Livraison intégrale : aucun filtre de taille ni de chemin métier.
Écarte seulement les montages virtuels (proc/sys/dev/run) et les dossiers
non-stock/snapshot-* (sorties de ce script, pour éviter la récursion).

Usage:
  sudo -E python3 recenser.py              # nouveau snapshot-<UTC>
  sudo -E python3 recenser.py --resume DIR # reprend une copie inachevée
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


VIRTUAL_TOP = {"proc", "sys", "dev", "run"}
SCRIPT_DIR = Path(__file__).resolve().parent
# Archive durable (évite GitHub LFS pour les gros snapshots).
STORE_ARCHIVE_ROOT = Path(
    "/cursor/stores/user/cold/archives/zips-non-stock"
)
CHUNK = 83_886_080


def load_dpkg_owned() -> set[str]:
    owned: set[str] = set()
    for list_path in glob.glob("/var/lib/dpkg/info/*.list"):
        try:
            with open(list_path, "r", errors="ignore") as fh:
                for line in fh:
                    p = line.rstrip("\n")
                    if p:
                        owned.add(p)
        except OSError:
            continue
    return owned


def is_snapshot_path(path: str) -> bool:
    try:
        rel = os.path.relpath(path, SCRIPT_DIR)
    except ValueError:
        return False
    if rel == "." or rel.startswith(".." + os.sep):
        parts = Path(path).parts
        try:
            i = parts.index("non-stock")
        except ValueError:
            return False
        return i + 1 < len(parts) and parts[i + 1].startswith("snapshot-")
    first = rel.split(os.sep, 1)[0]
    return first.startswith("snapshot-")


def iter_non_stock(owned: set[str]):
    for dirpath, dirnames, filenames in os.walk("/", topdown=True, followlinks=False):
        pruned = []
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if d in VIRTUAL_TOP and dirpath == "/":
                pruned.append(d)
            elif is_snapshot_path(full):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for name in filenames:
            full = os.path.join(dirpath, name)
            if full in owned or is_snapshot_path(full):
                continue
            yield full


def already_present(src: str, dest_root: Path) -> bool:
    target = dest_root / src.lstrip("/")
    return target.exists() or target.is_symlink()


def cp_one(src: str, dest_root: Path, errors: list[str]) -> bool:
    target = dest_root / src.lstrip("/")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["cp", "-a", "--reflink=auto", src, str(target)],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return True
        errors.append(f"{src}\tcp:{(r.stderr or r.stdout).strip()}")
        return False
    except OSError as exc:
        errors.append(f"{src}\t{exc}")
        return False


def copy_all(paths: list[str], dest: Path, errors: list[str]) -> tuple[int, int]:
    """Prefer tar batches; fall back to cp -a per file on batch issues."""
    copied = 0
    failed = 0
    pending = [p for p in paths if not already_present(p, dest)]
    skipped = len(paths) - len(pending)
    print(f"à copier: {len(pending)} (déjà présents: {skipped})", flush=True)

    # Fast path: streaming tar | tar (sans tout charger en RAM)
    list_file = dest / ".copy-list"
    list_file.write_text("\n".join(p.lstrip("/") for p in pending) + "\n", encoding="utf-8")
    t0 = time.time()
    prod = subprocess.Popen(
        [
            "tar",
            "-C",
            "/",
            "-cf",
            "-",
            "--ignore-failed-read",
            "--files-from",
            str(list_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cons = subprocess.Popen(
        ["tar", "-C", str(dest), "-xf", "-"],
        stdin=prod.stdout,
        stderr=subprocess.PIPE,
    )
    assert prod.stdout is not None
    prod.stdout.close()
    _out, cons_err = cons.communicate()
    _out, prod_err = prod.communicate()
    if cons.returncode == 0 and prod.returncode in (0, 1):
        # tar exit 1 = warnings (fichiers illisibles) avec --ignore-failed-read
        copied = len(pending)
        print(f"tar stream ok en {time.time() - t0:.1f}s", flush=True)
        if prod_err:
            errors.append(f"tar-create-warn\t{prod_err.decode('utf-8', 'replace')[:2000]}")
        list_file.unlink(missing_ok=True)
        return copied + skipped, failed

    err = (
        (prod_err or b"") + b" | " + (cons_err or b"")
    ).decode("utf-8", "replace")[:800]
    errors.append(f"tar-stream\t{err}")
    print(f"tar stream échoué, fallback cp par fichier: {err[:200]}", flush=True)
    list_file.unlink(missing_ok=True)

    for i, src in enumerate(pending, 1):
        if cp_one(src, dest, errors):
            copied += 1
        else:
            failed += 1
        if i % 5000 == 0:
            print(
                f"  ... {i}/{len(pending)} ({copied} ok, {failed} fail)",
                flush=True,
            )
    return copied + skipped, failed


def write_meta(dest: Path, stamp: str, count: int, extra: list[str]) -> None:
    lines = [
        f"stamp={stamp}",
        "definition=hors image Linux de base (absents des listes dpkg)",
        f"host={os.uname().nodename}",
        f"uname={' '.join(os.uname())}",
        f"count={count}",
        *extra,
    ]
    (dest / "META.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", type=Path, default=None, help="Reprendre snapshot-<stamp>")
    args = ap.parse_args()

    if args.resume:
        dest = args.resume.resolve()
        if not dest.is_dir() or not (dest / "MANIFEST.txt").is_file():
            print(f"resume invalide: {dest}", file=sys.stderr)
            return 2
        stamp = dest.name.removeprefix("snapshot-")
        print(f"== reprise {dest}", flush=True)
        paths = (dest / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
        paths = [p for p in paths if p]
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = SCRIPT_DIR / f"snapshot-{stamp}"
        dest.mkdir(parents=True, exist_ok=False)
        print(f"== non-stock recensement {stamp}", flush=True)
        print(f"destination: {dest}", flush=True)

        print("== index dpkg", flush=True)
        owned = load_dpkg_owned()
        print(f"fichiers dpkg indexés: {len(owned)}", flush=True)

        print("== inventaire hors dpkg", flush=True)
        t0 = time.time()
        paths = []
        for p in iter_non_stock(owned):
            paths.append(p)
            if len(paths) % 20000 == 0:
                print(f"  ... {len(paths)} recensés", flush=True)
        (dest / "MANIFEST.txt").write_text("\n".join(paths) + "\n", encoding="utf-8")
        print(f"non-stock recensés: {len(paths)} ({time.time() - t0:.1f}s)", flush=True)
        write_meta(dest, stamp, len(paths), [])

    print(f"== copie intégrale vers {dest}", flush=True)
    errors: list[str] = []
    t1 = time.time()
    copied, failed = copy_all(paths, dest, errors)

    if errors:
        err_path = dest / "ERRORS.txt"
        prev = err_path.read_text(encoding="utf-8") if err_path.exists() else ""
        err_path.write_text(prev + "\n".join(errors) + "\n", encoding="utf-8")

    extra = [
        f"copied={copied}",
        f"failed={failed}",
        f"manifest_lines={len(paths)}",
        f"copy_seconds={time.time() - t1:.1f}",
        f"finished_at={datetime.now(timezone.utc).isoformat()}",
    ]
    write_meta(dest, stamp, len(paths), extra)
    for line in extra:
        print(line, flush=True)

    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid and os.geteuid() == 0:
        subprocess.run(["chown", "-R", f"{uid}:{gid}", str(dest)], check=False)

    # cleanup resume helper if any
    (dest / "RESUME_MISSING.txt").unlink(missing_ok=True)
    (dest / ".copy-list").unlink(missing_ok=True)
    (dest / ".batch.tar").unlink(missing_ok=True)

    archive_path = archive_to_store(dest)
    if archive_path:
        print(f"== archive store: {archive_path}", flush=True)

    print(f"== terminé: {dest}", flush=True)
    print(str(dest))
    return 0 if failed == 0 else 1


def archive_to_store(dest: Path) -> Path | None:
    """Tar+split le snapshot dans le store utilisateur (sandsplit)."""
    if not STORE_ARCHIVE_ROOT.parent.is_dir():
        print("store utilisateur absent — pas d'archive sandsplit", flush=True)
        return None
    STORE_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    out = STORE_ARCHIVE_ROOT / f"{dest.name}.tar.sandsplit"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("part-*"):
        old.unlink()
    (out / "MANIFEST.json").unlink(missing_ok=True)

    print(f"== archive sandsplit → {out}", flush=True)
    prod = subprocess.Popen(
        ["tar", "-C", str(dest.parent), "-cf", "-", dest.name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cons = subprocess.Popen(
        [
            "split",
            "-b",
            str(CHUNK),
            "-d",
            "-a",
            "4",
            "-",
            str(out / "part-"),
        ],
        stdin=prod.stdout,
        stderr=subprocess.PIPE,
    )
    assert prod.stdout is not None
    prod.stdout.close()
    _o, cons_err = cons.communicate()
    _o, prod_err = prod.communicate()
    if cons.returncode != 0 or prod.returncode not in (0, 1):
        err = ((prod_err or b"") + b" | " + (cons_err or b"")).decode(
            "utf-8", "replace"
        )[:800]
        print(f"archive échouée: {err}", flush=True)
        return None

    import json

    parts = sorted(out.glob("part-*"))
    info = {
        "original_name": f"{dest.name}.tar",
        "chunk_size": CHUNK,
        "parts": [{"name": p.name, "size": p.stat().st_size} for p in parts],
        "reassemble": f"cat part-* > {dest.name}.tar",
        "source_snapshot": str(dest),
        "original_size": sum(p.stat().st_size for p in parts),
        "definition": "hors image Linux de base (non-dpkg), livraison intégrale",
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )
    return out


if __name__ == "__main__":
    sys.exit(main())
