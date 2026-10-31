"""NAS의 앨범 폴더마다 조건부 앨범을 만들고, 앨범 커버를 가장 늦게 촬영된 사진으로 맞춘다.

인자 없이 실행하면 실제로 앨범을 만들고 커버를 바꾼다.
--dry-run 을 붙이면 읽기 요청만 보내고, 무엇을 만들고 바꿀지 로그로만 보여 준다.

NAS에서 root 권한(DSM 작업 스케줄러의 사용자를 root로 지정)으로 실행한다.
대상 DSM 사용자는 스크립트와 같은 폴더의 photosync.env 에서 읽는다.
    PHOTOS_USERS=user      개별 계정 (쉼표로 구분)
    PHOTOS_GROUPS=group    그룹 (쉼표로 구분, 구성원 계정으로 풀어서 합친다)
같은 이름의 인자(--photos_users, --photos_groups)를 하나라도 주면
photosync.env 는 읽지 않고 인자로 준 값만 대상으로 한다. (시험용)
로그인이나 비밀번호는 쓰지 않고, 사용자마다 그 사람의 개인 공간을 한 번씩 순서대로 처리한다.

없는 계정, '-'로 시작하는 이름, 조회에 실패한 그룹은 건너뛰고 나머지를 계속 처리하며, 그런 문제가 하나라도 있으면 종료 코드 1이다.

같은 폴더의 photosync.lock 으로 동시 실행을 막는다. 이전 실행이 끝나지 않았으면 아무것도 하지 않고 종료 코드 0으로 끝낸다.
실행이 끝나면 사용자별, 전체 처리 건수를 요약 한 줄로 남긴다.

종료 코드: 0 정상(이미 실행 중이라 건너뛴 경우 포함), 1 일부 계정·그룹에 문제가 있었던 경우, 2 처리할 사용자를 정할 수 없는 경우
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
from dataclasses import dataclass

from folder_tree_collector import build_album_folder_ids_by_album_name, collect_folder_tree
from synology_photos_client import SynologyApiError, SynologyPhotosClient, SynowebapiTransport
from target_user_resolver import (
    TargetUserConfigError,
    build_target_users,
    does_dsm_user_exist,
    load_target_user_names,
    split_names,
)

DEFAULT_ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photosync.env")

LOCK_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photosync.lock")

ACTION_KEEP = "keep"
ACTION_CREATE = "create"
ACTION_FOLDER_SET_CHANGED = "folder_set_changed"
ACTION_NAME_CONFLICT = "name_conflict"


@dataclass(frozen=True)
class PlannedAlbumAction:
    action_kind: str
    album_name: str
    folder_ids: tuple[int, ...]
    existing_album_id: int | None = None
    existing_condition: dict | None = None


@dataclass
class SyncSummary:
    """한 사용자(또는 전체)에서 처리한 건수. 드라이런이면 '할 예정'인 건수다."""
    created_album_count: int = 0
    folder_updated_album_count: int = 0
    unchanged_album_count: int = 0
    cover_changed_album_count: int = 0
    skipped_empty_album_count: int = 0
    name_conflict_album_count: int = 0

    def add(self, other: SyncSummary) -> None:
        self.created_album_count += other.created_album_count
        self.folder_updated_album_count += other.folder_updated_album_count
        self.unchanged_album_count += other.unchanged_album_count
        self.cover_changed_album_count += other.cover_changed_album_count
        self.skipped_empty_album_count += other.skipped_empty_album_count
        self.name_conflict_album_count += other.name_conflict_album_count

    def describe(self) -> str:
        return (
            "생성 %d / 폴더 조건 변경 %d / 유지 %d / 커버 변경 %d / 사진 없어 건너뜀 %d / 이름 충돌 %d"
            % (
                self.created_album_count,
                self.folder_updated_album_count,
                self.unchanged_album_count,
                self.cover_changed_album_count,
                self.skipped_empty_album_count,
                self.name_conflict_album_count,
            )
        )


def acquire_run_lock(lock_file_path: str):
    """다른 실행이 없으면 잠금을 잡고 파일 객체를, 이미 실행 중이면 None 을 돌려 준다.

    잠금은 프로세스가 끝나면(비정상 종료 포함) 운영체제가 풀어 주므로 찌꺼기 파일이 남아도 문제없다.
    돌려받은 파일 객체를 닫지 말고 끝까지 들고 있어야 잠금이 유지된다.
    """
    lock_file = open(lock_file_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def plan_album_actions(
    album_folder_ids_by_album_name: dict[str, list[int]],
    existing_condition_albums: list[dict],
) -> list[PlannedAlbumAction]:
    """원하는 앨범(앨범명 -> 폴더 ID들)과 이미 있는 조건부 앨범을 비교해 할 일을 정한다.

    - 폴더 ID 집합이 같은 앨범이 이미 있으면 keep (앨범 이름이 바뀌었어도 같은 앨범으로 본다)
    - 같은 이름인데 폴더 ID 집합이 다르면 folder_set_changed (폴더 조건을 새 집합으로 바꾼다)
    - 같은 이름이지만 폴더 조건이 없는 앨범(직접 만든 앨범)이면 name_conflict (건드리지 않고 알리기만 한다)
    - 둘 다 아니면 create
    """
    existing_album_by_folder_id_set = {}
    existing_album_by_album_name = {}
    for existing_album in existing_condition_albums:
        existing_folder_ids = frozenset(existing_album.get("condition", {}).get("folder_filter", []))
        if existing_folder_ids:
            existing_album_by_folder_id_set[existing_folder_ids] = existing_album
        existing_album_by_album_name[existing_album["name"]] = existing_album

    planned_actions: list[PlannedAlbumAction] = []
    for album_name, folder_ids in sorted(album_folder_ids_by_album_name.items()):
        wanted_folder_ids = tuple(sorted(folder_ids))
        same_folders_album = existing_album_by_folder_id_set.get(frozenset(wanted_folder_ids))
        same_name_album = existing_album_by_album_name.get(album_name)
        if same_folders_album is not None:
            planned_actions.append(
                PlannedAlbumAction(ACTION_KEEP, album_name, wanted_folder_ids, same_folders_album["id"])
            )
        elif same_name_album is not None:
            same_name_has_folder_filter = bool(
                same_name_album.get("condition", {}).get("folder_filter")
            )
            planned_actions.append(
                PlannedAlbumAction(
                    ACTION_FOLDER_SET_CHANGED if same_name_has_folder_filter else ACTION_NAME_CONFLICT,
                    album_name,
                    wanted_folder_ids,
                    same_name_album["id"],
                    same_name_album.get("condition"),
                )
            )
        else:
            planned_actions.append(PlannedAlbumAction(ACTION_CREATE, album_name, wanted_folder_ids))
    return planned_actions


def refresh_album_cover(
    client: SynologyPhotosClient, album_id: int, album_name: str, apply_changes: bool,
    logger: logging.Logger,
) -> bool:
    """앨범 커버가 가장 늦게 촬영된 사진이 아니면 바꾼다. 바꿨거나 바꿀 예정이면 True."""
    latest_item_id = client.get_latest_item_id_in_album(album_id)
    if latest_item_id is None:
        logger.info("[%s] 사진이 없어 커버를 건너뜁니다", album_name)
        return False
    current_cover_item_id = client.get_album_cover_item_id(album_id)
    if current_cover_item_id == latest_item_id:
        logger.info("[%s] 커버가 이미 최신 사진입니다 (항목 %s)", album_name, latest_item_id)
        return False
    if not apply_changes:
        logger.info("[%s] (드라이런) 커버를 항목 %s로 바꿀 예정", album_name, latest_item_id)
        return True
    client.set_album_cover(album_id, latest_item_id)
    logger.info("[%s] 커버를 항목 %s로 변경했습니다", album_name, latest_item_id)
    return True


def run_sync(
    client: SynologyPhotosClient, apply_changes: bool, logger: logging.Logger
) -> SyncSummary:
    summary = SyncSummary()
    root_folder = client.get_root_folder()
    folder_id_by_path, leaf_folder_paths = collect_folder_tree(
        root_folder.folder_id, client.list_child_folders
    )
    album_folder_ids_by_album_name = build_album_folder_ids_by_album_name(
        folder_id_by_path, leaf_folder_paths
    )
    logger.info("앨범 폴더 %d개를 찾았습니다", len(album_folder_ids_by_album_name))

    existing_condition_albums = client.list_my_condition_albums(root_folder.owner_user_id)
    planned_actions = plan_album_actions(album_folder_ids_by_album_name, existing_condition_albums)

    for planned_action in planned_actions:
        album_name = planned_action.album_name
        album_id = planned_action.existing_album_id

        if planned_action.action_kind == ACTION_NAME_CONFLICT:
            logger.warning(
                "[%s] 같은 이름의 앨범이 있지만 폴더 조건이 없는 앨범이라 건드리지 않습니다",
                album_name,
            )
            summary.name_conflict_album_count += 1
            continue

        if planned_action.action_kind == ACTION_FOLDER_SET_CHANGED:
            current_folder_ids = sorted(planned_action.existing_condition.get("folder_filter", []))
            wanted_folder_ids = list(planned_action.folder_ids)
            if not apply_changes:
                logger.info(
                    "[%s] (드라이런) 폴더 조건을 %s에서 %s로 바꿀 예정",
                    album_name, current_folder_ids, wanted_folder_ids,
                )
            else:
                client.update_condition_album_folders(
                    album_id, root_folder.owner_user_id, wanted_folder_ids,
                    planned_action.existing_condition,
                )
                logger.info(
                    "[%s] 폴더 조건을 %s에서 %s로 바꿨습니다 (앨범 %s)",
                    album_name, current_folder_ids, wanted_folder_ids, album_id,
                )
            summary.folder_updated_album_count += 1

        if planned_action.action_kind == ACTION_CREATE:
            item_count = client.count_items_in_folders(
                root_folder.owner_user_id, list(planned_action.folder_ids)
            )
            if item_count == 0:
                logger.info("[%s] 사진이 없어 앨범을 만들지 않습니다", album_name)
                summary.skipped_empty_album_count += 1
                continue
            if not apply_changes:
                logger.info(
                    "[%s] (드라이런) 폴더 %s로 조건부 앨범을 만들 예정 (사진 %d장)",
                    album_name, list(planned_action.folder_ids), item_count,
                )
                summary.created_album_count += 1
                continue
            album_id = client.create_condition_album(
                album_name, root_folder.owner_user_id, list(planned_action.folder_ids)
            )
            logger.info("[%s] 조건부 앨범을 만들었습니다 (앨범 %s)", album_name, album_id)
            summary.created_album_count += 1

        if planned_action.action_kind == ACTION_KEEP:
            summary.unchanged_album_count += 1

        if album_id is not None:
            if refresh_album_cover(client, album_id, album_name, apply_changes, logger):
                summary.cover_changed_album_count += 1
    return summary


def main() -> int:
    lock_file = acquire_run_lock(LOCK_FILE_PATH)
    if lock_file is None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.getLogger("sync_album_folders").warning(
            "이전 실행이 아직 끝나지 않아 이번 실행은 건너뜁니다 (%s)", LOCK_FILE_PATH
        )
        return 0
    return run_with_arguments()


def run_with_arguments() -> int:
    argument_parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argument_parser.add_argument(
        "--photos_users", metavar="USERS",
        help="앨범을 만들 DSM 계정 (쉼표로 구분). 주면 photosync.env 를 읽지 않고 인자로 준 값만 쓴다",
    )
    argument_parser.add_argument(
        "--photos_groups", metavar="GROUPS",
        help="앨범을 만들 DSM 그룹 (쉼표로 구분). 주면 photosync.env 를 읽지 않고 인자로 준 값만 쓴다",
    )
    argument_parser.add_argument(
        "--dry-run", "--dry_run", action="store_true", dest="dry_run",
        help="변경하지 않고 만들 앨범과 바꿀 커버를 로그로만 보여 준다",
    )
    parsed_arguments = argument_parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("sync_album_folders")

    apply_changes = not parsed_arguments.dry_run
    logger.info("실행 모드: %s", "적용" if apply_changes else "드라이런 (변경하지 않음)")

    try:
        if (
            parsed_arguments.photos_users is not None
            or parsed_arguments.photos_groups is not None
        ):
            target_users = build_target_users(
                split_names(parsed_arguments.photos_users or ""),
                split_names(parsed_arguments.photos_groups or ""),
            )
        else:
            target_users = load_target_user_names(DEFAULT_ENV_FILE_PATH)
    except TargetUserConfigError as config_error:
        logger.error("%s", config_error)
        return 2

    for problem_message in target_users.problem_messages:
        logger.error("%s", problem_message)
    for warning_message in target_users.warning_messages:
        logger.warning("%s", warning_message)
    if not target_users.user_names:
        logger.error("처리할 사용자가 없습니다 (그룹이 비어 있거나 조회에 실패했습니다)")
        return 2

    failed_user_names: list[str] = []
    total_summary = SyncSummary()
    for user_name in target_users.user_names:
        logger.info("===== 사용자 %s =====", user_name)
        if not does_dsm_user_exist(user_name):
            logger.error("[%s] NAS에 없는 계정이라 건너뜁니다", user_name)
            failed_user_names.append(user_name)
            continue
        client = SynologyPhotosClient(SynowebapiTransport(user_name).call_api)
        try:
            user_summary = run_sync(client, apply_changes, logger)
            logger.info("[%s] 요약: %s", user_name, user_summary.describe())
            total_summary.add(user_summary)
        except SynologyApiError as api_error:
            logger.error("[%s] API 오류: %s", user_name, api_error)
            failed_user_names.append(user_name)

    logger.info(
        "전체 요약 (%s, 사용자 %d명 중 실패 %d명): %s",
        "적용" if apply_changes else "드라이런",
        len(target_users.user_names), len(failed_user_names), total_summary.describe(),
    )
    if failed_user_names or target_users.problem_messages:
        logger.error(
            "문제가 있었습니다. 실패한 사용자: %s, 건너뛴 설정 항목 %d개",
            ", ".join(failed_user_names) or "없음",
            len(target_users.problem_messages),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
