"""Portable single-instance lock and verified SQLite backup/restore."""
from contextlib import contextmanager, closing
from datetime import datetime, date
from pathlib import Path
import os
import sqlite3
import sys
import tempfile

@contextmanager
def instance_lock(root):
    root.mkdir(parents=True,exist_ok=True)
    with (root/"ogm.lock").open("a+b") as f:
        f.seek(0,2)
        if f.tell()==0:f.write(b"0");f.flush()
        f.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e:raise RuntimeError("OGM already running or database locked") from e
        try:yield
        finally:
            f.seek(0)
            if os.name=="nt":msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def validate(c):
    if c.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:raise ValueError("SQLite integrity check failed")
    if c.execute("PRAGMA foreign_key_check").fetchone():raise ValueError("Broken foreign keys")
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"employees","vehicles","cards","fuel_operations","writeoff_months"}<=tables:raise ValueError("Not an OGM database")

def backup(db, label="manual"):
    db=Path(db)
    if not db.is_file():raise ValueError("Database does not exist")
    directory=db.parent/"backups";directory.mkdir(exist_ok=True)
    target=directory/(datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-"+label+".sqlite3")
    tmp=target.with_suffix(".tmp")
    try:
        with closing(sqlite3.connect(db)) as src,closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst);validate(dst)
        os.replace(tmp,target);return target
    finally:tmp.unlink(missing_ok=True)

def daily(db):
    folder=Path(db).parent/"backups";prefix=date.today().strftime("%Y%m%d")
    if folder.is_dir() and any(folder.glob(prefix+"-*-daily.sqlite3")):return None
    return backup(db,"daily")

def restore(db,name):
    db=Path(db);folder=db.parent/"backups"
    if Path(name).name!=name or not name.endswith(".sqlite3"):raise ValueError("Select a backup filename from data/backups")
    source=folder/name
    if not source.is_file():raise ValueError("Backup not found")
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)) as src:validate(src)
    if db.exists():backup(db,"before-restore")
    # The process lock must be held and all database connections closed.
    for suffix in ("-wal","-shm","-journal"):
        if Path(str(db)+suffix).exists():raise ValueError("Database sidecar exists; close all database clients before restore")
    fd,tmpname=tempfile.mkstemp(prefix="restore-",suffix=".tmp",dir=db.parent);os.close(fd);tmp=Path(tmpname)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)) as src,closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst);validate(dst)
        os.replace(tmp,db)
    finally:tmp.unlink(missing_ok=True)

if __name__=="__main__":
    db=Path(__file__).resolve().parent/"data"/"ogm.sqlite3"
    try:
        with instance_lock(db.parent):
            if len(sys.argv)==2 and sys.argv[1]=="backup":print(backup(db))
            elif len(sys.argv)==3 and sys.argv[1]=="restore":
                restore(db,sys.argv[2]);print("Restore complete. Start OGM.")
            elif len(sys.argv)==2 and sys.argv[1]=="list":
                for p in sorted((db.parent/"backups").glob("*.sqlite3"),reverse=True):print(p.name)
            else:raise ValueError("Usage: maintenance.py backup | list | restore BACKUP.sqlite3")
    except (OSError,RuntimeError,ValueError,sqlite3.Error) as e:print("ERROR:",e,file=sys.stderr);sys.exit(1)
