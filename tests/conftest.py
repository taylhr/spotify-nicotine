from typing import Any, Dict, List, Optional

import pytest

from spotify_nicotine.config import Config, DEFAULTS
from spotify_nicotine.models import Track


def make_cfg(**overrides: Any) -> Config:
    values: Dict[str, Any] = dict(DEFAULTS)
    values["formats"] = list(values["formats"])
    values.update(overrides)
    return Config(**values)


def make_track(
    name: str = "Hotel California",
    artists: Optional[List[str]] = None,
    album: str = "Hotel California",
    duration_ms: int = 391_000,
    track_id: str = "4uLU6hMCjMI75M1A2tKUQC",
) -> Track:
    return Track(
        id=track_id,
        name=name,
        artists=artists if artists is not None else ["Eagles"],
        album=album,
        duration_ms=duration_ms,
        isrc=None,
        positions=[0],
    )


def make_item(
    file_path: str,
    username: str = "peer1",
    size: int = 15_662_354,
    extension: str = "",
    file_attributes: Optional[Dict[str, Any]] = None,
    is_private: bool = False,
    free_upload_slots: bool = True,
    queue_position: int = 0,
    upload_speed: int = 0,
) -> Dict[str, Any]:
    return {
        "username": username,
        "file_path": file_path,
        "size": size,
        "extension": extension,
        "is_private": is_private,
        "free_upload_slots": free_upload_slots,
        "queue_position": queue_position,
        "upload_speed": upload_speed,
        "file_attributes": file_attributes or {},
        "received_at": 0.0,
    }


@pytest.fixture
def cfg() -> Config:
    return make_cfg()
