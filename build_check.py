"""Detect stale generated JavaScript after an update, including portable installs."""
from pathlib import Path
import hashlib
import sys

root=Path(__file__).resolve().parent
marker=root/"static"/".build-fingerprint"
sources=[root/"package.json",root/"tsconfig.json",*sorted((root/"src").rglob("*.ts"))]
outputs=[root/"static"/name for name in ("app.js","fuel-ui.js","writeoff-ui.js","operations-ui.js")]
h=hashlib.sha256()
for p in sources:
    h.update(p.relative_to(root).as_posix().encode()+b"\0"+p.read_bytes()+b"\0")
digest=h.hexdigest()
if len(sys.argv)==2 and sys.argv[1]=="mark":
    if not all(p.is_file() for p in outputs):raise SystemExit("Build output missing")
    marker.write_text(digest,encoding="ascii")
elif len(sys.argv)==1:
    raise SystemExit(0 if all(p.is_file() for p in outputs) and marker.is_file() and marker.read_text(encoding="ascii").strip()==digest else 1)
else:raise SystemExit(2)
