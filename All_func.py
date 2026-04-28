from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Set, Tuple

import holidays as pyholidays
import pandas as pd


# 당직표 생성에 필요한 설정값을 담는 클래스
@dataclass
class DutyConfig:
    year: int
    month: int

    # 평일 / 주말·공휴일 로테이션 인원
    weekday_members: List[str]
    holiday_members: List[str]

    # 개인별 불가능 날짜
    personal_unavailable: Dict[str, Set[date]]

    # 전월 마지막 담당자
    # 입력된 사람의 다음 순번부터 이번 달 로테이션 시작
    prev_weekday_last_member: Optional[str] = None
    prev_holiday_last_member: Optional[str] = None

    # 같은 사람이 연속으로 당직 서는 것 방지
    prevent_consecutive_same_person: bool = True

    # 당직 간 최소 휴식일
    # 예: 2면 월요일 당직 후 화/수는 가능하면 제외
    min_rest_days_between_duties: int = 2


class DutyScheduler:
    def __init__(self, config: DutyConfig):
        self.config = config

        # 평일/주말 인원을 합친 전체 인원 목록
        # dict.fromkeys를 사용해서 순서 유지 + 중복 제거
        self.all_members = list(dict.fromkeys(
            config.weekday_members + config.holiday_members
        ))

        # 한국 공휴일 자동 계산
        self.holidays = self._build_kr_holidays(config.year)

        # 내부 상태값 초기화
        self.blocked_dates: Dict[str, Set[date]] = {}
        self.weekday_idx = 0
        self.holiday_idx = 0
        self.duty_counts = {}
        self.rows = []
        self.last_assignee = None

        self._reset_state()

    # 스케줄 생성 전 내부 상태값 초기화
    def _reset_state(self) -> None:
        # 개인별 불가능 날짜
        # 이후 대체휴무일도 여기에 추가되어 해당일 당직 제외 처리됨
        self.blocked_dates = {
            member: set(self.config.personal_unavailable.get(member, set()))
            for member in self.all_members
        }

        # 전월 마지막 담당자의 다음 사람부터 평일 로테이션 시작
        self.weekday_idx = self._get_start_index(
            self.config.weekday_members,
            self.config.prev_weekday_last_member,
        )

        # 전월 마지막 담당자의 다음 사람부터 주말/공휴일 로테이션 시작
        self.holiday_idx = self._get_start_index(
            self.config.holiday_members,
            self.config.prev_holiday_last_member,
        )

        # 담당자별 당직 횟수 관리
        self.duty_counts = {
            member: {
                "평일": 0,
                "주말/공휴일": 0,
                "총합": 0,
            }
            for member in self.all_members
        }

        # 생성 결과 저장용
        self.rows = []

        # 직전 날짜 담당자
        # 연속 당직 방지에 사용
        self.last_assignee = None

        # 사람별 마지막 당직일
        # 퐁당퐁당 방지에 사용
        self.last_duty_date_by_member = {
            member: None
            for member in self.all_members
        }

        # 대체휴무 배정 대기 목록
        self.pending_comp_targets = []

        # 이미 사용된 대체휴무일
        # 동일한 대체휴무일 중복 배정 방지용
        self.used_comp_off_dates = set()

    # 한국 공휴일 생성
    def _build_kr_holidays(self, year: int) -> Set[date]:
        kr_holidays = pyholidays.country_holidays("KR", years=[year])
        return {d for d in kr_holidays.keys()}

    # 전월 마지막 담당자의 다음 순번 계산
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

    # 평일 당직 대상일 여부
    def is_weekday_duty(self, d: date) -> bool:
        return not self.is_weekend(d) and not self.is_holiday(d)

    # 날짜 구분값 반환
    def get_day_type(self, d: date) -> str:
        if d.weekday() == 5:
            return "토요일"
        if d.weekday() == 6:
            return "일요일"
        if self.is_holiday(d):
            return "공휴일"
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
    # 기준일이 도래한 대체휴무를 영업일에 배정하고,
    # 해당 담당자의 blocked_dates에 추가해서 그날 당직을 못 서게 함
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

            # 대체휴무일에는 당직 배정 제외
            self.blocked_dates.setdefault(item["assignee"], set()).add(comp_date)

            # 결과 rows에도 대체휴무일 반영
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

    # 대체휴무 기본 후보일 계산
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

    # 당직자 선택 함수
    # 조건:
    # 1. 개인 불가능 날짜 제외
    # 2. 대체휴무일 제외
    # 3. 연속 당직 방지
    # 4. 가능하면 당직 텀 확보
    # 5. 당직 횟수 균등 배정
    def pick_next_member(
        self,
        members: List[str],
        start_idx: int,
        target_date: date,
        rotation_type: str,
    ) -> Tuple[str, int]:
        n = len(members)

        # 후보자 수집
        # enforce_gap=True면 당직 텀 조건까지 적용
        def collect_candidates(enforce_gap: bool) -> List[Tuple[str, int, int]]:
            candidates = []

            for step in range(n):
                idx = (start_idx + step) % n
                member = members[idx]

                # 개인 불가능일 / 대체휴무일 제외
                if target_date in self.blocked_dates.get(member, set()):
                    continue

                # 같은 사람 연속 당직 방지
                if (
                    self.config.prevent_consecutive_same_person
                    and self.last_assignee is not None
                    and member == self.last_assignee
                    and n > 1
                ):
                    continue

                # 가능하면 당직 텀 확보
                last_duty_date = self.last_duty_date_by_member.get(member)

                if enforce_gap and last_duty_date is not None:
                    diff_days = (target_date - last_duty_date).days

                    # 최소 휴식일 이하이면 후보 제외
                    if diff_days <= self.config.min_rest_days_between_duties:
                        continue

                candidates.append((member, idx, step))

            return candidates

        # 1차: 당직 텀 조건 적용
        candidates = collect_candidates(enforce_gap=True)

        # 2차: 후보가 없으면 당직 텀 조건만 완화
        candidates_relaxed_gap = False
        if not candidates:
            candidates = collect_candidates(enforce_gap=False)
            candidates_relaxed_gap = True

        if not candidates:
            raise ValueError(
                f"{target_date} 날짜에는 배정 가능한 인원이 없습니다."
            )

        # 균등 배정 우선순위
        # 1. 해당 로테이션 횟수가 적은 사람
        # 2. 전체 당직 횟수가 적은 사람
        # 3. 기존 순번상 앞 사람
        candidates.sort(
            key=lambda x: (
                self.duty_counts[x[0]][rotation_type],
                self.duty_counts[x[0]]["총합"],
                x[2],
            )
        )

        selected_member, selected_idx, _ = candidates[0]

        self.duty_counts[selected_member][rotation_type] += 1
        self.duty_counts[selected_member]["총합"] += 1
        self.last_duty_date_by_member[selected_member] = target_date

        next_idx = (selected_idx + 1) % n
        return selected_member, next_idx

    # 생성 대상 월의 전체 날짜 목록 생성
    def build_month_dates(self) -> List[date]:
        _, last_day = calendar.monthrange(
            self.config.year,
            self.config.month,
        )

        return [
            date(self.config.year, self.config.month, day)
            for day in range(1, last_day + 1)
        ]

    # 과거 방식의 대체휴무 배정 함수
    # 현재 assign()에서는 사용하지 않음
    # 필요 없으면 삭제 가능
    def assign_comp_off_dates(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["comp_off_date"] = None

        comp_targets = []

        for idx, row in df.iterrows():
            work_date = row["date"]
            day_type = row["day_type"]

            if day_type in ("토요일", "일요일", "공휴일"):
                base_comp_date = self.get_base_comp_off_date(work_date)

                if base_comp_date is not None:
                    comp_targets.append({
                        "idx": idx,
                        "work_date": work_date,
                        "assignee": row["assignee"],
                        "day_type": day_type,
                        "priority": self.get_comp_off_priority(work_date),
                        "base_comp_date": base_comp_date,
                    })

        comp_targets.sort(
            key=lambda x: (
                x["base_comp_date"],
                x["priority"],
                x["work_date"],
            )
        )

        used_comp_off_dates = set()

        for item in comp_targets:
            comp_date = self.adjust_to_business_day(item["base_comp_date"])

            while comp_date in used_comp_off_dates:
                comp_date = self.adjust_to_business_day(
                    comp_date + timedelta(days=1)
                )

            df.at[item["idx"], "comp_off_date"] = comp_date
            used_comp_off_dates.add(comp_date)

        return df

    # 전체 모니터링 일정 생성
    def assign(self) -> pd.DataFrame:
        self._reset_state()

        for d in self.build_month_dates():
            # 오늘까지 배정되어야 할 대체휴무를 먼저 반영
            # 이렇게 해야 대체휴무일에는 당직이 배정되지 않음
            self.allocate_due_comp_offs(d)

            # 평일이면 평일 로테이션 사용
            if self.is_weekday_duty(d):
                assignee, self.weekday_idx = self.pick_next_member(
                    self.config.weekday_members,
                    self.weekday_idx,
                    d,
                    "평일",
                )
                rotation_type = "평일"

            # 주말/공휴일이면 주말/공휴일 로테이션 사용
            else:
                assignee, self.holiday_idx = self.pick_next_member(
                    self.config.holiday_members,
                    self.holiday_idx,
                    d,
                    "주말/공휴일",
                )
                rotation_type = "주말/공휴일"

            row_idx = len(self.rows)
            day_type = self.get_day_type(d)

            # 당직 결과 저장
            self.rows.append({
                "date": d,
                "weekday_name": ["월", "화", "수", "목", "금", "토", "일"][d.weekday()],
                "day_type": day_type,
                "rotation_type": rotation_type,
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

            # 직전 담당자 기록
            self.last_assignee = assignee

        # 월말 이후로 밀린 대체휴무까지 최종 배정
        self.allocate_due_comp_offs(date.max)

        return pd.DataFrame(self.rows)