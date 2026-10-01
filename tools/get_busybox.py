#!/usr/bin/env python3
"""Download a statically linked riscv64 busybox (from Alpine Linux).

    python tools/get_busybox.py            # -> ./busybox.static
"""

import argparse
import io
import re
import sys
import tarfile
import urllib.request

REPO = 'https://dl-cdn.alpinelinux.org/alpine/v3.20/main/riscv64/'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-o', '--output', default='busybox.static')
    a = p.parse_args()
    index = urllib.request.urlopen(REPO, timeout=30).read().decode()
    names = sorted(set(re.findall(r'busybox-static-[0-9][^"]*?\.apk', index)))
    if not names:
        sys.exit('could not find busybox-static in ' + REPO)
    url = REPO + names[-1]
    print('downloading', url)
    apk = urllib.request.urlopen(url, timeout=60).read()
    # an .apk is a series of concatenated gzip'd tar segments
    with tarfile.open(fileobj=io.BytesIO(apk), mode='r:gz') as t:
        for m in t:
            if m.name.endswith('bin/busybox.static'):
                data = t.extractfile(m).read()
                break
        else:
            sys.exit('busybox.static not found in package')
    with open(a.output, 'wb') as f:
        f.write(data)
    print('wrote %s (%d KiB)' % (a.output, len(data) >> 10))


if __name__ == '__main__':
    main()
