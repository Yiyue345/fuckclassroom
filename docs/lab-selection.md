# 实验课选课

插件 ID 为 `lab_selection`，入口为 `/lab-selection`，不依赖 `course_selection` 插件，可单独启用：

```powershell
$env:FUCKCLASSROOM_PLUGINS="lab_selection"
python main.py
```

支持按学期分页查询课程、查看实验与已选状态、整组选课和单个实验的组合时间段选课。
提交前重新查询名额、时间、已选状态或冲突标记；网络中断不自动重发，提交后查询已选状态。
安装依赖：`python -m pip install -r requirements/lab_selection.txt`，以及 `python -m playwright install chromium`。

## 自动候补

整组或实验时间段旁的“加入候补”会创建本地自动任务，定期读取教务状态，在有空位且满足条件时自动提交。它不是学校提供的排队队列，不保证能选上。
默认检查间隔 30 秒，可设置 15–3600 秒。任务保存在 `data/lab_selection/waitlist.json`，页面显示检查次数、提交次数、下次检查时间和结果，并支持取消。
候补需要保持应用运行；退出后停止，重启恢复等待任务。插件热重载会先停止旧实例，避免重复后台执行。
会话或网络失效时延后检查，重新登录后继续。任务绑定创建时的教务账号，切换账号会暂停候补。
提交前再次核对并先持久化“正在提交”；提交结果不明或重启时发现中断提交，会转为“待人工核对”，不会自动重发。核对后可取消旧任务，再重新添加。
提交期间取消只能停止后续尝试，已发出的请求仍会核验并保留实际结果。

`auth/academic.py` 按需创建 `academic_session`，两个插件共用同一个本地会话对象、登录锁、凭据服务和 Hy2 实例。
沿用 `data/webvpn` 中的会话文件，不复制 Cookie，也不单独维护实验课账号。
普通选课的 worker、自动选课与路由仍只在 `course_selection` 启用时创建。
仅实验课插件启用时，由它处理会话清除、网络设置失效和代理退出清理。

共享会话内部复用 `CourseSelectionAssistant` 的认证实现；实验业务通过独立服务组合它。
实验教学有自己的短期令牌：教务会话访问入口得到教育令牌，再交换成实验后端的 `X-Access-Token`。
令牌只留在单次操作内存中，不写入文件、不复用普通选课 Token。

Codex 内嵌浏览器与应用的 Playwright 会话存储不同；内嵌浏览器登录用于接口调查，不会自动更新应用保存的会话。插件若提示会话失效，请点击“登录本科教务”。

## 协议来源与验证范围

2026-09-22，实际菜单为“智慧实验教学 → 我的课程”，入口为 `/student/for-std/extra-system/newcapec-experiment/course/stu`。
前端为 `/guet-lab-web/`，后端为同一教务主机的 `/guet-lab-system`。
协议从学校部署的 `app.3623b8c9.js`、`chunk-1935f5f0.d81e5eb6.js`、`chunk-15786e90.f5ff23f1.js`、`chunk-48885192.988bd727.js` 提取。
浏览器核对了当前学期空列表、上一学期课程、整组已选状态、名额、选课起止时间及组内实验时间地点。
没有执行真实选课或退课。请求构造、响应处理、提交校验及不重试行为通过模拟接口测试；完整客户端尚未以应用保存会话做端到端实网验证。

以下均为相对 `/guet-lab-system` 的上游 GET 接口：

| 功能 | 路径 | 参数 |
| --- | --- | --- |
| 换取实验令牌 | `/api/authentication/getAccessTokenByEduToken` | `token` |
| 学期 | `/mesTeacherCalendar/mesTeacherCalendar/getTeachCalendarOptions` | 无 |
| 我的课程 | `/experiment/mesTeachTask/queryListByStuId` | `teacherCalendarId`, `pageNo`, `pageSize`；上游支持 `archive` |
| 课程数量 | `/experiment/mesTeachTask/queryStuCourseCountInfo` | `teacherCalendarId` |
| 实验项目及分组 | `/experiment/mesTeachTask/getSubjectSelectionList` | `taskId` |
| 可选时间组合 | `/schedule/ScheduleItemBySubject/getItemListToSelect` | `subjectId`, `taskId`, `theoryFlag=true` |
| 整组选课 | `/schedule/ScheduleItemBySubject/stuSelectGroup` | `groupId`, `taskId`, `selectWey=1` |
| 时间组合选课 | `/schedule/ScheduleItemBySubject/stuSelectItem` | `itemIds`（逗号分隔）, `taskId`, `stuId`, `selectWey=1` |
| 整组退课（仅记录，未实现） | `/schedule/ScheduleItemBySubject/stuDropCourseByGroup` | `groupId`, `taskId` |
| 实验退课（仅记录，未实现） | `/schedule/ScheduleItemBySubject/stuDropCourse` | `subjectId`, `taskId`, `stuId` |

上游选课虽然使用 GET，本地提交接口必须使用 POST，不会被查询或自动重试触发。
响应封装为 `{success, message, result}`；课程列表为 `result.records`。整组含 `groupInfo`, `hasSelect`, `selectCount`, `maxCount`, `itemList`；单项分类含 `type`, `needComplete`, `selectCount`, `list`。时间组合含 `list` 与 `isConflict`, `isTheoryConflict`, `isFull`, `isEnding`, `notBegin`。

其他菜单见 [academic-entrypoints.md](academic-entrypoints.md)。文档不保存身份信息、Cookie、令牌或个人课程数据。分析材料在被 Git 忽略的 `output/academic-discovery/` 中。
