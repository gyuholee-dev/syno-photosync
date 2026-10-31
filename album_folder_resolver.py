"""사진이 들어 있는 NAS 폴더 경로로부터 '앨범 폴더'와 '앨범명'을 판별하는 순수 로직.

시놀로지 포토 API 호출은 이 모듈에 없다. 폴더 경로(문자열 튜플)만 입력으로 받으므로
NAS 없이도 단위 테스트로 검증할 수 있다.

경로 표현:
    개인 공간 Photos 루트 기준 상대 경로를 폴더 이름의 튜플로 표현한다.
    예) /DCIM/Test/2026/10  ->  ("DCIM", "Test", "2026", "10")
        Photos 루트 자체     ->  ()
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Tuple

# NAS의 Python 3.8에서는 모듈 수준 별칭에 tuple[...]을 쓸 수 없어 typing.Tuple을 쓴다.
FolderPath = Tuple[str, ...]

YEAR_FOLDER_PATTERN = re.compile(r"^\d{4}$")
MONTH_FOLDER_PATTERN = re.compile(r"^(0[1-9]|1[0-2])$")
SYSTEM_FOLDER_NAMES = frozenset({"@eaDir"})

# 포토 앱의 기본 타임라인 백업 위치. 앨범으로 만들면 안 되는 '업로드 루트'.
DEFAULT_UPLOAD_ROOT_FOLDER_PATHS: frozenset[FolderPath] = frozenset({(), ("PhotoLibrary",)})


def is_year_month_folder_path(folder_path: FolderPath) -> bool:
    """경로 끝이 '<4자리 연도>/<01~12 월>' 쌍이면 True."""
    if len(folder_path) < 2:
        return False
    year_folder_name, month_folder_name = folder_path[-2], folder_path[-1]
    return bool(
        YEAR_FOLDER_PATTERN.match(year_folder_name)
        and MONTH_FOLDER_PATTERN.match(month_folder_name)
    )


def is_system_folder_path(folder_path: FolderPath) -> bool:
    """경로 중 하나라도 @eaDir 같은 시스템 폴더면 True."""
    return any(folder_name in SYSTEM_FOLDER_NAMES for folder_name in folder_path)


def should_descend_into_folder(folder_path: FolderPath) -> bool:
    """폴더 탐색 시 이 폴더 안으로 더 내려갈지 결정한다.

    연/월 폴더(YYYY/MM)는 사진이 직접 들어 있는 끝 폴더이므로 더 내려가지 않는다.
    시스템 폴더도 내려가지 않는다.
    """
    if is_system_folder_path(folder_path):
        return False
    return not is_year_month_folder_path(folder_path)


def resolve_album_folder_path(photo_folder_path: FolderPath) -> FolderPath:
    """사진이 직접 들어 있는 폴더 경로에서 연/월 구간을 제거해 앨범 폴더 경로를 구한다."""
    if is_year_month_folder_path(photo_folder_path):
        return photo_folder_path[:-2]
    return photo_folder_path


def group_photo_folders_into_albums(
    photo_folder_paths: list[FolderPath],
    upload_root_folder_paths: frozenset[FolderPath] = DEFAULT_UPLOAD_ROOT_FOLDER_PATHS,
) -> dict[str, list[FolderPath]]:
    """사진이 직접 들어 있는 폴더들을 앨범명 기준으로 묶는다.

    - 연/월 구간을 제거한 경로가 앨범 폴더가 되고, 그 마지막 폴더 이름이 앨범명이다.
    - 앨범 폴더가 업로드 루트와 같으면(타임라인 백업) 앨범에서 제외한다.
    - 서로 다른 경로의 같은 이름 앨범 폴더는 하나의 앨범으로 합친다.
    - 같은 앨범 폴더가 여러 번 나와도(직접 사진 + 연/월 폴더) 한 번만 담는다.

    반환값: {앨범명: [앨범 폴더 경로, ...]} (경로는 정렬되어 있다)
    """
    album_folder_paths_by_album_name: dict[str, set[FolderPath]] = defaultdict(set)

    for photo_folder_path in photo_folder_paths:
        if is_system_folder_path(photo_folder_path):
            continue
        album_folder_path = resolve_album_folder_path(photo_folder_path)
        if album_folder_path in upload_root_folder_paths:
            continue
        album_name = album_folder_path[-1]
        album_folder_paths_by_album_name[album_name].add(album_folder_path)

    return {
        album_name: sorted(album_folder_paths)
        for album_name, album_folder_paths in sorted(album_folder_paths_by_album_name.items())
    }
