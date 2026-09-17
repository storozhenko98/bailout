#!/usr/bin/env python3
"""Exercise installer platform selection, checksums, and preserving existing binaries."""
import hashlib
import io
import os
import pathlib
import subprocess
import tarfile
import tempfile

installer = pathlib.Path(__file__).resolve().parent.parent / "install.sh"
with tempfile.TemporaryDirectory(prefix="bailout-installer-") as tmp:
    root = pathlib.Path(tmp)
    tools = root / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text("""#!/usr/bin/env python3
import os, pathlib, sys, shutil
args=sys.argv[1:]
if '-w' in args:
 print('https://github.com/storozhenko98/bailout/releases/tag/v0.1.0',end='')
else:
 url=next(a for a in args if a.startswith('https://'))
 shutil.copyfile(pathlib.Path(os.environ['FIXTURES']) / url.rsplit('/',1)[1],args[args.index('-o')+1])
""")
    uname = tools / "uname"
    uname.write_text('#!/bin/sh\nif [ "$1" = -s ]; then echo "$TEST_OS"; else echo "$TEST_ARCH"; fi\n')
    curl.chmod(0o755)
    uname.chmod(0o755)
    content = b"#!/bin/sh\necho 'bailout fixture'\n"
    hashes = []
    for name in ["macos-arm64", "linux-x64", "linux-arm64"]:
        asset = root / f"bailout-{name}.tar.gz"
        with tarfile.open(asset, "w:gz") as archive:
            info = tarfile.TarInfo("bailout")
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
        hashes.append(f"{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}\n")
    checksums = root / "SHA256SUMS"
    checksums.write_text(''.join(hashes))
    destination = root / "installed"
    env = {**os.environ, 'PATH': str(tools) + ':' + os.environ['PATH'], 'FIXTURES': str(root), 'BAILOUT_INSTALL_DIR': str(destination)}
    for os_name, arch in [('Darwin','arm64'), ('Linux','x86_64'), ('Linux','aarch64')]:
        env.update(TEST_OS=os_name, TEST_ARCH=arch)
        result = subprocess.run(['bash', str(installer)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (destination / 'bailout').read_bytes() == content
    checksums.write_text('0' * 64 + '  bailout-linux-arm64.tar.gz\n')
    result = subprocess.run(['bash', str(installer)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Checksum mismatch' in result.stderr
    assert (destination / 'bailout').read_bytes() == content
    env.update(TEST_OS='Darwin', TEST_ARCH='x86_64')
    result = subprocess.run(['bash', str(installer)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert (destination / 'bailout').read_bytes() == content
print('PASS: three supported platforms; checksum mismatch preserves existing install; Intel Mac rejected')
