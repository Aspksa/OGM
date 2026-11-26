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

def inspect_inbox(db,cache=None):
    """Local preview only. Do not move, delete, import or modify supplier documents."""
    import automation
    import fuel
    from hashlib import sha256
    root=Path(db).parent/"incoming"
    if root.is_symlink():raise ValueError("Папка входящих не должна быть ссылкой")
    root.mkdir(exist_ok=True)
    paths=sorted((p for p in root.iterdir() if p.suffix.lower() in (".xls",".xlsx") and not p.is_symlink() and p.is_file() and not p.name.startswith("~$")),key=lambda p:p.name.casefold())
    rows=[];cache=cache if cache is not None else {};fresh={}
    with closing(sqlite3.connect(Path(db).resolve().as_uri()+"?mode=ro",uri=True,timeout=5)) as c:
        c.row_factory=sqlite3.Row
        for p in paths[:50]:
            try:
                before=p.stat()
                if not 0<before.st_size<=fuel.MAX_FILE:raise ValueError("Неверный размер файла")
                key=(p.name,before.st_size,before.st_mtime_ns)
                if key in cache:meta=cache[key]
                else:
                    with p.open("rb") as stream:blob=stream.read(fuel.MAX_FILE+1)
                    after=p.stat()
                    if len(blob)!=before.st_size or after.st_mtime_ns!=before.st_mtime_ns:raise ValueError("Файл ещё записывается")
                    automation.file_kind(p.name,blob)
                    controls=[];ops=fuel.parse_file(p.name,blob,controls)
                    if not ops:raise ValueError("Не распознана выписка ГСМ")
                    meta={"digest":sha256(blob).hexdigest(),"months":sorted({r[1][:7] for r in ops}),"operations":len(ops)}
                fresh[key]=meta
                active=c.execute("SELECT 1 FROM fuel_imports WHERE digest=? AND cancelled_at IS NULL",(meta["digest"],)).fetchone()
                rows.append({"name":p.name,**meta,"state":"imported" if active else "ready"})
            except Exception as e:
                rows.append({"name":p.name,"state":"review","message":str(e)[:300]})
        integrity=[r[0] for r in c.execute("PRAGMA quick_check")]
        broken=c.execute("PRAGMA foreign_key_check").fetchone()
    cache.clear();cache.update(fresh)
    return {"folder":str(root),"scanned_at":datetime.now().isoformat(timespec="seconds"),"rows":rows,"omitted":max(0,len(paths)-50),"integrity_ok":integrity==["ok"] and broken is None}

def backup(db, label="manual", destination=None):
    db=Path(db)
    if not db.is_file():raise ValueError("Database does not exist")
    directory=Path(destination).resolve() if destination is not None else db.parent/"backups"
    if destination is not None and not directory.is_dir():raise ValueError("External backup directory does not exist")
    directory.mkdir(exist_ok=True)
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
    target=backup(db,"daily")
    # Keep recent automatic copies; manual and pre-restore copies are never pruned.
    copies=sorted(folder.glob("*-daily.sqlite3"),reverse=True)
    for old in copies[30:]:old.unlink(missing_ok=True)
    return target

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
            elif len(sys.argv)==3 and sys.argv[1]=="backup-to":print(backup(db,"external",sys.argv[2]))
            elif len(sys.argv)==3 and sys.argv[1]=="restore":
                restore(db,sys.argv[2]);print("Restore complete. Start OGM.")
            elif len(sys.argv)==2 and sys.argv[1]=="list":
                for p in sorted((db.parent/"backups").glob("*.sqlite3"),reverse=True):print(p.name)
            else:raise ValueError("Usage: maintenance.py backup | backup-to DIRECTORY | list | restore BACKUP.sqlite3")
    except (OSError,RuntimeError,ValueError,sqlite3.Error) as e:print("ERROR:",e,file=sys.stderr);sys.exit(1)
