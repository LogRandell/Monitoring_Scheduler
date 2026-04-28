from datetime import date
import json
import os
import sys
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox

from All_func import DutyConfig, DutyScheduler
from Make_excel import export_excel


# 실행 위치 기준 경로 반환
# exe로 실행될 경우와 python 실행을 구분
def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# "2026-05-01" → date 객체 변환
def parse_date(date_text: str) -> date:
    y, m, d = map(int, date_text.split("-"))
    return date(y, m, d)


# config.json 파일 읽기
def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# 개인별 불가능 날짜 문자열 → date set 변환
def convert_unavailable(raw_unavailable: dict) -> dict:
    result = {}

    for name, date_list in raw_unavailable.items():
        result[name] = {parse_date(d) for d in date_list}

    return result


# 실제 당직표 생성 로직
def generate_schedule(config_path: str):
    base_dir = get_base_dir()

    raw = load_config(config_path)

    # 개인별 불가능 날짜 변환
    personal_unavailable = convert_unavailable(
        raw.get("personal_unavailable", {})
    )

    # 설정 객체 생성
    config = DutyConfig(
        year=raw["year"],
        month=raw["month"],

        # 단일 로테이션으로 사용할 인원
        weekday_members=raw["weekday_members"],

        # config.json 기존 형태 유지용 (소스 롤백 가능성 염두)
        holiday_members=raw.get("holiday_members"),

        personal_unavailable=personal_unavailable,

        # 단일 로테이션 기준 전월 마지막 담당자
        prev_weekday_last_member=raw.get("prev_weekday_last_member"),

        # config.json 기존 형태 유지용 (소스 롤백 가능성 염두)
        prev_holiday_last_member=raw.get("prev_holiday_last_member"),

        prevent_consecutive_same_person=raw.get(
            "prevent_consecutive_same_person",
            True
        ),
        min_rest_days_between_duties=raw.get(
            "min_rest_days_between_duties",
            3
        ),
    )

    # 스케줄러 생성
    scheduler = DutyScheduler(config)

    # 출력 폴더 설정
    output_dir = raw.get("output_dir", "./output")

    if not os.path.isabs(output_dir):
        output_dir = os.path.join(base_dir, output_dir) 

    os.makedirs(output_dir, exist_ok=True)

    # 파일명 생성
    output_file = os.path.join(
        output_dir,
        f"Monitoring_Schedule_{config.year}{config.month:02d}.xlsx"
    )

    # 당직 배정 수행
    df = scheduler.assign()

    # 엑셀 파일 생성
    export_excel(scheduler, output_file, df)

    return output_file


# =========================
# GUI 영역
# =========================

class DutySchedulerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("월별 모니터링표 생성기")
        self.root.geometry("560x240")
        self.root.resizable(False, False)

        base_dir = get_base_dir()

        # 기본 config.json 경로
        self.config_path = tk.StringVar(
            value=os.path.join(base_dir, "config.json")
        )

        self.build_ui()

    # UI 구성
    def build_ui(self):
        title = tk.Label(
            self.root,
            text="월별 모니터링표 생성기",
            font=("맑은 고딕", 16, "bold")
        )
        title.pack(pady=15)

        frame = tk.Frame(self.root)
        frame.pack(padx=20, pady=10, fill="x")

        label = tk.Label(frame, text="config 파일:")
        label.grid(row=0, column=0, sticky="w")

        entry = tk.Entry(frame, textvariable=self.config_path, width=55)
        entry.grid(row=1, column=0, padx=(0, 8), pady=5)

        browse_btn = tk.Button(
            frame,
            text="찾기",
            width=8,
            command=self.browse_config
        )
        browse_btn.grid(row=1, column=1, pady=5)

        run_btn = tk.Button(
            self.root,
            text="모니터링표 생성",
            font=("맑은 고딕", 12, "bold"),
            width=20,
            height=2,
            command=self.run
        )
        run_btn.pack(pady=15)

        self.status = tk.Label(
            self.root,
            text="config.json을 선택한 뒤 버튼을 누르세요.",
            fg="gray"
        )
        self.status.pack(pady=5)

    # config 파일 선택
    def browse_config(self):
        file_path = filedialog.askopenfilename(
            title="config.json 선택",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )

        if file_path:
            self.config_path.set(file_path)

    # 실행 버튼 클릭 시
    def run(self):
        config_path = self.config_path.get().strip()

        if not config_path:
            messagebox.showerror("오류", "config.json 파일을 선택하세요.")
            return

        if not os.path.exists(config_path):
            messagebox.showerror(
                "오류",
                f"config 파일을 찾을 수 없습니다.\n\n{config_path}"
            )
            return

        try:
            self.status.config(text="모니터링표 생성 중...", fg="blue")
            self.root.update_idletasks()

            output_file = generate_schedule(config_path)

            self.status.config(text="생성 완료", fg="green")

            messagebox.showinfo(
                "완료",
                f"엑셀 파일 생성 완료\n\n{output_file}"
            )

            # 생성된 파일 자동 열기
            try:
                os.startfile(output_file)
            except Exception:
                pass

        except Exception as e:
            self.status.config(text="오류 발생", fg="red")

            error_text = traceback.format_exc()

            messagebox.showerror(
                "오류",
                f"모니터링표 생성 중 오류가 발생했습니다.\n\n{e}"
            )

            # 로그 파일 저장
            log_path = os.path.join(get_base_dir(), "error_log.txt")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(error_text)


def main():
    root = tk.Tk()
    app = DutySchedulerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()