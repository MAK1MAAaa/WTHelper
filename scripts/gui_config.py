"""Persist the desktop executable selected by the user outside the app folder."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

if __package__:
    from .runtime import state_root
else:
    from runtime import state_root


MAX_SETTINGS_BYTES = 16_384


def settings_path() -> Path:
    return state_root() / "settings.json"


def load_desktop_path() -> Path | None:
    try:
        path = settings_path()
        if path.stat().st_size > MAX_SETTINGS_BYTES:
            return None
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        value = data.get("desktop_executable") if isinstance(data, dict) else None
        return Path(value) if isinstance(value, str) and value and "\0" not in value else None
    except (OSError, ValueError, TypeError):
        return None


def save_desktop_path(path: Path) -> None:
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"desktop_executable": str(path)}, ensure_ascii=False, indent=2) + "\n"
    if len(content.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ValueError("桌宠路径过长，无法保存。")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                                         prefix=".settings-", suffix=".tmp", delete=False) as file:
            temporary = file.name
            file.write(content)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
