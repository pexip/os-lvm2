#!/usr/bin/env python3

import os
import re
import sys
import tarfile


class Version:
    __slots__ = '_match',

    _version_rules = r'''
^
(?P<version_upstream>
    2\.02
    \.
    \d+
)
(?:
  -[^-]+
)?
$
'''
    _version_re = re.compile(_version_rules, re.X)

    def __init__(self, version):
        match = self._version_re.match(version)
        if match is None:
            raise ValueError('Invalid lvm version: {}'.format(version))
        self._match = match.groupdict()

    @property
    def upstream(self):
        return self._match['version_upstream']

    @property
    def upstream_tag(self):
        return 'v{}'.format(re.sub(r'\.', '_', self._match['version_upstream']))


class Main(object):
    log = sys.stdout.write

    def __init__(self, tar, version):
        self.tar = tarfile.open(tar)
        self.version = Version(version)

        self.orig_dir = 'lvm2-{}'.format(self.version.upstream)
        self.orig_tar = 'lvm2_{}.orig.tar.xz'.format(self.version.upstream)

    def __call__(self):
        out = '../{}'.format(self.orig_tar)
        self.log('Generate tarball {}\n'.format(out))

        try:
            os.stat(out)
            raise RuntimeError('Destination {} already exists'.format(out))
        except OSError: pass

        try:
            with tarfile.open(out, 'w:xz') as f:
                for tarinfo in self.tar:
                    name = tarinfo.name.split('/', 1)
                    name[0] = self.orig_dir
                    tarinfo.name = '/'.join(name)
                    tarinfo.uid = tarinfo.gid = 0
                    tarinfo.uname = tarinfo.gname = 'root'
                    f.addfile(tarinfo, self.tar.extractfile(tarinfo))
        except:
            os.unlink(out)
            raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('tar')
    parser.add_argument('version')

    args = parser.parse_args()
    Main(args.tar, args.version)()
