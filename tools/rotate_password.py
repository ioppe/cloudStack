#!/usr/bin/env python3
"""Rotate the single administrator password without printing it or command arguments."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller
from api_client import Client


def main():
    root = Path(__file__).resolve().parents[1]
    path = root / 'ADMIN-CREDENTIALS.txt'
    client = Client('http://192.168.0.30:8089', str(path))
    client.login()
    new_password = secrets.token_urlsafe(27)
    client.call('POST', '/api/password', {'current_password': client.password, 'password': new_password})
    text = path.read_text(encoding='utf-8')
    lines = [('密码：' + new_password) if line.startswith('密码：') else line for line in text.splitlines()]
    temporary = path.with_name('.ADMIN-CREDENTIALS.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')
    os.replace(str(temporary), str(path))
    remote = "import json,os,sys; p='/etc/vm-platform/controller.json'; c=json.load(open(p,encoding='utf-8')); c['password_hash']=json.load(sys.stdin)['password_hash']; tmp=p+'.rotate-tmp'; f=open(tmp,'w',encoding='utf-8'); json.dump(c,f,indent=2); f.close(); os.chmod(tmp,0o600); os.rename(tmp,p)"
    import shlex
    proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@192.168.0.30', '/usr/bin/python3 -c ' + shlex.quote(remote)], input=json.dumps({'password_hash': controller.hash_password(new_password)}).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
    if proc.returncode:
        raise RuntimeError('Password rotated in database and credentials; private bootstrap hash synchronization failed')
    check = Client(client.base, str(path))
    check.login()
    check.call('POST', '/api/logout')
    print('Administrator password rotated, old sessions revoked, private credentials remain mode 0600, new login verified.')


if __name__ == '__main__':
    main()
