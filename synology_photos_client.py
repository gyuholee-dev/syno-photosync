"""시놀로지 포토 웹 API 클라이언트 (비공식 API, 브라우저 네트워크 캡처 기준).

NAS에서 root 권한으로 `synowebapi --exec ... runner=<사용자>`를 호출해 API를 실행한다.
로그인이 없으므로 비밀번호를 저장할 필요가 없고, runner로 지정한 사용자의 개인 공간이 대상이 된다.
(NAS 시험 결과: runner를 바꾸면 루트 폴더 ID와 owner_user_id가 사용자별로 달라진다.)

캡처로 확인한 요청 형식:
    - api, method, version + 나머지 파라미터
    - 나머지 파라미터 값은 전부 JSON으로 직렬화한다. (숫자 120, 배열 [120], 문자열 "capture_test")

확인하지 못한 부분 (NAS에서 첫 실행 때 확인 필요):
    - set_condition(조건 수정)은 폴더를 제거하는 요청 하나만 캡처했다. 추가도 같은 요청 형식일 것으로 가정했다.
    - 객체·배열 파라미터(condition 등)를 synowebapi가 그대로 받아들이는지.
    - Browse.Item list 요청에서 additional 파라미터를 생략해도 되는지.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SYNOWEBAPI_PATH = "/usr/syno/bin/synowebapi"
SYNOWEBAPI_TIMEOUT_SECONDS = 60
# synowebapi는 JSON 앞에 '[Line 295] Exec WebAPI: ... param={...}' 로그 줄을 붙여 출력한다.
# 그 줄에도 '{'가 있으므로, 줄 맨 앞에서 시작하는 '{'를 응답 JSON의 시작으로 본다.
SYNOWEBAPI_RESPONSE_START_PATTERN = re.compile(r"^\{", re.MULTILINE)

FOLDER_LIST_PAGE_SIZE = 1000
ALBUM_LIST_PAGE_SIZE = 100
MY_ALBUMS_CATEGORY = "normal_share_with_me"


class SynologyApiError(Exception):
    """API가 success=false를 돌려줬거나 응답 형식이 예상과 다를 때 발생한다."""


@dataclass(frozen=True)
class RootFolder:
    folder_id: int
    owner_user_id: int


def encode_json_parameter(parameter_value: Any) -> str:
    """캡처된 요청처럼 공백 없는 JSON 문자열로 직렬화한다."""
    return json.dumps(parameter_value, separators=(",", ":"), ensure_ascii=False)


def parse_synowebapi_output(raw_output: str) -> dict:
    """synowebapi 출력에서 응답 JSON만 꺼낸다. 앞의 로그 줄과 뒤의 군더더기는 무시한다."""
    response_start = SYNOWEBAPI_RESPONSE_START_PATTERN.search(raw_output)
    if response_start is None:
        raise SynologyApiError(
            f"synowebapi 출력에서 응답 JSON을 찾지 못했습니다: {raw_output.strip()[:200]}"
        )
    api_response, _ = json.JSONDecoder().raw_decode(raw_output[response_start.start():])
    return api_response


class SynowebapiTransport:
    """NAS의 synowebapi 명령으로 API를 실행한다. root 권한이 필요하다."""

    def __init__(
        self,
        runner_user_name: str,
        synowebapi_path: str = SYNOWEBAPI_PATH,
        timeout_seconds: int = SYNOWEBAPI_TIMEOUT_SECONDS,
    ):
        self.runner_user_name = runner_user_name
        self.synowebapi_path = synowebapi_path
        self.timeout_seconds = timeout_seconds

    def build_command(
        self, api_name: str, method_name: str, version: int, parameters: dict[str, Any]
    ) -> list[str]:
        """셸을 거치지 않는 인자 목록을 만든다. 값은 캡처된 요청처럼 JSON 문자열이다."""
        command = [
            self.synowebapi_path,
            "--exec",
            f"api={api_name}",
            f"method={method_name}",
            f"version={version}",
            f"runner={self.runner_user_name}",
        ]
        command.extend(
            f"{parameter_name}={encode_json_parameter(parameter_value)}"
            for parameter_name, parameter_value in parameters.items()
        )
        return command

    def call_api(
        self, api_name: str, method_name: str, version: int, parameters: dict[str, Any]
    ) -> dict:
        command = self.build_command(api_name, method_name, version, parameters)
        try:
            completed_process = subprocess.run(
                command,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except OSError as execution_error:
            raise SynologyApiError(
                f"synowebapi를 실행할 수 없습니다 (root 권한 필요): {execution_error}"
            ) from execution_error
        except subprocess.TimeoutExpired as timeout_error:
            raise SynologyApiError(f"{api_name}.{method_name} 시간 초과") from timeout_error

        api_response = parse_synowebapi_output(completed_process.stdout + completed_process.stderr)
        if not api_response.get("success"):
            raise SynologyApiError(f"{api_name}.{method_name} 실패: {api_response.get('error')}")
        return api_response.get("data", {})


class SynologyPhotosClient:
    """포토 API를 의미 있는 이름의 메서드로 감싼다. 실제 전송은 call_api 콜백이 맡는다."""

    def __init__(self, call_api: Callable[[str, str, int, dict[str, Any]], dict]):
        self._call_api = call_api

    def get_root_folder(self) -> RootFolder:
        """Photos 루트 폴더의 ID와 소유자(= 로그인 사용자) ID를 조회한다."""
        response_data = self._call_api("SYNO.Foto.Browse.Folder", "get", 2, {"name": "/"})
        root_folder = response_data["folder"]
        return RootFolder(folder_id=root_folder["id"], owner_user_id=root_folder["owner_user_id"])

    def list_child_folders(self, parent_folder_id: int) -> list[dict]:
        """부모 폴더의 직계 하위 폴더를 전부 조회한다. (1000개씩 페이지 순회)"""
        child_folders: list[dict] = []
        while True:
            response_data = self._call_api(
                "SYNO.Foto.Browse.Folder",
                "list",
                2,
                {"id": parent_folder_id, "limit": FOLDER_LIST_PAGE_SIZE, "offset": len(child_folders)},
            )
            page_folders = response_data["list"]
            child_folders.extend(page_folders)
            if len(page_folders) < FOLDER_LIST_PAGE_SIZE:
                return child_folders

    def count_items_in_folders(self, user_id: int, folder_ids: list[int]) -> int:
        """폴더 조건에 해당하는 사진 수(하위 폴더 포함)를 미리 센다."""
        response_data = self._call_api(
            "SYNO.Foto.Browse.ConditionAlbum",
            "peek_item_count",
            4,
            {"condition": self._build_folder_condition(user_id, folder_ids)},
        )
        return response_data["count"]

    def list_my_condition_albums(self, user_id: int) -> list[dict]:
        """내가 만든 조건부 앨범만 조회한다. 응답의 condition.folder_filter로 폴더 조건을 알 수 있다."""
        all_albums: list[dict] = []
        while True:
            response_data = self._call_api(
                "SYNO.Foto.Browse.Album",
                "list",
                4,
                {
                    "offset": len(all_albums),
                    "limit": ALBUM_LIST_PAGE_SIZE,
                    "category": MY_ALBUMS_CATEGORY,
                    "sort_by": "create_time",
                    "sort_direction": "desc",
                },
            )
            page_albums = response_data["list"]
            all_albums.extend(page_albums)
            if len(page_albums) < ALBUM_LIST_PAGE_SIZE:
                break
        return [
            album
            for album in all_albums
            if album.get("type") == "condition" and album.get("owner_user_id") == user_id
        ]

    def create_condition_album(self, album_name: str, user_id: int, folder_ids: list[int]) -> int:
        """폴더 조건의 조건부 앨범을 만들고 새 앨범 ID를 돌려준다."""
        response_data = self._call_api(
            "SYNO.Foto.Browse.ConditionAlbum",
            "create",
            4,
            {
                "name": album_name,
                "condition": self._build_folder_condition(user_id, folder_ids),
            },
        )
        return response_data["album"]["id"]

    def update_condition_album_folders(
        self,
        album_id: int,
        user_id: int,
        folder_ids: list[int],
        existing_condition: dict | None = None,
    ) -> None:
        """조건부 앨범의 폴더 조건을 folder_ids로 통째로 바꾼다. (추가와 제거 모두 이 한 번의 호출)

        기존 조건에 다른 필드가 있으면 그대로 두고 folder_filter만 교체한다.
        """
        updated_condition = dict(existing_condition) if existing_condition else {}
        updated_condition.setdefault("user_id", user_id)
        updated_condition.setdefault("item_type", [])
        updated_condition["folder_filter"] = list(folder_ids)
        self._call_api(
            "SYNO.Foto.Browse.ConditionAlbum",
            "set_condition",
            4,
            {"id": album_id, "condition": updated_condition},
        )

    def get_latest_item_id_in_album(self, album_id: int) -> int | None:
        """앨범에서 촬영 시각이 가장 늦은 사진의 항목 ID. 사진이 없으면 None."""
        response_data = self._call_api(
            "SYNO.Foto.Browse.Item",
            "list",
            7,
            {
                "offset": 0,
                "limit": 1,
                "sort_by": "takentime",
                "sort_direction": "desc",
                "album_id": album_id,
            },
        )
        items = response_data["list"]
        return items[0]["id"] if items else None

    def get_album_cover_item_id(self, album_id: int) -> int | None:
        """앨범의 현재 커버 사진 항목 ID(thumbnail.unit_id). 알 수 없으면 None."""
        response_data = self._call_api(
            "SYNO.Foto.Browse.Album",
            "get",
            4,
            {"id": [album_id], "additional": ["thumbnail"]},
        )
        album_entries = response_data["list"]
        if not album_entries:
            return None
        return album_entries[0].get("additional", {}).get("thumbnail", {}).get("unit_id")

    def set_album_cover(self, album_id: int, item_id: int) -> None:
        self._call_api(
            "SYNO.Foto.Browse.Album",
            "set_cover",
            1,
            {"id": album_id, "id_item": item_id},
        )

    @staticmethod
    def _build_folder_condition(user_id: int, folder_ids: list[int]) -> dict:
        return {"user_id": user_id, "item_type": [], "folder_filter": list(folder_ids)}
