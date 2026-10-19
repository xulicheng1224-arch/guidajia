# 鬼打架 CSV 格式规范（v1.1）

## 通用规则

- 文件为 UTF-8 编码，允许 BOM；英文逗号分隔，首行为英文列名，区分大小写。导出统一使用 UTF-8 BOM 和 CRLF，空数据也保留表头。
- 每次导入最多 1000 条、5 MB。先预览、确认后写入；重复及无效记录逐条跳过并显示原因。文件表头错误、重复列名、未知列名或列数错误会阻止整个文件导入。
- 含逗号、双引号或换行的单元格用双引号包裹；内部双引号写成两个双引号。建议使用下载的模板，避免手工拼接 CSV。
- 数字不带货币符号、千位分隔符。淘宝商品 ID、SKU ID 和系统商品 ID 都作为**文本**导入 Excel / Numbers，避免科学计数法和长整数丢失精度。
- 布尔值填写 `true` / `false`。数组和对象填写 JSON，例如 `["显卡","自营"]`；在 CSV 文件中为 `"[""显卡"",""自营""]"`。
- 可选单元格留空表示使用默认值。日期为 `YYYY-MM-DD`。时间为含时区的 ISO 8601，例如 `2026-10-18T12:00:00+08:00` 或 `2026-10-18T04:00:00Z`。
- 导出包含系统元数据 `id,kind,version,created_at,updated_at,deleted_at`，导入时忽略这些列，生成新的系统 ID。导入是新增，不会覆盖已有商品或自动恢复回收站内容。已有商品按规范化后的完整商品及 SKU 链接去重。
- 导出自由文本如果可能被表格软件识别为公式（包括空白、制表符、换行或 BOM 后的 `= + - @`），会添加单引号保护。原本以单引号开头的文本会双写该引号，重新导入时还原原文；旧版导出的保护空格也兼容。
- JSON 导入和导出继续可用。CSV 用于交换商品清单和价格，完整恢复请使用数据库备份；它不会恢复 Cookie、访问令牌、采集任务或全部审计信息。

## 商品清单（product.csv）

模板：在「商品库 → 导入」或「设置 → 导入导出 → 商品清单 → 导入」中下载。

| 列名 | 规则 |
| --- | --- |
| `url` | 可暂留空；有链接时为淘宝 / 天猫商品完整链接。保留 `id` 和 `skuId`，例如 `https://item.taobao.com/item.htm?id=123&skuId=456`。短链接不支持。 |
| `name` | 无链接时**必填**。有链接而名称为空时自动生成待识别名称；自填名称不校验是否与页面标题一致。 |
| `category` | 分类，默认 `显卡`。 |
| `brand,model,spec,shop` | 品牌、型号、规格、店铺，自由文本。名称、规格文字不能代替真实 SKU ID。 |
| `shop_type` | `官方旗舰` / `授权店` / `普通店`，默认普通店。 |
| `group` | 分组，默认 `观察中`。 |
| `tags,expected_options` | JSON 字符串数组，默认 `[]`。expected_options 为页面应匹配的规格选项。 |
| `msrp` | 建议零售价，**整数分**，例如 `199900` 为 1999 元；空值表示未填写。 |
| `release_date` | 上市日期，可空。 |
| `notes` | 备注，可含换行。 |
| `interval_hours` | 采集间隔（小时），0.1–8760，默认 4。 |
| `paused` | 布尔值。**无论文件如何填写，导入后始终暂停采集**，需要在页面主动恢复。 |
| `watch,baseline` | 是否关注、是否纳入价格参考，默认均为 `true`。 |

最小示例：

```csv
url,name,category,spec
https://item.taobao.com/item.htm?id=123&skuId=456,示例显卡,显卡,16GB
```

商品 CSV 的导出列顺序固定为：元数据六列 + 模板中的商品字段顺序。导出的商品清单可直接用于导入；同链接已存在会跳过。无链接的参考商品按名称、品牌、型号和规格去重，保持暂停采集；后续在编辑页补链接，系统 ID 和历史价格不变。

## 历史价格（observations.csv）

先导出商品清单取得系统 `id`，填入价格文件的 `product_id`。**不是淘宝商品 ID**。价格文件只允许关联系统中已存在且未删除的商品。新导入的商品会生成新 ID，跨数据库迁移时必须先映射这些 ID。

