from __future__ import annotations

import unittest

from fuckclassroom.course_selection.catalog import (
    _CatalogOptionsParser,
    _catalog_query_params,
    _normalize_catalog_course,
)
from fuckclassroom.course_selection.models import CourseSelectionApiError


class CatalogParsingTests(unittest.TestCase):
    def test_parses_official_filter_options(self) -> None:
        parser = _CatalogOptionsParser()
        parser.feed(
            """
            <select id="semester">
              <option value="320">旧学期</option>
              <option value="321" selected>当前学期</option>
            </select>
            <select id="courseTypeAssoc">
              <option value="">全部</option>
              <option value="2">学科基础</option>
            </select>
            <select id="campusAssoc"><option value="3">花江校区</option></select>
            """
        )

        self.assertEqual(parser.default_semester, "321")
        self.assertEqual(len(parser.options["semesters"]), 2)
        self.assertEqual(parser.options["course_types"][0]["value"], "2")
        self.assertEqual(parser.options["campuses"][0]["label"], "花江校区")

    def test_query_params_are_whitelisted_and_keep_multiple_periods(self) -> None:
        params = _catalog_query_params(
            {
                "course_name": "高等数学",
                "department_id": "12",
                "credits_min": "2.5",
                "periods": ["1", "2"],
                "ignored": "unsafe",
            },
            2,
            50,
        )

        self.assertIn(("courseNameZhLike", "高等数学"), params)
        self.assertIn(("openDepartmentAssocs", "12"), params)
        self.assertIn(("creditsGte", "2.5"), params)
        self.assertEqual([value for key, value in params if key == "courseIndexs"], ["1", "2"])
        self.assertNotIn("ignored", [key for key, _ in params])

    def test_rejects_invalid_filter_values(self) -> None:
        with self.assertRaisesRegex(CourseSelectionApiError, "节次"):
            _catalog_query_params({"periods": ["13"]}, 1, 50)

    def test_normalizes_course_capacity_and_schedule(self) -> None:
        course = _normalize_catalog_course(
            {
                "id": 88,
                "code": "CLS-01",
                "nameZh": "数据结构-01",
                "course": {"code": "CS101", "nameZh": "数据结构", "credits": 3},
                "openDepartment": {"nameZh": "计算机学院"},
                "courseType": {"nameZh": "专业基础"},
                "teacherAssignmentList": [
                    {"person": {"nameZh": "张老师"}},
                    {"person": {"nameZh": "张老师"}},
                ],
                "scheduleText": {"dateTimePlaceText": {"textZh": "1~16周 星期一 1~2节 花江校区"}},
                "stdCount": 60,
                "limitCount": 60,
            }
        )

        self.assertEqual(course["lesson_id"], 88)
        self.assertEqual(course["teachers"], "张老师")
        self.assertEqual(course["campus"], "花江校区")
        self.assertTrue(course["is_full"])
        self.assertEqual(course["available_count"], 0)


if __name__ == "__main__":
    unittest.main()
