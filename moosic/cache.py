import os
import shutil
import hashlib


def convert_size(sz):
    if not sz[-1].isdigit():
        sz, ch = int(sz[:-1]), sz[-1]

        if ch == 'G':
            sz = sz * 1024 * 1024 * 1024
        elif ch == 'M':
            sz = sz * 1024 * 1024
        elif ch == 'K':
            sz = sz * 1024

        return sz
    return int(sz)


class LRUDiskCacheFile(object):
    def __init__(self, parent, path):
        self.path = path
        self._parent = parent
        self._file = open(path, 'w')

    def __getattr__(self, attr):
        return getattr(self._file, attr)

    def close(self):
        self._file.close()
        self._parent._scan_file(self.path)
        self._parent._check()


class LRUDiskCache(object):
    def __init__(self, path, max_size=None):
        if not os.path.exists(path):
            os.mkdir(path)

        self.path = path
        self.max_size = convert_size(max_size)
        self._files = {}
        self._total_size = 0
        self._scan()

    def _scan_file(self, path):
        stat = os.stat(path)

        self._total_size += stat.st_size
        self._files[path] = {
            'size': stat.st_size,
            'last_access': stat.st_atime,
            'created': stat.st_mtime,
        }

    def _scan(self):
        for root, dirs, files in os.walk(self.path):
            for name in files:
                path = os.path.join(root, name)
                self._scan_file(path)

        self._check()

    def _check(self):
        if self._total_size > self.max_size:
            self._purge_bytes(self._total_size - self.max_size)

    def _purge_bytes(self, amount):
        sorted_files = sorted(self._files.items(), key=lambda i: i[1]['created'])

        for path, obj in sorted_files:
            os.remove(path)
            amount -= obj['size']

            if amount <= 0:
                return

    def _key_path(self, key):
        key = hashlib.md5(key).hexdigest()
        return os.path.join(self.path, key)

    def delete(self, key):
        path = self._key_path(key)
        obj = self._files[path]

        del self._files[path]
        os.remove(path)
        self._total_size -= obj['size']

    def put(self, key):
        return LRUDiskCacheFile(self, self._key_path(key))

    def put_from_path(self, key, path):
        new_path = self._key_path(key)
        shutil.copy(path, new_path)
        self._scan_file(new_path)
        self._check()

    def get(self, key):
        return open(self._key_path(key), 'r')

    def has(self, key):
        return self._key_path(key) in self._files
