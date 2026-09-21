from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from fuckclassroom.course_selection.helpers import _group_plan_courses, _plan_course_category
from fuckclassroom.plugins import build_plugin_registry


class PlanCourseGroupTests(unittest.TestCase):
    def test_course_property_takes_priority_over_course_type(self) -> None:
        course = {"course_property": "必修", "course_type": "通识必修"}

        self.assertEqual(_plan_course_category(course), "必修")

    def test_common_property_names_are_normalized(self) -> None:
        self.assertEqual(_plan_course_category({"course_property": "通识教育"}), "通识")
        self.assertEqual(_plan_course_category({"course_type": "实践教学"}), "实践")
        self.assertEqual(_plan_course_category({"course_property": "其他性质"}), "其他")

    def test_groups_follow_the_expected_order_and_skip_empty_categories(self) -> None:
        courses = [
            {"course_name": "通识课", "course_property": "通识教育"},
            {"course_name": "任选课", "course_property": "专业任选"},
            {"course_name": "必修课", "course_property": "必修"},
            {"course_name": "未知课", "course_property": ""},
        ]

        groups = _group_plan_courses(courses)

        self.assertEqual(
            [group["label"] for group in groups],
            ["必修", "专业任选", "通识", "其他"],
        )
        self.assertEqual(groups[0]["courses"], [courses[2]])


    def test_selection_info_template_compiles(self) -> None:
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        registry = build_plugin_registry()
        environment = Environment(
            loader=FileSystemLoader(registry.template_directories(template_dir))
        )

        self.assertIsNotNone(environment.get_template("selection/info.html"))


if __name__ == "__main__":
    unittest.main()
