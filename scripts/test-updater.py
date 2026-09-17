#!/usr/bin/env python3
"""Real release binary against local GitHub-shaped fixtures; no network or install changes."""
import hashlib
import io
import json
import os
import pathlib
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

binary = pathlib.Path(sys.argv[1]).resolve()
original = binary.read_bytes()
version = subprocess.check_output([binary, '--version'], text=True).strip().split()[-1]
name = 'macos-arm64' if sys.platform == 'darwin' else 'linux-arm64' if platform.machine() in ('arm64', 'aarch64') else 'linux-x64'
with tempfile.TemporaryDirectory(prefix='bailout-updater-') as folder:
    root = pathlib.Path(folder).resolve(); tools = root/'tools'; tools.mkdir()
    curl = tools/'curl'
    curl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
args=sys.argv[1:]; root=pathlib.Path(os.environ['FIXTURES'])
assert args[0]=='-q'
assert args[args.index('--proto')+1]=='=https'
assert args[args.index('--proto-redir')+1]=='=https'
with (root/'calls').open('a') as f: f.write(json.dumps(args)+'\\n')
if '--head' in args:
 if os.environ.get('MODE')=='offline': sys.exit(22)
 if os.environ.get('MODE')=='slow': (root/'checking').touch(); time.sleep(20)
 print('https://github.com/storozhenko98/bailout/releases/tag/v'+os.environ['LATEST'],end='')
else:
 if os.environ.get('MODE')=='incomplete': sys.exit(18)
 url=args[-1]
 assert url.startswith('https://github.com/storozhenko98/bailout/releases/download/v')
 shutil.copyfile(root/url.rsplit('/',1)[1],args[args.index('--output')+1])
'''); curl.chmod(0o755)
    installed = root/'bailout'
    candidate = b'''#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1:]==['--version']:
 print('bailout 99.0.0')
else:
 pathlib.Path(os.environ['FIXTURES'],'restarted').write_text(json.dumps({'args':sys.argv[1:], 'cwd':os.getcwd(), 'input':sys.stdin.read(), 'marker':os.environ.get('BAILOUT_UPDATE_RESTART'), 'api':os.environ.get('BAILOUT_API_URL')}))
'''
    asset = root/f'bailout-{name}.tar.gz'
    def archive(content=candidate, kind=tarfile.REGTYPE):
        with tarfile.open(asset, 'w:gz') as tar:
            info=tarfile.TarInfo('bailout'); info.mode=0o755; info.type=kind
            if kind == tarfile.REGTYPE: info.size=len(content); tar.addfile(info,io.BytesIO(content))
            else: info.linkname=str(root/'outside'); tar.addfile(info)
        (root/'SHA256SUMS').write_text(hashlib.sha256(asset.read_bytes()).hexdigest()+'  '+asset.name+'\n')
    def setup(**extra):
        installed.write_bytes(original); installed.chmod(0o755)
        for name in ['calls', 'restarted', 'checking']:
            (root/name).unlink(missing_ok=True)
        env={**os.environ,'PATH':str(tools)+':'+os.environ['PATH'],'FIXTURES':str(root),'LATEST':'99.0.0','BAILOUT_API_URL':'http://127.0.0.1:1','NO_COLOR':'1'}
        for name in ['BAILOUT_NO_UPDATE','BAILOUT_UPDATE_RESTART','MODE']: env.pop(name,None)
        return {**env,**extra}
    def run(env, args=['update'], text=''):
        return subprocess.run([installed,*args],env=env,cwd=root,input=text,capture_output=True,text=True,timeout=15)
    def unchanged():
        assert installed.read_bytes()==original
        assert not (root/'restarted').exists()
        assert not list(root.glob('.bailout-update-*'))
    for latest in [version,'0.0.1']:
        r=run(setup(LATEST=latest)); assert r.returncode==0,(r.stdout,r.stderr); unchanged()
    archive()
    for mode in ['offline','incomplete']:
        r=run(setup(MODE=mode)); assert r.returncode!=0; unchanged()
    (root/'SHA256SUMS').write_text('0'*64+'  '+asset.name+'\n')
    r=run(setup()); assert 'Checksum mismatch' in r.stderr; unchanged()
    (root/'outside').write_text('do not touch'); (root/'outside').chmod(0o600)
    for kind in [tarfile.SYMTYPE,tarfile.LNKTYPE]:
        archive(kind=kind); r=run(setup()); assert r.returncode!=0; unchanged()
        assert (root/'outside').read_text()=='do not touch' and (root/'outside').stat().st_mode & 0o777==0o600
    archive(candidate.replace(b'99.0.0',b'98.0.0'))
    r=run(setup()); assert 'version check' in r.stderr; unchanged()
    archive()
    env=setup(); r=run(env,['--model','auto','repair my config'],'stdin is preserved\n'); assert r.returncode==0,(r.stdout,r.stderr)
    record=json.loads((root/'restarted').read_text())
    assert record['args']==['--model','auto','repair my config'] and record['cwd']==str(root)
    assert record['input']=='stdin is preserved\n' and record['api']==env['BAILOUT_API_URL']
    assert record['marker'].endswith(':99.0.0') and installed.read_bytes()==candidate
    assert not list(root.glob('.bailout-update-*'))
    for args in [['--help'],['--version']]:
        assert run(setup(),args).returncode==0
        assert not (root/'calls').exists(); unchanged()
    r=run(setup(BAILOUT_NO_UPDATE='1'),['hello']); assert not (root/'calls').exists(); unchanged()
    r=run(setup(MODE='offline'),['hello']); assert 'continuing with' in r.stderr; unchanged()
    env=setup(MODE='slow'); p=subprocess.Popen([installed,'update'],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+5
    while not (root/'checking').exists() and time.monotonic()<deadline: time.sleep(.02)
    assert (root/'checking').exists()
    second=run(env); assert 'Another startup' in second.stderr
    p.send_signal(signal.SIGINT); _,err=p.communicate(timeout=5); assert p.returncode==130,(p.returncode,err); unchanged()
print('PASS: startup update/restart preserves args, cwd, stdin and backend; failures, links, concurrency, cancellation and opt-out preserve installation')
