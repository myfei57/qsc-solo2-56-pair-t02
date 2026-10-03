"""门动作不可变分段流水。

每樘门使用一个目录：``manifest.json`` 保存当前段与累计条数，``events-00001.jsonl``
等文件只追加、封口后不再改写。当前段用“临时文件写整段 + 原子替换”的方式追加，
避免进程崩溃留下半条 JSON；封口段视为历史，后续不再修改。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from ..errors import StorageError
from ..store import JsonStore

SEGMENT_LIMIT = 512


class DoorActionJournal:
    """按门机标识保存的分段只追加动作流水。"""

    def __init__(self, store: JsonStore, segment_limit: int = SEGMENT_LIMIT) -> None:
        self._store = store
        self._limit = max(1, segment_limit)
        self._lock = threading.RLock()

    @property
    def store(self) -> JsonStore:
        return self._store

    @staticmethod
    def _doc(psd_id: str) -> str:
        return f"degradation/actions/{psd_id}/manifest"

    def _dir(self, psd_id: str) -> str:
        return os.path.join(self._store.root, "degradation", "actions", psd_id)

    def path_of(self, psd_id: str) -> str:
        return self._store.path_of(self._doc(psd_id))

    def append(self, psd_id: str, record: dict) -> str:
        with self._lock:
            return self.append_many(psd_id, [record])

    def append_many(self, psd_id: str, records: list[dict]) -> str:
        """原子追加多条记录，必要时滚动新段。"""

        if not records:
            return self.path_of(psd_id)
        with self._lock:
            directory = self._dir(psd_id)
            os.makedirs(directory, exist_ok=True)
            manifest = self._read_manifest(directory)
            pending = list(records)
            while pending:
                segment = int(manifest["segment"])
                path = self._segment_path(directory, segment)
                current = self._read_file(path)
                capacity = self._limit - len(current)
                if capacity <= 0:
                    manifest["segment"] = segment + 1
                    manifest["sealed_until"] = segment
                    current = []
                    capacity = self._limit
                    path = self._segment_path(directory, manifest["segment"])
                accepted = pending[:capacity]
                self._replace_file(path, current + accepted)
                del pending[:capacity]
                manifest["count"] = int(manifest["count"]) + len(accepted)
            self._write_manifest(directory, manifest)
            return self.path_of(psd_id)

    def read_all(self, psd_id: str) -> list[dict]:
        """读取一樘门的全部历史。"""

        directory = self._dir(psd_id)
        if not os.path.isdir(directory):
            return []
        records: list[dict] = []
        for segment in self._segments(directory):
            records.extend(self._read_file(self._segment_path(directory, segment)))
        return records

    def read_tail(self, psd_id: str, limit: int) -> list[dict]:
        """只读取最后 ``limit`` 条事件，避免启动扫描整个寿命历史。"""

        directory = self._dir(psd_id)
        if not os.path.isdir(directory):
            return []
        records: list[dict] = []
        for segment in reversed(self._segments(directory)):
            records[0:0] = self._read_file(self._segment_path(directory, segment))
            if len(records) >= limit:
                break
        return records[-limit:]

    def count(self, psd_id: str) -> int:
        directory = self._dir(psd_id)
        manifest = self._read_manifest(directory)
        return int(manifest.get("count", 0))

    def list_psd_ids(self) -> list[str]:
        base = os.path.join(self._store.root, "degradation", "actions")
        if not os.path.isdir(base):
            return []
        return sorted(
            name
            for name in os.listdir(base)
            if os.path.isfile(os.path.join(base, name, "manifest.json"))
        )

    def _read_manifest(self, directory: str) -> dict:
        path = os.path.join(directory, "manifest.json")
        if not os.path.isfile(path):
            return {"segment": 1, "count": 0, "sealed_until": 0}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise StorageError(f"门动作清单损坏: {directory}", component="degradation") from error
        if not isinstance(manifest, dict):
            raise StorageError(f"门动作清单不是对象: {directory}", component="degradation")
        return manifest

    def _write_manifest(self, directory: str, manifest: dict) -> None:
        self._replace_file(os.path.join(directory, "manifest.json"), manifest, json_document=True)

    @staticmethod
    def _segment_path(directory: str, segment: int) -> str:
        return os.path.join(directory, f"events-{segment:05d}.jsonl")

    def _segments(self, directory: str) -> list[int]:
        segments: list[int] = []
        for name in os.listdir(directory):
            if not (name.startswith("events-") and name.endswith(".jsonl")):
                continue
            try:
                segments.append(int(name[len("events-") : -len(".jsonl")]))
            except ValueError:
                continue
        return sorted(segments)

    def _read_file(self, path: str) -> list[dict]:
        if not os.path.isfile(path):
            return []
        records: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise StorageError(f"门动作流水记录不是对象: {path}:{line_number}", component="degradation")
                    records.append(payload)
        except (OSError, json.JSONDecodeError) as error:
            raise StorageError(f"门动作流水损坏且历史不可变: {path}", component="degradation") from error
        return records

    def _replace_file(
        self,
        path: str,
        payload: object,
        json_document: bool = False,
    ) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        try:
            handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=directory,
                prefix=".degrade.",
                suffix=".tmp",
                delete=False,
            )
            with handle:
                if json_document:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                else:
                    for record in payload:
                        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        except OSError as error:
            raise StorageError(f"写入门动作流水失败: {error}", component="degradation") from error
