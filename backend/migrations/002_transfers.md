# 002 备用具调拨

`transfers` 表记录已确认的备用具调拨。应用启动时 SQLAlchemy 会补建缺失表。

- `station_id`、`demand_asset_id`、`spare_id`：工位、需求器具与备用具编号。
- `work_area`、`shift_start`、`shift_end`：调拨落点与使用区间。
- `dataset_id`：确认时依据的数据批次；后续重导入会触发复核。
- `state`：`locked`（生效）、`conflict`（源数据失效，继续占用）、`released`（已释放）。
- `reason`：冲突或失效原因，供班报与收件箱解释。
- `released_at`、`released_by`、`release_note`：释放信息；已释放记录不再参与规划。

台账、证书、工位需求与调拨路程仍保存在不可变的 `datasets.rows` 中，新增字段为
`range_min`、`range_max`、`station_id`、`shift_start`、`shift_end`、`stop_priority`、
`from_area`、`to_area`、`travel_minutes`。旧批次缺少这些列时按空值处理，不影响证书分析。
