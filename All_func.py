from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Set, Tuple

import holidays as pyholidays
import pandas as pd


# 모니터링표 생성에 필요한 설정값
@dataclass
class DutyConfig:
    year: int
    month: int

    # 전체 모니터링 담당자 리스트
    # 이제 평일/주말/공휴일 구분 없이 이 리스트 하나로만 로테이션
    weekday_members: List[str]

    # 기존 config 호환용
    # Main.py에서 holiday_members를 넘겨도 에러 안 나게 남겨둠
    holiday_members: Optional[List[str]] = None

    # 개인별 불가능 날짜
    personal_unavailable: Dict[str, Set[date]] = None

    # 전월 마지막 담당자
    # 이제 이 값 하나만 실질적으로 사용
    prev_weekday_last_member: Optional[str] = None

    # 기존 config 호환용
    prev_holiday_last_member: Optional[str] = None

    # 같은 사람이 연속으로 모니터링 서는 것 방지
    prevent_consecutive_same_person: bool = True

    # 최소 휴식일
    # 예: 2면 월요일 근무 후 화/수는 가능하면 제외
    min_rest_days_between_duties: int = 2


class DutyScheduler:
    def __init__(self, config: DutyConfig):
        self.config = config

        if self.config.personal_unavailable is None:
            self.config.personal_unavailable = {}

        # 단일 로테이션 인원
        self.members = list(dict.fromkeys(config.weekday_members))

        # Make_excel.py에서 scheduler.all_members를 사용하므로 유지
        self.all_members = self.members

        # 한국 공휴일 자동 계산
        self.holidays = self._build_kr_holidays(config.year)

        # 내부 상태값
        self.blocked_dates: Dict[str, Set[date]] = {}
        self.rotation_idx = 0
        self.duty_counts = {}
        self.rows = []
        self.last_assignee = None

        self._reset_state()

    # 스케줄 생성 전 상태 초기화
    def _reset_state(self) -> None:
        # 개인별 불가능 날짜
        # 이후 대체휴무일도 추가되어 해당 날짜 당직 제외 처리
        self.blocked_dates = {
            member: set(self.config.personal_unavailable.get(member, set()))
            for member in self.all_members
        }

        # 전월 마지막 담당자 다음 사람부터 시작
        self.rotation_idx = self._get_start_index(
            self.members,
            self.config.prev_weekday_last_member,
        )

        # 담당자별 횟수 관리
        self.duty_counts = {
            member: {
                "전체": 0,
                "평일": 0,
                "주말": 0,
                "공휴일": 0,
            }
            for member in self.all_members
        }

        self.rows = []
        self.last_assignee = None

        # 사람별 마지막 근무일
        self.last_duty_date_by_member = {
            member: None
            for member in self.all_members
        }

        # 대체휴무 관리
        self.pending_comp_targets = []
        self.used_comp_off_dates = set()

    # 한국 공휴일 생성
    def _build_kr_holidays(self, year: int) -> Set[date]:
        kr_holidays = pyholidays.country_holidays("KR", years=[year])
        return {d for d in kr_holidays.keys()}

    # 전월 마지막 담당자 기준 시작 인덱스 계산
    def _get_start_index(
        self,
        members: List[str],
        prev_last_member: Optional[str],
    ) -> int:
        if not members:
            raise ValueError("로테이션 인원이 비어 있습니다.")

        if prev_last_member and prev_last_member in members:
            return (members.index(prev_last_member) + 1) % len(members)

        return 0

    # 주말 여부
    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    # 공휴일 여부
    def is_holiday(self, d: date) -> bool:
        return d in self.holidays

    # 날짜 구분값 반환
    def get_day_type(self, d: date) -> str:
        if d.weekday() == 5:
            return "토요일"
        if d.weekday() == 6:
            return "일요일"
        if self.is_holiday(d):
            return "공휴일"
        return "평일"

    # 통계용 구분 키 반환
    def get_count_type(self, d: date) -> str:
        if self.is_holiday(d):
            return "공휴일"
        if self.is_weekend(d):
            return "주말"
        return "평일"

    # 입력 날짜 다음 영업일 반환
    def next_business_day(self, d: date) -> date:
        current = d + timedelta(days=1)

        while self.is_weekend(current) or self.is_holiday(current):
            current += timedelta(days=1)

        return current

    # 입력 날짜가 주말/공휴일이면 다음 영업일로 이동
    def adjust_to_business_day(self, d: date) -> date:
        current = d

        while self.is_weekend(current) or self.is_holiday(current):
            current += timedelta(days=1)

        return current

    # 대체휴무 실제 배정
    def allocate_due_comp_offs(self, up_to_date: date) -> None:
        due_targets = [
            item for item in self.pending_comp_targets
            if not item["assigned"] and item["base_comp_date"] <= up_to_date
        ]

        # 같은 후보일이면 토요일 > 일요일 > 공휴일 우선
        due_targets.sort(
            key=lambda x: (
                x["base_comp_date"],
                x["priority"],
                x["work_date"],
            )
        )

        for item in due_targets:
            comp_date = self.adjust_to_business_day(item["base_comp_date"])

            # 이미 다른 대체휴무가 배정된 날짜면 다음 영업일로 이동
            while comp_date in self.used_comp_off_dates:
                comp_date = self.adjust_to_business_day(
                    comp_date + timedelta(days=1)
                )

            item["assigned"] = True
            item["comp_off_date"] = comp_date

            self.used_comp_off_dates.add(comp_date)

            # 대체휴무일에는 해당 담당자 근무 제외
            self.blocked_dates.setdefault(item["assignee"], set()).add(comp_date)

            # 결과에도 대체휴무일 반영
            self.rows[item["row_idx"]]["comp_off_date"] = comp_date

    # 대체휴무 대상 등록
    def add_pending_comp_target(
        self,
        row_idx: int,
        work_date: date,
        assignee: str,
        day_type: str,
    ) -> None:
        base_comp_date = self.get_base_comp_off_date(work_date)

        if base_comp_date is None:
            return

        self.pending_comp_targets.append({
            "row_idx": row_idx,
            "work_date": work_date,
            "assignee": assignee,
            "day_type": day_type,
            "priority": self.get_comp_off_priority(work_date),
            "base_comp_date": base_comp_date,
            "assigned": False,
        })

    # 대체휴무 우선순위
    # 1. 토요일
    # 2. 일요일
    # 3. 공휴일
    def get_comp_off_priority(self, d: date) -> int:
        if d.weekday() == 5:
            return 1
        if d.weekday() == 6:
            return 2
        if self.is_holiday(d):
            return 3
        return 99

    # 대체휴무 기본 후보일
    # 토요일 → 월요일
    # 일요일 → 화요일
    # 공휴일 → 다음날부터 영업일 보정
    def get_base_comp_off_date(self, d: date) -> Optional[date]:
        if d.weekday() == 5:
            return d + timedelta(days=2)

        if d.weekday() == 6:
            return d + timedelta(days=2)

        if self.is_holiday(d):
            return d + timedelta(days=1)

        return None

    # 담당자 선택
    # 단일 로테이션 기준으로 균등 배정
    def pick_next_member(
        self,
        members: List[str],
        start_idx: int,
        target_date: date,
    ) -> Tuple[str, int]:
        n = len(members)
        count_type = self.get_count_type(target_date)

        def collect_candidates(enforce_gap: bool) -> List[Tuple[str, int, int]]:
            candidates = []

            for step in range(n):
                idx = (start_idx + step) % n
                member = members[idx]

                # 개인 불가능일 / 대체휴무일 제외
                if target_date in self.blocked_dates.get(member, set()):
                    continue

                # 같은 사람 연속 근무 방지
                if (
                    self.config.prevent_consecutive_same_person
                    and self.last_assignee is not None
                    and member == self.last_assignee
                    and n > 1
                ):
                    continue

                # 가능하면 최소 휴식일 확보
                last_duty_date = self.last_duty_date_by_member.get(member)

                if enforce_gap and last_duty_date is not None:
                    diff_days = (target_date - last_duty_date).days

                    if diff_days <= self.config.min_rest_days_between_duties:
                        continue

                candidates.append((member, idx, step))

            return candidates

        # 1차: 당직 텀 조건 적용
        candidates = collect_candidates(enforce_gap=True)

        # 2차: 후보가 없으면 텀 조건만 완화
        if not candidates:
            candidates = collect_candidates(enforce_gap=False)

        if not candidates:
            raise ValueError(
                f"{target_date} 날짜에는 배정 가능한 인원이 없습니다."
            )

        # 균등 배정 우선순위
        # 1. 전체 근무 횟수가 적은 사람
        # 2. 해당 유형 근무 횟수가 적은 사람
        # 3. 기존 순번상 앞 사람
        candidates.sort(
            key=lambda x: (
                self.duty_counts[x[0]]["전체"],
                self.duty_counts[x[0]][count_type],
                x[2],
            )
        )

        selected_member, selected_idx, _ = candidates[0]

        self.duty_counts[selected_member]["전체"] += 1
        self.duty_counts[selected_member][count_type] += 1
        self.last_duty_date_by_member[selected_member] = target_date

        next_idx = (selected_idx + 1) % n
        return selected_member, next_idx

    # 생성 대상 월의 전체 날짜 생성
    def build_month_dates(self) -> List[date]:
        _, last_day = calendar.monthrange(
            self.config.year,
            self.config.month,
        )

        return [
            date(self.config.year, self.config.month, day)
            for day in range(1, last_day + 1)
        ]

    # 전체 모니터링 일정 생성
    def assign(self) -> pd.DataFrame:
        self._reset_state()

        for d in self.build_month_dates():
            # 오늘까지 배정되어야 할 대체휴무를 먼저 반영
            self.allocate_due_comp_offs(d)

            # 이제 평일/주말/공휴일 구분 없이 단일 로테이션 사용
            assignee, self.rotation_idx = self.pick_next_member(
                self.members,
                self.rotation_idx,
                d,
            )

            row_idx = len(self.rows)
            day_type = self.get_day_type(d)

            self.rows.append({
                "date": d,
                "weekday_name": ["월", "화", "수", "목", "금", "토", "일"][d.weekday()],
                "day_type": day_type,
                "rotation_type": "단일 로테이션",
                "assignee": assignee,
                "comp_off_date": None,
            })

            # 주말/공휴일 근무자는 대체휴무 대상 등록
            if day_type in ("토요일", "일요일", "공휴일"):
                self.add_pending_comp_target(
                    row_idx=row_idx,
                    work_date=d,
                    assignee=assignee,
                    day_type=day_type,
                )

            self.last_assignee = assignee

        # 월말 이후로 밀린 대체휴무까지 최종 배정
        self.allocate_due_comp_offs(date.max)

        return pd.DataFrame(self.rows)
