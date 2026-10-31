"""폴더 트리를 순회해서 앨범명별 '앨범 폴더 ID 목록'을 만든다.

포토 API 호출은 이 모듈에 없다. 하위 폴더 조회는 `list_child_folders` 콜백으로 주입받으므로
NAS 없이도 가짜 폴더 트리로 테스트할 수 있다.

콜백이 돌려주는 폴더 한 건의 형태는 캡처로 확인한 Browse.Folder list 응답을 따른다.
    {"id": 121, "name": "/PhotoLibrary/DCIM/Test/2026", "parent": 120, ...}
`name`은 폴더 이름 한 조각이 아니라 Photos 루트 기준 전체 경로다.

앨범 폴더를 찾는 방법:
    1. 트리를 끝까지 순회하며 '자식 폴더가 없는 폴더(말단 폴더)'를 모은다.
       (연/월 폴더 YYYY/MM은 말단이라고 보고 그 안으로는 내려가지 않는다.)
    2. 말단 폴더 경로에서 연/월 구간을 제거하면 앨범 폴더가 된다. (album_folder_resolver)
    3. 앨범 폴더의 ID는 순회 중 기록해 둔 경로-ID 대응표에서 찾는다.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from album_folder_resolver import (
    DEFAULT_UPLOAD_ROOT_FOLDER_PATHS,
    FolderPath,
    group_photo_folders_into_albums,
    is_system_folder_path,
    should_descend_into_folder,
)

ListChildFolders = Callable[[int], List[Dict]]


def parse_folder_path(folder_name: str) -> FolderPath:
    """'/PhotoLibrary/DCIM/Test' -> ('PhotoLibrary', 'DCIM', 'Test')"""
    return tuple(folder_name_part for folder_name_part in folder_name.split("/") if folder_name_part)


def collect_folder_tree(
    root_folder_id: int,
    list_child_folders: ListChildFolders,
) -> tuple[dict[FolderPath, int], list[FolderPath]]:
    """루트 폴더부터 트리를 순회한다.

    반환값:
        folder_id_by_path: 방문한 모든 폴더의 {경로: 폴더 ID}
        leaf_folder_paths: 자식 폴더가 없는(또는 더 내려가지 않기로 한) 말단 폴더 경로 목록
    """
    root_folder_path: FolderPath = ()
    folder_id_by_path: dict[FolderPath, int] = {root_folder_path: root_folder_id}
    leaf_folder_paths: list[FolderPath] = []
    folders_to_visit: list[tuple[FolderPath, int]] = [(root_folder_path, root_folder_id)]

    while folders_to_visit:
        current_folder_path, current_folder_id = folders_to_visit.pop()

        child_folder_entries: list[dict] = []
        if should_descend_into_folder(current_folder_path):
            child_folder_entries = list_child_folders(current_folder_id)

        visible_child_folders: list[tuple[FolderPath, int]] = []
        for child_folder_entry in child_folder_entries:
            child_folder_path = parse_folder_path(child_folder_entry["name"])
            if is_system_folder_path(child_folder_path):
                continue
            visible_child_folders.append((child_folder_path, child_folder_entry["id"]))

        if not visible_child_folders:
            leaf_folder_paths.append(current_folder_path)
            continue

        for child_folder_path, child_folder_id in visible_child_folders:
            folder_id_by_path[child_folder_path] = child_folder_id
            folders_to_visit.append((child_folder_path, child_folder_id))

    return folder_id_by_path, leaf_folder_paths


def build_album_folder_ids_by_album_name(
    folder_id_by_path: dict[FolderPath, int],
    leaf_folder_paths: list[FolderPath],
    upload_root_folder_paths: frozenset[FolderPath] = DEFAULT_UPLOAD_ROOT_FOLDER_PATHS,
) -> dict[str, list[int]]:
    """앨범명별로 조건부 앨범의 folder_filter에 넣을 폴더 ID 목록을 만든다."""
    album_folder_paths_by_album_name = group_photo_folders_into_albums(
        leaf_folder_paths, upload_root_folder_paths
    )
    return {
        album_name: [folder_id_by_path[album_folder_path] for album_folder_path in album_folder_paths]
        for album_name, album_folder_paths in album_folder_paths_by_album_name.items()
    }
