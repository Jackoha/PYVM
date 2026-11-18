#!/usr/bin/env python3
"""Download a Linux kernel version from kernel.org and build an Image for pyvm.

    python3 tools/get_kernel.py --list              what kernel.org offers now
    python3 tools/get_kernel.py 6.6.162             fetch, verify, build Image-6.6.162
    python3 tools/get_kernel.py 6.6.162 --fetch-only          stop after extracting
    python3 tools/get_kernel.py --installed         kernels already built here

Each step is skipped when its result is already there, so re-running after an
interrupted download or a failed build costs nothing:

    linux-<version>.tar.xz   downloaded (resumable, several connections at once)
    checksum                 compared against kernel.org's signed sha256sums.asc
    linux-<version>/         extracted
    Image-<version>          built by tools/build_linux.sh (same config as Image)

The boot menu (`python3 -m pyvm`) drives this with `g`, so a new version can be
fetched, built and booted without leaving it.

Only released versions can be had this way.  Release candidates are published
without a checksum list that covers them, and this script will not install
something it cannot check.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASES = 'https://www.kernel.org/releases.json'
MIRROR = 'https://cdn.kernel.org/pub/linux/kernel'
CONNECTIONS = 16                  # kernel.org is often slower than one stream
VERSION_RE = re.compile(r'\d+\.\d+(\.\d+)?\Z')


class Fail(Exception):
    """Something the user needs to know about, reported without a traceback."""


def say(text=''):
    """print(), flushed: progress goes to stderr, and a log must interleave."""
    print(text, flush=True)


def tty():
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


# ------------------------------------------------------------------- kernel.org
def series(version):
    """'6.6.162' -> 'v6.x', the directory kernel.org files it under."""
    return 'v%s.x' % version.split('.')[0]


def tarball(version):
    return 'linux-%s.tar.xz' % version


def url_of(version, name=None):
    return '%s/%s/%s' % (MIRROR, series(version), name or tarball(version))


def fetch(url, timeout=60):
    """-> bytes.  Used for the small files (release list, checksums)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'pyvm-get-kernel'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as e:
        raise Fail('cannot reach %s: %s' % (url, e))


def releases():
    """The versions kernel.org is currently publishing, newest series first."""
    try:
        data = json.loads(fetch(RELEASES))
    except ValueError as e:
        raise Fail('kernel.org returned something that is not JSON: %s' % e)
    out = []
    for r in data.get('releases', []):
        v = r.get('version', '')
        out.append({'version': v,
                    'moniker': r.get('moniker', ''),
                    'date': (r.get('released') or {}).get('isodate', ''),
                    'eol': bool(r.get('iseol')),
                    'usable': bool(VERSION_RE.match(v))})
    return out


def published_sha256(version):
    """The checksum kernel.org publishes for this tarball.

    sha256sums.asc is a PGP-signed list covering every file in the series
    directory.  We read the sum out of it; `--verify-signature` additionally
    checks the PGP signature on the tarball itself, which needs gpg and the
    kernel.org signing keys.
    """
    want = tarball(version)
    text = fetch(url_of(version, 'sha256sums.asc'), timeout=120).decode('utf-8', 'replace')
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == want and re.fullmatch(r'[0-9a-f]{64}', parts[0]):
            return parts[0]
    raise Fail('kernel.org has no checksum for %s -- is %s a released version?\n'
               '       (try --list; release candidates are not supported)'
               % (want, version))


