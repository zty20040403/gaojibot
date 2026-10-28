"""Explicit, checksum-bound project edits carried by an accepted ZIP artifact.

Missing files are unchanged, not deleted. The archive is never extracted onto
the host filesystem; callers stream the declared members into immutable CAS.

The root descriptor has format=1, project_id, base_version,
base_manifest_sha256, sorted files (path/sha256/size/mode), and sorted delete
paths. Changed bytes live at files/<path>. Optional rebuild replaces the recipe
(null removes it); omission preserves it and repins retained lockfile hashes.
Browser cache exclusions are inherited, never supplied by an artifact.
"""

from copy import deepcopy
import io
import json
import stat
import zipfile

from src.project_content import MAX_FILE_BYTES, MAX_MANIFEST_BYTES, MAX_VERSION_FILES
from src.project_content import manifest_bytes, project_path, retained_manifest

DESCRIPTOR = "gaoji-project-update.json"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate project update JSON key")
        result[key] = value
    return result


class ProjectUpdate:
    def __init__(self, content, *, project_id, base_version, base_manifest_sha256,
                 baseline, quota_bytes):
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_FILE_BYTES:
            raise ValueError("Project update archive exceeds artifact capacity")
        self.archive = zipfile.ZipFile(io.BytesIO(content))
        try:
            self._read(project_id, base_version, base_manifest_sha256, baseline, quota_bytes)
        except BaseException:
            self.close()
            raise

    def _read(self, project_id, base_version, base_hash, baseline, quota):
        members = self.archive.infolist()
        if len(members) > MAX_VERSION_FILES * 2 + 1:
            raise ValueError("Too many project update members")
        names, files, directories = set(), {}, set()
        for entry in members:
            name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
            if (project_path(name) != name or name.casefold() in names
                    or entry.orig_filename != entry.filename
                    or entry.is_dir() and entry.filename != name + "/"):
                raise ValueError("Duplicate or noncanonical project update path")
            names.add(name.casefold())
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (mode not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                    or entry.flag_bits & 1
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError("Unsafe project update member")
            limit = MAX_MANIFEST_BYTES if name == DESCRIPTOR else MAX_FILE_BYTES
            if entry.file_size < 0 or entry.file_size > limit:
                raise ValueError("Project update member exceeds size limit")
            if entry.is_dir():
                if entry.file_size:
                    raise ValueError("Project update directory contains data")
                directories.add(name)
            else:
                files[name] = entry
        if DESCRIPTOR not in files:
            raise ValueError("Missing project update descriptor")
        descriptor = json.loads(self.archive.read(files[DESCRIPTOR]), object_pairs_hook=_unique_object)
        required = {"format", "project_id", "base_version", "base_manifest_sha256", "files", "delete"}
        if (not isinstance(descriptor, dict) or set(descriptor) - required - {"rebuild"}
                or not required.issubset(descriptor) or type(descriptor["format"]) is not int
                or descriptor["format"] != 1
                or type(descriptor["project_id"]) is not int or descriptor["project_id"] != project_id
                or type(descriptor["base_version"]) is not int or descriptor["base_version"] != base_version
                or descriptor["base_manifest_sha256"] != base_hash):
            raise ValueError("Project update is not bound to its admitted baseline")
        changed = descriptor["files"]
        manifest_bytes({"format": 1, "files": changed}, quota_bytes=quota)
        deleted = descriptor["delete"]
        if (not isinstance(deleted, list) or len(deleted) > MAX_VERSION_FILES
                or any(not isinstance(p, str) or project_path(p) != p for p in deleted)
                or deleted != sorted(set(deleted))):
            raise ValueError("Invalid explicit project deletions")
        result = {item["path"]: item for item in baseline["files"]}
        if set(deleted) - result.keys() or set(deleted) & {item["path"] for item in changed}:
            raise ValueError("Project deletion must name an unchanged baseline file")
        for name in deleted:
            del result[name]
        result.update((item["path"], item) for item in changed)
        expected = {DESCRIPTOR, *("files/" + item["path"] for item in changed)}
        parents = {"/".join(p.split("/")[:i]) for p in expected for i in range(1, len(p.split("/")))}
        if set(files) != expected or not directories.issubset(parents):
            raise ValueError("Project update contains missing or undeclared files")
        if any(files["files/" + item["path"]].file_size != item["size"] for item in changed):
            raise ValueError("Project update file size differs from descriptor")
        spec = deepcopy(descriptor.get("rebuild", baseline.get("rebuild")))
        if spec is not None and "rebuild" not in descriptor:
            for locked in spec["locks"]:
                if locked["path"] not in result:
                    raise ValueError("Removed lockfile requires an explicit new rebuild specification")
                locked["sha256"] = result[locked["path"]]["sha256"]
        self.manifest = retained_manifest(sorted(result.values(), key=lambda item: item["path"]),
            rebuild=spec, browser_cache=baseline.get("browser_cache"))
        self.encoded = manifest_bytes(self.manifest, quota_bytes=quota)
        self.changed = changed

    def open(self, path):
        return self.archive.open("files/" + path)

    def close(self):
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
