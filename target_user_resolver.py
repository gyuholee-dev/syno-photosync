"""앨범을 만들 대상 DSM 사용자 목록을 photosync.env 파일에서 읽어 정한다.

photosync.env 형식 (쉼표로 구분, 둘 중 하나만 써도 된다):
    PHOTOS_USERS=user
    PHOTOS_GROUPS=family

그룹은 구성원 계정으로 풀어서 개별 계정과 합친다. 중복은 한 번만 남기고 적은 순서를 유지한다.
그룹 구성원 조회는 `lookup_group_members` 콜백으로 주입받으므로 NAS 없이 테스트할 수 있다.
"""

from __future__ import annotations

import pwd
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List

SYNOGROUP_PATH = "/usr/syno/sbin/synogroup"
SYNOGROUP_TIMEOUT_SECONDS = 30

USERS_VARIABLE_NAME = "PHOTOS_USERS"
GROUPS_VARIABLE_NAME = "PHOTOS_GROUPS"

GROUP_MEMBERS_HEADER_PATTERN = re.compile(r"^\s*Group Members\b", re.IGNORECASE)
# NAS 실제 출력: '0:[alice]' (계정명이 대괄호로 감싸여 있다)
GROUP_MEMBER_LINE_PATTERN = re.compile(r"^\s*\d+:\s*\[(.+?)\]\s*$")

LookupGroupMembers = Callable[[str], List[str]]

OPTION_LIKE_NAME_PREFIX = "-"


class TargetUserConfigError(Exception):
    """대상 사용자를 정할 수 없을 때(설정 파일 오류, 그룹 조회 실패, 대상 없음) 발생한다."""


@dataclass(frozen=True)
class TargetUsers:
    """처리할 사용자 목록과, 목록을 만드는 중 생긴 문제·경고 메시지.

    problem_messages: 그룹 조회 실패, 잘못된 이름. 종료 코드를 1로 만든다.
    warning_messages: 구성원이 없는 그룹처럼 실패는 아니지만 알려 둘 일. 종료 코드에 영향이 없다.
    """

    user_names: List[str]
    problem_messages: List[str]
    warning_messages: List[str] = field(default_factory=list)


def parse_env_file_text(env_file_text: str) -> Dict[str, str]:
    """KEY=VALUE 줄을 읽는다. 빈 줄, # 주석, 'export ' 접두사, 값을 감싼 따옴표를 처리한다."""
    variables: Dict[str, str] = {}
    for raw_line in env_file_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        variable_name, variable_value = line.split("=", 1)
        variable_value = variable_value.strip()
        if variable_value[:1] in ("'", '"') and variable_value[-1:] == variable_value[:1]:
            variable_value = variable_value[1:-1]
        else:
            variable_value = variable_value.split(" #", 1)[0].strip()
        variables[variable_name.strip()] = variable_value
    return variables


def split_names(comma_separated_names: str) -> List[str]:
    """'a, b,,c' -> ['a', 'b', 'c']"""
    return [name.strip() for name in comma_separated_names.split(",") if name.strip()]


def parse_group_members(synogroup_output: str) -> List[str]:
    """`synogroup --get <그룹>` 출력에서 구성원 계정명을 꺼낸다.

    'Group Members:' 줄 다음부터 '0:[alice]', '1:[bob]' 형태의 줄이 이어진다. (NAS 출력으로 확인함)
    """
    member_names: List[str] = []
    in_members_section = False
    for output_line in synogroup_output.splitlines():
        if GROUP_MEMBERS_HEADER_PATTERN.match(output_line):
            in_members_section = True
            continue
        if not in_members_section:
            continue
        member_match = GROUP_MEMBER_LINE_PATTERN.match(output_line)
        if member_match:
            member_names.append(member_match.group(1))
    if not in_members_section:
        raise TargetUserConfigError(
            f"synogroup 출력에서 'Group Members'를 찾지 못했습니다: {synogroup_output.strip()[:200]}"
        )
    return member_names


def describe_option_like_name(kind: str, name: str) -> str:
    return f"{kind} '{name}'은(는) '{OPTION_LIKE_NAME_PREFIX}'로 시작해서 건너뜁니다 (명령 옵션으로 해석될 수 있습니다)"


