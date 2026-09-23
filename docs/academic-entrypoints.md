# 本科教务功能入口清单

采集日期：2026-09-22。来源：已登录本科教务首页展开后的“学生全部服务”菜单。
这是菜单链接清单，不代表每个功能的业务 API 都已经抓取或测试；权限及菜单可能因账号变化。

直连基址：`https://bkjwtest.guet.edu.cn`。WebVPN 使用项目 `JW_WEBVPN_BASE_URL`，路径保持一致。
不要把包含临时 `token`、`ticket` 的重定向地址保存为功能入口。申请、报名、评价、上报页面可能包含写操作。

以下路径均以 `/student/for-std/` 开头：

| 分类 | 功能 | 路径后缀 |
| --- | --- | --- |
| 综合服务 | 空闲教室查询 | `room-free` |
| 综合服务 | 全校开课查询 | `lesson-search` |
| 综合服务 | 常用文件下载 | `common-file` |
| 综合服务 | 辅导答疑查询 | `counsel-answering` |
| 综合服务 | 缴费信息查询 | `payment` |
| 综合服务 | 创新创业学分查看 | `innovate-credit` |
| 综合服务 | 毕业课程成绩和学分绩查询 | `graduate-score` |
| 学籍 | 学籍信息 | `student-info` |
| 学籍 | 学生信息核对 | `std-info-check-apply` |
| 学籍 | 学籍异动申请 | `std-alteration-apply` |
| 学籍 | 转专业申请 | `change-major-apply` |
| 学籍 | 辅修申请 | `minor-apply` |
| 学籍 | 奖惩信息 | `cancel-punish-apply` |
| 培养方案 | 培养方案 | `program/struct-document/info/index` |
| 培养方案 | 培养方案完成情况 | `program-completion-preview` |
| 培养方案 | 学业预警 | `precaution` |
| 选课 | 选课 | `course-select` |
| 选课 | 我的课表 | `course-table` |
| 选课 | 退役生免修申请 | `exempt-study-apply` |
| 选课 | 重修报名 | `retake-signup` |
| 考试 | 考试信息 | `exam-arrange` |
| 考试 | 缓考申请 | `exam-delay-apply` |
| 考试 | 等级考试 | `other-exam-signup` |
| 成绩 | 我的成绩 | `grade/sheet` |
| 成绩 | 下载电子成绩单 | `student-grade-sheet` |
| 教材 | 教材评价 | `textbook-evaluate` |
| 学分认定 | 课程替代申请 | `course-substitute-apply` |
| 学分认定 | 交流课程学分认定申请 | `credit-certification-apply/course_apply` |
| 学分认定 | 非课程类成果学分认定申请 | `credit-certification-apply/other_apply` |
| 教学质量管理 | 学生总结性评教 | `extra-system/student-summation-forstudent/index` |
| 教学质量管理 | 学生即时性评教 | `extra-system/student-immediate-forstudent/index` |
| 智慧实验教学 | 我的课程 | `extra-system/newcapec-experiment/course/stu` |
| 竞赛 | 竞赛获奖信息上报 | `competition-award-apply` |

实验课详细协议见 [lab-selection.md](lab-selection.md)。普通选课与全校开课已在 `fuckclassroom/course_selection` 中实现。
