
from dataclasses import dataclass  # 保存解析后的字段集合
from datetime import datetime, timedelta, timezone  # 使用带时区的日期，并逐分钟寻找触发点
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # 根据 IANA 时区名称换算本地时间


class CronSyntaxError(ValueError):  # 表示表达式格式不符合本模块支持的 Cron 子集
    pass  # 不增加额外字段


def _parse_field(source: str, minimum: int, maximum: int, weekday: bool = False) -> frozenset[int]:  # 解析单个字段的数字、列表、范围和步长
    values: set[int] = set()  # 用集合去掉重复值
    if not source:  # 不接受空字段
        raise CronSyntaxError("Empty cron field")  # 给出明确错误
    for part in source.split(","):  # 逗号将字段拆成多段
        if not part:  # 拒绝连续逗号与尾随逗号
            raise CronSyntaxError(f"Empty item in cron field: {source}")  # 明确拒绝
        units = part.split("/")  # 将基础表达式与步长分离
        if len(units) > 2:  # 一个项目不能出现多个斜线
            raise CronSyntaxError(f"Invalid step expression: {part}")  # 明确拒绝
        try:  # 数字转换可能失败
            step = int(units[1]) if len(units) == 2 else 1  # 无斜线时步长为1
        except ValueError as error:  # 捕获不合法的步长
            raise CronSyntaxError(f"Invalid step: {part}") from error  # 使用专门的异常
        if step < 1:  # 0 或负步长都不能使用
            raise CronSyntaxError(f"Step must be positive: {part}")  # 防止死循环
        base = units[0]  # 取出步长前面的部分
        if base == "*":  # 星号表示该字段的完整取值范围
            start, end = minimum, maximum  # 遍历此字段支持的最小值和最大值
        elif "-" in base:  # 横线表示闭区间
            segments = base.split("-")  # 将 a-b 拆开
            if len(segments) != 2 or not all(segment.isdigit() for segment in segments):  # 防止错误范围
                raise CronSyntaxError(f"Invalid range: {part}")  # 提示输入不合法
            start, end = map(int, segments)  # 得到区间两端
        elif base.isdigit():  # 单个整数
            start = int(base)  # 解析指定起点
            end = maximum if len(units) == 2 else start  # a/n 代表从 a 到字段最大值按 n 取值
        else:  # 不支持名称以及 L、W、# 等扩展写法
            raise CronSyntaxError(f"Unsupported cron item: {part}")  # 明确显示支持范围
        if start < minimum or end > maximum or start > end:  # 对数字做范围检查
            raise CronSyntaxError(f"Cron value outside [{minimum}, {maximum}]: {part}")  # 拒绝无效数字
        for number in range(start, end + 1, step):  # 按步长产生所有允许的整数
            values.add(0 if weekday and number == 7 else number)  # 星期日的 7 统一映射为 0
    return frozenset(values)  # 返回不可修改集合，后续匹配可以安全复用


@dataclass(frozen=True)  # 一旦解析完成就不再更改
class CronExpression:  # 五字段定时表达式的运行时对象
    minutes: frozenset[int]  # 分钟允许值，0到59
    hours: frozenset[int]  # 小时允许值，0到23
    days: frozenset[int]  # 每月日期允许值，1到31
    months: frozenset[int]  # 月份允许值，1到12
    weekdays: frozenset[int]  # 星期允许值，0或7均代表星期日

    @classmethod  # 提供字符串解析入口
    def parse(cls, text: str) -> "CronExpression":  # 输入格式：分 时 日 月 星期
        if not isinstance(text, str):  # 只支持字符串
            raise CronSyntaxError("Cron expression must be a string")  # 明确错误
        fields = text.split()  # 空格分隔五个字段
        if len(fields) != 5:  # 只支持标准五字段 Cron
            raise CronSyntaxError("Expected five cron fields: minute hour day month weekday")  # 拒绝六字段等扩展
        return cls(_parse_field(fields[0], 0, 59), _parse_field(fields[1], 0, 23), _parse_field(fields[2], 1, 31), _parse_field(fields[3], 1, 12), _parse_field(fields[4], 0, 7, weekday=True))  # 构造已经校验的表达式

    def matches(self, local_time: datetime) -> bool:  # 判断某个本地时间是否满足表达式
        weekday = (local_time.weekday() + 1) % 7  # 将 Python 的周一=0 转换为 Cron 的周日=0
        day_matches = local_time.day in self.days  # 检查日期
        week_matches = weekday in self.weekdays  # 检查星期
        day_is_any = len(self.days) == 31  # 日期字段没有限制时不需要参与 OR 判断
        week_is_any = len(self.weekdays) == 7  # 星期字段没有限制时不需要参与 OR 判断
        if day_is_any and week_is_any:  # 日期和星期都不限制
            calendar_ok = True  # 接受任何日期
        elif day_is_any:  # 只有星期受限制
            calendar_ok = week_matches  # 只检查星期
        elif week_is_any:  # 只有日期受限制
            calendar_ok = day_matches  # 只检查日期
        else:  # 日期和星期都指定时，采用传统 Cron OR 语义
            calendar_ok = day_matches or week_matches  # 两项任意满足即可
        return local_time.minute in self.minutes and local_time.hour in self.hours and local_time.month in self.months and calendar_ok  # 合并所有字段

    def next_after(self, after: datetime, tz_name: str = "UTC", max_days: int = 366 * 5) -> datetime:  # 寻找严格晚于 after 的下一个 UTC 触发时刻
        if after.tzinfo is None or after.utcoffset() is None:  # 防止无时区时间被误解
            raise ValueError("after must be timezone-aware")  # 要求传入明确时区
        try:  # 用户输入的时区可能无效
            target_zone = ZoneInfo(tz_name)  # 加载 IANA 时区
        except ZoneInfoNotFoundError as error:  # Windows 未安装 tzdata 时也可能到这里
            raise ValueError(f"Unknown timezone: {tz_name}; install tzdata if needed") from error  # 提供诊断信息
        current = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)  # 从下一个 UTC 分钟开始寻找
        deadline = after.astimezone(timezone.utc) + timedelta(days=max_days)  # 避免不可能的表达式无限搜索
        while current <= deadline:  # 在允许搜索的时间范围内逐分钟检查
            if self.matches(current.astimezone(target_zone)):  # 转成本地时间后再比较 Cron 字段
                return current  # 返回 UTC，便于统一存储与比较
            current += timedelta(minutes=1)  # 查找下一分钟；UTC 遍历能正确跳过夏令时不存在的本地时刻
        raise ValueError(f"No matching cron time found in {max_days} days")  # 例如非法日期组合长期不出现时报告错误
