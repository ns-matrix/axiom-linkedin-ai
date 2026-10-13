"""Local JSON file persistence for recordings, skills, plans and sessions.

Everything stays on this machine. Files are written atomically and reads are
guarded by a lock so the API and tests can run concurrently.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

_LOCK = threading.RLock()
T = TypeVar("T", bound=BaseModel)

_SAFE_NAME = re.compile(r"[^a-z0-9_\-]+")


def data_dir() -> pathlib.Path:
    root = pathlib.Path(os.getenv("AXIOM_DATA_DIR", str(pathlib.Path(__file__).resolve().parent.parent / "data")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _folder(kind: str) -> pathlib.Path:
    path = data_dir() / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_for(kind: str, item_id: str) -> pathlib.Path:
    safe = _SAFE_NAME.sub("_", item_id.lower()).strip("_") or "item"
    return _folder(kind) / f"{safe}.json"


def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_model(kind: str, model: BaseModel) -> pathlib.Path:
    ident = (getattr(model, "session_id", None) or getattr(model, "plan_id", None)
             or getattr(model, "recording_id", None) or getattr(model, "skill_id", None))
    if not ident:
        raise ValueError("model has no identifier field")
    path = _file_for(kind, str(ident))
    tmp = path.with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return path


def load_model(kind: str, item_id: str, model_cls: type[T]) -> T | None:
    data = _read_json(_file_for(kind, item_id))
    if data is None:
        return None
    try:
        return model_cls.model_validate(data)
    except Exception:
        return None


def list_models(kind: str, model_cls: type[T]) -> list[T]:
    items: list[T] = []
    with _LOCK:
        paths = sorted(_folder(kind).glob("*.json"))
    for path in paths:
        data = _read_json(path)
        if data is None:
            continue
        try:
            items.append(model_cls.model_validate(data))
        except Exception:
            continue
    return items


def delete_model(kind: str, item_id: str) -> bool:
    path = _file_for(kind, item_id)
    with _LOCK:
        if path.exists():
            path.unlink()
            return True
    return False


def archive_model(kind: str, item_id: str, version: int) -> None:
    """Keep an immutable copy of an older version before overwriting."""
    src = _file_for(kind, item_id)
    if not src.exists():
        return
    archive = _folder(kind) / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    dst = archive / f"{src.stem}.v{version}.json"
    with _LOCK:
        if not dst.exists():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def mutate(kind: str, item_id: str, model_cls: type[T], fn: Callable[[T], T]) -> T | None:
    with _LOCK:
        current = load_model(kind, item_id, model_cls)
        if current is None:
            return None
        updated = fn(current)
        save_model(kind, updated)
        return updated
