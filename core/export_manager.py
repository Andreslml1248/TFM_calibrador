#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/export_manager.py
Gestiona la cola local de exportaciones y su copia automatica a una USB.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import string
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from config import hardware as config


@dataclass
class ExportSyncResult:
    usb_detected: bool
    usb_root: Optional[str]
    target_dir: Optional[str]
    pending_count: int
    copied_count: int
    preferred_usb_path: Optional[str] = None
    last_error: Optional[str] = None


class ExportManager:
    def __init__(
        self,
        base_dir: str,
        *,
        usb_roots_provider: Optional[Callable[[], list[str]]] = None,
    ) -> None:
        self.base_dir = os.path.abspath(base_dir)
        self.results_dir = os.path.join(self.base_dir, str(config.RESULTS_DIR))
        self.data_dir = os.path.join(self.base_dir, str(config.DATA_DIR))
        self.queue_file = os.path.join(self.data_dir, str(config.EXPORT_QUEUE_FILE))
        self.usb_export_dirname = str(config.USB_EXPORT_DIRNAME)
        self._usb_roots_provider = usb_roots_provider or self._discover_usb_roots
        self.ensure_results_dir()
        os.makedirs(self.data_dir, exist_ok=True)

    def ensure_results_dir(self) -> str:
        os.makedirs(self.results_dir, exist_ok=True)
        return self.results_dir

    def register_export(self, local_path: str) -> ExportSyncResult:
        full_path = os.path.abspath(local_path)
        if not os.path.isfile(full_path):
            raise FileNotFoundError(full_path)

        records = self._load_pending_records()
        norm_target = self._norm_path(full_path)
        if all(self._norm_path(item["local_path"]) != norm_target for item in records):
            records.append(
                {
                    "local_path": full_path,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "last_error": None,
                }
            )
            self._save_pending_records(records)
        return self.sync_pending_exports(preferred_local_path=full_path)

    def sync_pending_exports(self, preferred_local_path: Optional[str] = None) -> ExportSyncResult:
        records = self._load_pending_records()
        usb_roots = self._get_usb_roots()
        usb_root = usb_roots[0] if usb_roots else None
        target_dir = os.path.join(usb_root, self.usb_export_dirname) if usb_root else None
        copied_paths: list[tuple[str, str]] = []
        last_error: Optional[str] = None

        if usb_root and target_dir:
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as exc:
                last_error = str(exc)
            else:
                remaining: list[dict[str, Optional[str]]] = []
                for record in records:
                    src = str(record["local_path"])
                    dst = os.path.join(target_dir, os.path.basename(src))
                    try:
                        shutil.copy2(src, dst)
                    except Exception as exc:
                        record["last_error"] = str(exc)
                        remaining.append(record)
                        last_error = str(exc)
                    else:
                        copied_paths.append((src, dst))
                records = remaining

        self._save_pending_records(records)

        preferred_usb_path = None
        if preferred_local_path:
            norm_preferred = self._norm_path(preferred_local_path)
            for src, dst in copied_paths:
                if self._norm_path(src) == norm_preferred:
                    preferred_usb_path = dst
                    break

        return ExportSyncResult(
            usb_detected=bool(usb_root),
            usb_root=usb_root,
            target_dir=target_dir,
            pending_count=len(records),
            copied_count=len(copied_paths),
            preferred_usb_path=preferred_usb_path,
            last_error=last_error,
        )

    def format_status_text(self, sync_result: ExportSyncResult) -> str:
        pending = int(sync_result.pending_count)
        if sync_result.usb_detected and sync_result.usb_root:
            usb_name = self._display_usb_name(sync_result.usb_root)
            return f"USB: {usb_name} | Pend.: {pending}"
        if pending > 0:
            return f"USB: no | Pend.: {pending}"
        return "USB: no | Pend.: 0"

    @staticmethod
    def _norm_path(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _load_pending_records(self) -> list[dict[str, Optional[str]]]:
        if not os.path.isfile(self.queue_file):
            return []
        try:
            with open(self.queue_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception:
            return []
        if not isinstance(raw_data, list):
            return []

        cleaned: list[dict[str, Optional[str]]] = []
        seen: set[str] = set()
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            local_path = item.get("local_path")
            if not isinstance(local_path, str) or not local_path.strip():
                continue
            full_path = os.path.abspath(local_path)
            if not os.path.isfile(full_path):
                continue
            norm_path = self._norm_path(full_path)
            if norm_path in seen:
                continue
            cleaned.append(
                {
                    "local_path": full_path,
                    "created_at": str(item.get("created_at") or ""),
                    "last_error": str(item.get("last_error") or "") or None,
                }
            )
            seen.add(norm_path)
        return cleaned

    def _save_pending_records(self, records: list[dict[str, Optional[str]]]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)

    def _get_usb_roots(self) -> list[str]:
        roots = []
        seen: set[str] = set()
        for path in self._usb_roots_provider():
            if not isinstance(path, str):
                continue
            full_path = os.path.abspath(path)
            norm_path = self._norm_path(full_path)
            if norm_path in seen:
                continue
            if not os.path.isdir(full_path):
                continue
            if not os.access(full_path, os.W_OK):
                continue
            roots.append(full_path)
            seen.add(norm_path)
        return roots

    def _discover_usb_roots(self) -> list[str]:
        if os.name == "nt":
            return self._discover_windows_usb_roots()
        return self._discover_posix_usb_roots()

    def _discover_posix_usb_roots(self) -> list[str]:
        candidates: list[str] = []
        scan_roots = ("/media", "/run/media", "/mnt")
        queue: list[tuple[str, int]] = []

        for scan_root in scan_roots:
            if os.path.isdir(scan_root):
                queue.append((scan_root, 0))

        seen: set[str] = set()
        while queue:
            current, depth = queue.pop(0)
            try:
                entries = sorted(
                    [entry for entry in os.scandir(current) if entry.is_dir(follow_symlinks=False)],
                    key=lambda entry: entry.path.lower(),
                )
            except Exception:
                continue

            for entry in entries:
                path = entry.path
                norm_path = self._norm_path(path)
                if norm_path in seen:
                    continue
                seen.add(norm_path)
                try:
                    if os.path.ismount(path) and os.access(path, os.W_OK):
                        candidates.append(path)
                        continue
                except Exception:
                    pass
                if depth < 2:
                    queue.append((path, depth + 1))
        return candidates

    @staticmethod
    def _discover_windows_usb_roots() -> list[str]:
        candidates: list[str] = []
        drive_type_removable = 2
        get_drive_type = getattr(ctypes.windll.kernel32, "GetDriveTypeW", None)
        if get_drive_type is None:
            return candidates

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if not os.path.exists(root):
                    continue
                if int(get_drive_type(root)) != drive_type_removable:
                    continue
                if os.access(root, os.W_OK):
                    candidates.append(root)
            except Exception:
                continue
        return candidates

    @staticmethod
    def _display_usb_name(path: str) -> str:
        clean_path = path.rstrip("\\/")
        name = os.path.basename(clean_path)
        if name:
            return name
        return clean_path
