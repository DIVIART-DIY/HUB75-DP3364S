"""Offline publication checks; never opens GPIO or DRM devices."""
from pathlib import Path
import ast,hashlib,json,re,sys
root=Path(__file__).resolve().parents[1]
for p in root.rglob('*.py'):ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
for p in root.rglob('*'):
    if '.git' in p.parts or not p.is_file() or p.suffix in ('.zip','.so') or '.pio' in p.parts:continue
    data=p.read_bytes()
    assert (b'-----BEGIN '+b'OPENSSH PRIVATE KEY-----') not in data,str(p)
    if p.name!='check_publication.py':
        text=data.decode('utf-8',errors='replace')
        for forbidden in ('information'+'-clock','Information'+' Clock','diviart@','192.168.0.5','/home/diviart'):
            assert forbidden not in text,str(p)+': '+forbidden
    if p.suffix in ('.py','.dts','.c','.cpp','.h'):assert b'\r' not in data,str(p)
readme=(root/'README.md').read_text()
for target in re.findall(r'\]\(([^)]+)\)',readme):
    if '://' not in target and not target.startswith('#'):assert (root/target).exists(),target
print('PASS: syntax, local README links, privacy markers and LF endings')