# ---------------------------------------------------------------------- download
def sha256_of(path, progress=False):
    h = hashlib.sha256()
    size = os.path.getsize(path)
    show = progress and size and tty()        # a log does not want 100 updates
    done = 0
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
            done += len(chunk)
            if show:
                sys.stderr.write('\r  checking... %3d%%' % (100 * done // size))
    if show:
        sys.stderr.write('\r' + ' ' * 24 + '\r')
    return h.hexdigest()


def content_length(url):
    req = urllib.request.Request(url, method='HEAD',
                                 headers={'User-Agent': 'pyvm-get-kernel'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            n = r.headers.get('Content-Length')
            ranges = (r.headers.get('Accept-Ranges') or '').lower()
            return (int(n) if n else None), ('bytes' in ranges)
    except (urllib.error.URLError, OSError, ValueError):
        return None, False


def curl_range(url, path, start, end, tries=30):
    """Fetch bytes start..end into path, resuming after a dropped connection."""
    open(path, 'wb').close()
    for _ in range(tries):
        have = os.path.getsize(path)
        if start + have > end:
            return True
        with open(path, 'ab') as out:
            subprocess.run(['curl', '-sfL', '--connect-timeout', '20',
                            '-r', '%d-%d' % (start + have, end), url],
                           stdout=out, stderr=subprocess.DEVNULL)
    return os.path.getsize(path) > end - start


def download(url, out, parallel=CONNECTIONS):
    """Download url to out.  Uses several connections when that is possible."""
    size, ranged = content_length(url)
    tmp = out + '.tmp'
    if size and ranged and parallel > 1 and shutil.which('curl'):
        sys.stderr.write('  %.0f MB over %d connections\n' % (size / 1e6, parallel))
        chunk = -(-size // parallel)
        jobs = []
        for i in range(parallel):
            s = i * chunk
            e = min(s + chunk - 1, size - 1)
            if s <= e:
                jobs.append((i, s, e))
        parts = ['%s.part%d' % (out, i) for i, _, _ in jobs]
        try:
            with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
                ok = list(pool.map(lambda j: curl_range(url, '%s.part%d' % (out, j[0]),
                                                        j[1], j[2]), jobs))
            if all(ok):
                with open(tmp, 'wb') as dst:
                    for p in parts:
                        with open(p, 'rb') as src:
                            shutil.copyfileobj(src, dst)
                if os.path.getsize(tmp) == size:
                    os.replace(tmp, out)
                    return
            sys.stderr.write('  parallel download fell short, retrying with one '
                             'connection\n')
        finally:
            for p in parts:
                if os.path.exists(p):
                    os.remove(p)
    if shutil.which('curl'):
        subprocess.run(['curl', '-fL', '--retry', '10', '-C', '-', '-o', out, url],
                       check=True)
        return
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={'User-Agent': 'pyvm-get-kernel'}), timeout=120) as r, \
            open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, out)


def verify_signature(version, tar_xz):
    """Check kernel.org's PGP signature, if gpg can.  -> a one-line verdict."""
    gpg = shutil.which('gpg') or shutil.which('gpg2')
    if not gpg:
        return 'not checked (no gpg installed)'
    if not shutil.which('xz'):
        return 'not checked (no xz installed)'
    sign = tar_xz[:-3] + '.sign'                 # linux-X.tar.sign, over the .tar
    if not os.path.exists(sign):
        try:
            with open(sign, 'wb') as f:
                f.write(fetch(url_of(version, os.path.basename(sign))))
        except Fail as e:
            return 'not checked (%s)' % e
    un = subprocess.Popen(['xz', '-dc', tar_xz], stdout=subprocess.PIPE)
    try:
        p = subprocess.run([gpg, '--verify', sign, '-'], stdin=un.stdout,
                           capture_output=True)
    finally:
        un.stdout.close()
        un.wait()
    err = p.stderr.decode('utf-8', 'replace')
    if p.returncode == 0:
        who = re.search(r'Good signature from "([^"]+)"', err)
        return 'good signature from %s' % (who.group(1) if who else 'kernel.org')
    if 'No public key' in err or 'public key not found' in err:
        return ("no kernel.org signing key in your keyring -- import it with\n"
                '       gpg --locate-keys torvalds@kernel.org gregkh@kernel.org')
    return 'FAILED: %s' % err.strip().splitlines()[-1:] or 'FAILED'


# ------------------------------------------------------------------------ steps
def ensure_tarball(version, root, want_sig=False):
    """Download and check linux-<version>.tar.xz.  -> its path."""
    path = os.path.join(root, tarball(version))
    say('==> checksum from kernel.org')
    want = published_sha256(version)
    if os.path.exists(path):
        say('==> %s is already here' % tarball(version))
        if sha256_of(path, progress=True) == want:
            say('    checksum ok')
        else:
            say('    checksum does not match, downloading again')
            os.remove(path)
    if not os.path.exists(path):
        say('==> downloading %s' % tarball(version))
        download(url_of(version), path)
        if sha256_of(path, progress=True) != want:
            os.remove(path)
            raise Fail('checksum mismatch after downloading %s (removed it)' % tarball(version))
        say('    checksum ok')
    if want_sig:
        say('==> signature: %s' % verify_signature(version, path))
    return path


def ensure_source(version, root):
    """Extract the tarball.  -> the source directory."""
    src = os.path.join(root, 'linux-%s' % version)
    if os.path.exists(os.path.join(src, 'Makefile')):
        say('==> source: %s (present)' % os.path.basename(src))
        return src
    if not shutil.which('tar'):
        raise Fail('tar is needed to extract the kernel source')
    say('==> extracting linux-%s.tar.xz' % version)
    subprocess.run(['tar', 'xf', tarball(version)], cwd=root, check=True)
    if not os.path.exists(os.path.join(src, 'Makefile')):
        raise Fail('%s did not contain linux-%s/' % (tarball(version), version))
    return src


def build(version, src, root, config='tiny', out=None, force=False):
    """Run tools/build_linux.sh.  -> the image path."""
    image = out or os.path.join(root, 'Image-%s' % version)
    if os.path.exists(image) and not force:
        say('==> %s is already built (use --force to build it again)'
              % os.path.basename(image))
        return image
    script = os.path.join(HERE, 'tools', 'build_linux.sh')
    say('==> building (this takes a few minutes)')
    r = subprocess.run([script, src, config, os.path.abspath(image)])
    if r.returncode != 0:
        raise Fail('the kernel build failed; the output above says why.\n'
                   '       A version pyvm has not been tried with may need a '
                   'newer compiler,\n       or changes to linux/pyvm.config.')
    if not os.path.exists(image):
        raise Fail('the build reported success but %s is missing' % image)
    return image


def installed(root):
    """-> [(path, version_or_None)] for the kernel images already built here."""
    sys.path.insert(0, HERE)
    from pyvm.launcher import discover, kernel_banner
    out = []
    for path in discover(root)[0]:
        banner = kernel_banner(path) or ''
        m = re.search(r'Linux (\S+)', banner)
        out.append((path, m.group(1) if m else None))
    return out


# -------------------------------------------------------------------------- CLI
def release_rows(root):
    """The release list, each row marked with whether it is already built here."""
    here = {v for _, v in installed(root) if v}
    rows = releases()
    for r in rows:
        r['installed'] = any(v == r['version'] or v.startswith(r['version'] + '-')
                             for v in here)
    return rows


def print_list(root, as_json=False):
    rows = release_rows(root)
    if as_json:
        json.dump(rows, sys.stdout)
        sys.stdout.write('\n')
        return
    say('kernel.org, as of now:\n')
    say('  %-10s %-14s %-11s %s' % ('series', 'version', 'released', ''))
    for r in rows:
        notes = []
        if r['eol']:
            notes.append('end of life')
        if not r['usable']:
            notes.append('not installable (no checksum list for it)')
        if r['installed']:
            notes.append('already built here')
        say('  %-10s %-14s %-11s %s'
              % (r['moniker'], r['version'], r['date'], '; '.join(notes)))
    say('\n  python3 tools/get_kernel.py <version>     to fetch and build one')
    say('  any released version works, not only the ones above')


def print_installed(root):
    rows = installed(root)
    if not rows:
        say('no kernel images here yet')
        return
    say('built here:\n')
    for path, version in rows:
        say('  %-24s %s' % (os.path.basename(path), version or 'unrecognised'))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='python3 tools/get_kernel.py', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('version', nargs='?', help='a released kernel version, e.g. 6.6.162')
    p.add_argument('--list', action='store_true', help='show what kernel.org offers')
    p.add_argument('--json', action='store_true',
                   help='with --list, print the release list as JSON (the boot menu '
                        'uses this)')
    p.add_argument('--installed', action='store_true', help='show what is built here')
    p.add_argument('--fetch-only', action='store_true', help='download and extract, do not build')
    p.add_argument('--config', default='tiny', choices=('tiny', 'defconfig'),
                   help="kernel config: 'tiny' (pyvm's, the default) or the stock riscv defconfig")
    p.add_argument('-o', '--output', help='name the image file (default Image-<version>)')
    p.add_argument('--force', action='store_true', help='rebuild even if the image exists')
    p.add_argument('--verify-signature', action='store_true',
                   help="also check kernel.org's PGP signature (needs gpg and their keys)")
    p.add_argument('--root', default=HERE, help='where to keep sources and images')
    a = p.parse_args(argv)

    if a.list:
        print_list(a.root, a.json)
        return 0
    if a.installed:
        print_installed(a.root)
        return 0
    if not a.version:
        p.error('give a version (see --list), or use --list / --installed')
    version = a.version.lstrip('vV')
    if not VERSION_RE.match(version):
        raise Fail('%r is not a released version number.  Release candidates and\n'
                   '       linux-next are not supported: kernel.org publishes no '
                   'checksum\n       list that covers them.' % a.version)

    tar = ensure_tarball(version, a.root, want_sig=a.verify_signature)
    src = ensure_source(version, a.root)
    if a.fetch_only:
        say('\nsource ready: %s' % src)
        return 0
    image = build(version, src, a.root, a.config, a.output, a.force)
    say('\nkernel image: %s' % image)
    say('boot it:      python3 -m pyvm -k %s -i rootfs.cpio'
          % os.path.relpath(image, os.getcwd()))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Fail as e:
        sys.stderr.write('get_kernel: %s\n' % e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.stderr.write('\ninterrupted\n')
        sys.exit(130)
