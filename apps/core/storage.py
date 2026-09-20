from pathlib import Path

from django.conf import settings


class LocalMediaStorage:
    def __init__(self, root=None):
        default_root = Path(settings.BASE_DIR) / "var" / "media"
        self.root = Path(root or getattr(settings, "MEDIA_ROOT", default_root))

    def _path(self, key):
        root = self.root.resolve()
        path = (root / key).resolve()
        if root != path and root not in path.parents:
            raise ValueError("Invalid storage key")
        return path

    def save(self, key, content):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(key)
        with path.open("wb") as destination:
            for chunk in content.chunks() if hasattr(content, "chunks") else iter(lambda: content.read(1024 * 1024), b""):
                destination.write(chunk)
        return key

    def open(self, key, mode="rb"):
        return self._path(key).open(mode)

    def exists(self, key):
        return self._path(key).exists()

    def delete(self, key) -> bool:
        """Remove the file; True when it existed (DRF-2170 media lifecycle)."""
        path = self._path(key)
        if not path.exists():
            return False
        path.unlink()
        return True
