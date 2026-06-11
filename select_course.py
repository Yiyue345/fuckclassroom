from __future__ import annotations

import argparse

from fuckclassroom.course_selection import CourseSelectionAssistant, SelectionTarget


def main() -> None:
    parser = argparse.ArgumentParser(description="GUET WebVPN 本科教务选课辅助脚本")
    parser.add_argument("--timeout-ms", type=int, default=120_000, help="页面操作超时时间，默认 120000")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("login", help="打开 WebVPN，手动登录并保存独立会话")
    subparsers.add_parser("open", help="使用已保存会话打开本科教务首页")

    assist_parser = subparsers.add_parser("assist", help="在当前选课页面辅助定位课程并人工确认点击")
    assist_parser.add_argument("--name", default="", help="课程名关键字")
    assist_parser.add_argument("--code", default="", help="课程代码")
    assist_parser.add_argument("--teacher", default="", help="教师名")

    args = parser.parse_args()
    assistant = CourseSelectionAssistant()

    if args.command == "login":
        assistant.login(timeout_ms=args.timeout_ms)
        return

    if args.command == "open":
        assistant.open_home(timeout_ms=args.timeout_ms)
        return

    if args.command == "assist":
        assistant.assist_select(
            SelectionTarget(
                course_name=args.name.strip(),
                course_code=args.code.strip(),
                teacher=args.teacher.strip(),
            ),
            timeout_ms=args.timeout_ms,
        )
        return

    parser.error("未知命令")


if __name__ == "__main__":
    main()