def lookup_group_members_with_synogroup(group_name: str) -> List[str]:
    """NAS의 synogroup 명령으로 그룹 구성원을 조회한다. 그룹이 없거나 조회에 실패하면 예외를 낸다."""
    if group_name.startswith(OPTION_LIKE_NAME_PREFIX):
        raise TargetUserConfigError(describe_option_like_name("그룹", group_name))
    try:
        completed_process = subprocess.run(
            [SYNOGROUP_PATH, "--get", group_name],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=SYNOGROUP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as execution_error:
        raise TargetUserConfigError(
            f"그룹 '{group_name}'을 조회할 수 없습니다: {execution_error}"
        ) from execution_error
    if completed_process.returncode != 0:
        raise TargetUserConfigError(
            f"그룹 '{group_name}'을 찾을 수 없거나 조회에 실패했습니다: "
            f"{(completed_process.stdout + completed_process.stderr).strip()[:200]}"
        )
    try:
        return parse_group_members(completed_process.stdout)
    except TargetUserConfigError as parse_error:
        raise TargetUserConfigError(
            f"그룹 '{group_name}'의 구성원을 읽지 못했습니다 (그룹이 없을 수 있습니다): {parse_error}"
        ) from parse_error


def does_dsm_user_exist(user_name: str) -> bool:
    """NAS에 그 계정이 있는지 확인한다. (DSM 로컬 계정은 /etc/passwd 로 조회된다)"""
    try:
        pwd.getpwnam(user_name)
    except KeyError:
        return False
    return True


def resolve_target_user_names(
    user_names: List[str],
    group_names: List[str],
    lookup_group_members: LookupGroupMembers,
) -> TargetUsers:
    """개별 계정과 그룹 구성원을 합쳐 중복 없는 계정 목록을 만든다.

    그룹 하나를 조회하지 못하거나 '-'로 시작하는 이름이 있어도 중단하지 않는다. 그 항목은 건너뛰고
    메시지만 모아서 돌려주므로 나머지 사용자는 계속 처리할 수 있다.
    """
    resolved_user_names: List[str] = []
    problem_messages: List[str] = []
    warning_messages: List[str] = []

    candidate_user_names = list(user_names)
    for group_name in group_names:
        try:
            group_member_names = lookup_group_members(group_name)
        except TargetUserConfigError as group_error:
            problem_messages.append(str(group_error))
            continue
        if not group_member_names:
            warning_messages.append(f"그룹 '{group_name}'에 구성원이 없습니다")
        candidate_user_names.extend(group_member_names)

    for candidate_user_name in candidate_user_names:
        if candidate_user_name.startswith(OPTION_LIKE_NAME_PREFIX):
            problem_messages.append(describe_option_like_name("계정", candidate_user_name))
        elif candidate_user_name not in resolved_user_names:
            resolved_user_names.append(candidate_user_name)
    return TargetUsers(resolved_user_names, problem_messages, warning_messages)


def load_target_user_names(
    env_file_path: str,
    lookup_group_members: LookupGroupMembers = lookup_group_members_with_synogroup,
) -> TargetUsers:
    """설정 파일(photosync.env)을 읽어 처리할 DSM 사용자 목록을 돌려준다.

    파일을 읽을 수 없거나 계정과 그룹이 하나도 적혀 있지 않으면 예외를 낸다.
    """
    try:
        with open(env_file_path, encoding="utf-8") as env_file:
            variables = parse_env_file_text(env_file.read())
    except OSError as read_error:
        raise TargetUserConfigError(
            f"설정 파일을 읽을 수 없습니다: {env_file_path}: {read_error}"
        ) from read_error

    return build_target_users(
        split_names(variables.get(USERS_VARIABLE_NAME, "")),
        split_names(variables.get(GROUPS_VARIABLE_NAME, "")),
        lookup_group_members,
        f"{env_file_path}에",
    )


def build_target_users(
    configured_user_names: List[str],
    configured_group_names: List[str],
    lookup_group_members: LookupGroupMembers = lookup_group_members_with_synogroup,
    source_description: str = "인자에",
) -> TargetUsers:
    """계정 이름과 그룹 이름 목록(설정 파일이나 인자에서 온 값)으로 처리할 사용자 목록을 만든다.

    둘 다 비어 있으면 예외를 낸다. source_description 은 그 오류 메시지에 쓸 출처 설명이다.
    """
    if not configured_user_names and not configured_group_names:
        raise TargetUserConfigError(
            f"{source_description} 대상이 없습니다. {USERS_VARIABLE_NAME} 또는 {GROUPS_VARIABLE_NAME}를 적어 주세요"
        )
    return resolve_target_user_names(configured_user_names, configured_group_names, lookup_group_members)
