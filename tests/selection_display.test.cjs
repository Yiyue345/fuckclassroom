const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const context = { window: {}, document: { querySelectorAll: () => [] } };
vm.runInNewContext(fs.readFileSync('fuckclassroom/plugins/course_selection/static/selection_display.js', 'utf8'), context);
const parse = (text) => JSON.parse(JSON.stringify(context.window.FuckClassroomSelectionDisplay.parseSchedule(text)));

test('separates time and classroom across multiple schedule records', () => {
  const records = parse('1~16周 星期一 1~2节 花江校区 教学楼A101；2-8周 周三 第3-4节（单周） 教学楼B202\n星期五 5-6节<br>待安排');
  assert.equal(records.length, 4);
  assert.equal(records[0].time, '1~16周 星期一 1~2节');
  assert.equal(records[0].room, '花江校区 教学楼A101');
  assert.equal(records[1].time, '2-8周 周三 第3-4节（单周）');
  assert.equal(records[1].room, '教学楼B202');
  assert.equal(records[2].room, '');
  assert.equal(records[3].raw, '待安排');
});

test('preserves unfamiliar schedules and treats markup as text', () => {
  for (const raw of ['线上教学，具体时间另行通知', '1-16周 教室A101', '<img src=x onerror=alert(1)>']) {
    assert.deepEqual(parse(raw), [{ time: '', room: '', raw }]);
  }
  assert.deepEqual(parse(null), []);
  assert.deepEqual(parse(' ；\n '), []);
});

test('removes empty lunch slots from the reported course but retains its actual lesson', () => {
  const lunch = [...'一二三四五六日'].map(day => `1~2周 星期${day} 中午~中午节`);
  const actual = '1~2周 星期日 11~11节';
  assert.deepEqual(parse([...lunch, actual, actual, '未公布'].join('；')), [
    { time: '1~2周 星期日 11节', room: '', raw: actual },
  ]);
  assert.deepEqual(parse(lunch.join('\n')), []);
  assert.deepEqual(parse('时间未公布；教室未公布'), []);
});

test('hides unpublished rooms without removing real rooms or unusual useful records', () => {
  assert.equal(parse('5~6周 星期日 7~8节 教室未公布')[0].room, '');
  assert.equal(parse('1~2周 星期日 1~11节 A101')[0].time, '1~2周 星期日 1~11节');
  assert.equal(parse('1~2周 星期日 11~11节 A101')[0].room, 'A101');
  assert.equal(parse('1~2周 星期一 中午~中午节 A101')[0].raw, '1~2周 星期一 中午~中午节 A101');
});