| 列名 | 规则 |
| --- | --- |
| `product_id` | **必填**，系统商品 ID。 |
| `observed_at` | **必填**，带时区的观测时间，不能在未来。 |
| `price` | **必填**，真实成交口径价格，正数，最高 1 亿元。 |
| `price_unit` | **必填**，`yuan` 表示元（最多两位小数），`cent` 表示分（必须为整数）。**所有导出金额均为分，price_unit 固定 cent**。 |
| `original_price` | 原价，可空，单位同 price。 |
| `stock` | `in_stock` / `out_of_stock` / `delisted` / `unknown`，默认 `in_stock`。 |
| `price_basis` | `regular` 普通价 / `subsidy` 补贴价 / `coupon` 券后价 / `member` 会员价 / `reference` 表格参考价。无链接时使用 reference；有链接时默认取 price_type 或 regular。不同口径分开训练。 |
| `price_type` | 兼容列；建议直接填 price_basis。 |
| `item_id` | 淘宝数字商品 ID，可空，自动从当前链接补齐；填写时必须匹配链接。 |
| `requested_sku_id` | 监测的数字 SKU ID，可空，自动从当前链接补齐；填写时必须匹配链接。 |
| `selected_sku_id,sku_verified` | 页面实际选中的数字 SKU ID 和验证结果，可空。链接缺少 skuId 时，只有填写已确认的 selected_sku_id 且 sku_verified=true 才能确定训练序列。 |
| `exclude_prediction` | 是否明确排除训练，默认 false。 |
| `reason` | 人工数据说明，可空。 |
| `data_quality` | `measured` 实测（默认）或 `carried_forward` 沿用前值；两者按用户设置均作为有效价格参与展示、统计、训练与未来标签；标记仅用于来源追溯。 |
| `import_source,source_cell,source_note` | 原文件名、单元格位置和原始数据性质，选填，随导出保留。 |
| `reference_product_id` | 导出诊断列；参考价由服务器归入当前 product_id，不用填写或伪造淘宝 ID / SKU。 |

```csv
product_id,observed_at,price,price_unit,price_basis
替换成已有系统商品ID,2026-10-01T12:00:00+08:00,1999.00,yuan,regular
```

导出固定列顺序：

```text
id,product_id,observed_at,price,price_unit,original_price,stock,price_type,price_basis,item_id,requested_sku_id,selected_sku_id,sku_verified,exclude_prediction,reason,data_quality,import_source,source_cell,source_note,status,valid,source,corrected,raw_price,raw_original_price,reference_product_id
```

- `price` 为应用人工修正后的当前有效价格；`raw_price,raw_original_price` 为原始观测金额（分），仅供核对，重新导入时不覆盖 price。
- `status,valid,source,corrected` 是导出诊断列。失败或无效记录可以导出，但不会被当作真实价格导入；人工已修正且 valid=true 的价格按有效价格导入。
- reference 价格独立归入当前系统商品的参考序列，不能同时填写淘宝商品 ID 或 SKU。补链接后保留该参考序列，新的淘宝 SKU 价格另建序列，不自动混合未知口径。
- 所有有效人工导入价格（包括沿用前值标记）与采集价格同等使用；人工价格在同日、同规格和同口径冲突时优先，原始值保留。
- 有效导入价格统一标记为人工真实价格，保留 SKU、价格口径和排除训练标志。满足对应数据覆盖、库存及训练条件后参与模型训练。
- 同系统商品、同一时刻、同价格、同 SKU、同价格口径的人工记录会跳过；如果只有相同自动采集记录，仍新增人工确认记录以建立人工优先级；相同时刻的不同价格口径可以分别保存。
- 逐日补录也可在「商品详情 → 按天编辑价格」完成，不必构造时间文件。

## 其他对象

事件日历支持 CSV / JSON；提醒规则、视图配置通常使用 JSON，接口也支持按模板导入 CSV。模板仅含表头，不附带会误导入的示例数据。

- 事件必填 `name,start,end`；结束日期不得早于开始日期。type 为 `大促/新品发布/官宣降价/供应变化/政策窗口`，confidence 为 `官宣/传闻/待确认`。其他列为 `scope,scope_value,source,notes,known_at,ignored`；known_at 导入时由系统记录，避免把后知事件当成提前已知。
- 提醒必填 `name`；列为 `name,enabled,scope,scope_value,condition,value,days,cooldown_minutes,confirmations,include_coupon,channels,template`。channels 为 JSON 数组；涉及某个商品的 scope_value 必须使用当前系统商品 ID。
- 备注必填 `product_id,text`；其余列 `type,start,end,on_chart`。区间备注需填开始、结束时间。
- 视图必填 `name`；config 为 JSON 对象。
- 日志、审计 CSV 仅用于核查，不提供导入；列名前缀固定，其余诊断字段追加在末尾。

CSV 是新增数据入口；需要恢复全部历史、撤销记录及关联 ID 时，使用「设置 → 保留与备份」的数据库备份。
