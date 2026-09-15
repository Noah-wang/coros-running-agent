# COROS Sleep Report

每天早上在一个时间窗口内轮询 COROS。只有检测到昨晚睡眠数据已经同步后，才读取睡眠、睡眠 HRV、每日健康概览、恢复状态和训练负荷，生成一份 Discord 晨报。

## 数据来源

- `querySleepData`：睡眠评分、主睡眠时长、深睡/浅睡/REM、清醒、睡眠窗口、午睡。
- `querySleepHrv`：睡眠 HRV 评估和曲线。
- `queryDailyHealthData`：步数、卡路里、压力、睡眠和心率概览。
- `queryRecoveryStatus`：当前恢复百分比、恢复等级、预计完全恢复时间。
- `queryTrainingLoadAssessment`：近期训练负荷状态。

## 输出结构

```text
## 早晨判断
## 睡眠与恢复证据
## 对今天训练的影响
## 今天的最小动作
## 记录
```

报告只做训练恢复建议，不做医疗诊断；COROS 没返回的数据会标注为缺失。

## Discord 使用

手动生成：

```text
!coros-sleep-report
```

或：

```text
!sleep-report
```

Slash command：

```text
/coros-sleep-report
```

## 环境变量

```bash
COROS_SLEEP_REPORT_ENABLED=true
COROS_SLEEP_REPORT_START_TIME=07:00
COROS_SLEEP_REPORT_END_TIME=12:00
COROS_SLEEP_REPORT_TIMEZONE=America/Los_Angeles
COROS_SLEEP_REPORT_POLL_MINUTES=30
COROS_SLEEP_REPORT_TIMEOUT_SECONDS=240
COROS_SLEEP_REPORT_LLM_TIMEOUT_SECONDS=180
```

- `COROS_SLEEP_REPORT_ENABLED`：是否开启自动睡眠晨报。
- `COROS_SLEEP_REPORT_START_TIME`：每天几点之后开始检查睡眠数据。
- `COROS_SLEEP_REPORT_END_TIME`：每天几点之后停止自动检查。
- `COROS_SLEEP_REPORT_TIMEZONE`：按哪个时区判断早晨时间。
- `COROS_SLEEP_REPORT_POLL_MINUTES`：后台检查间隔。

例如 `07:00-12:00` 表示：早上 7 点后开始轮询；如果 COROS 还没同步睡眠数据，就等待下一轮；同步完成后发送一次；中午 12 点后仍没有数据就跳过当天自动发送。

同一个睡眠日期只会自动发送一次，发送记录保存在 `data/memory.json` 的 `caches.coros-report.sleep_report_sent_dates`，不会进入长期记忆提示词。

## 参考

- COROS 官方 MCP：`querySleepData`、`querySleepHrv`、`queryDailyHealthData`
- Ultrahuman-MCP：morning brief / recovery check 结构
- OpenMinis health-sleep-analysis：睡眠阶段、HRV、趋势分析结构
