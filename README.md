# 展馆失物招领台 · 后端

Python + FastAPI + SQLite 实现的失物招领后端。不含图片识别；所有判断都基于
工作人员登记的文字细节与认领者提交的文字答案。现已支持**多个失物招领柜台、
跨柜台装箱调拨、开箱签收与错箱双向清点对上**，以及**不停业柜台盘点与差异复核**。

## 柜台盘点与差异复核如何落地

盘点在营业中完成，**不锁库存**：盘点期间登记、发箱、签收与领取照常进行。
每件失物在某一柜台有一次**连续在柜期间**（stay）：登记、入柜到达、错箱对上
或找回时开始；发箱、交付、签缺件、隔离或确认盘亏时结束。一次“见到”扫描只
为**同一次连续在柜期间**作证。

| 需求 | 实现 |
| --- | --- |
| 工作人员在所属柜台开一轮盘点，每柜最多一轮未结 | `POST /api/staff/stocktakes`；部分唯一索引 `one_open_stocktake_per_counter` 兜底，重复开轮 409 `open_round_exists` |
| 逐件提交现有失物编号 | `POST /api/staff/stocktakes/{round}/scans`，`item_ref` 接受公开编号 `LF-xxxxxx` 或内部 id |
| 不锁库存，登记/箱运/签收/认领照常 | 扫描与关轮只读账面；登记、发箱、签收、预约、交付在盘点期间均可用 |
| 按服务器采纳扫描时的账面状态分类 | `seen`：该件此刻 `held` + `at_counter` + 在本柜 + 未确认盘亏；其余（含未知编号、他柜、在途、缺件、隔离、已交付）记 `off_counter` 并写明 `reason` |
| 盘外扫描不移动实物归属 | `off_counter` 只落一条扫描事实；`items.custody_*` 不变 |
| 每次扫描有客户端唯一请求号，本轮同号同内容重送返原结果、同号改内容拒绝 | `stocktake_scans(round_id, request_no)` 唯一；同哈希 200 + `replayed:true`，异内容 409 `scan_content_conflict` |
| 关轮后不收新扫描，仍可重取原结果 | 关轮后 `POST .../scans` → 409 `round_closed`；`GET .../scans/{request_no}` 或同号重送仍返回原结果（`round_closed:true`） |
| 关轮时以当时可在本柜领取的实物为准，列出见到/待复核并保留盘外扫描 | `POST .../close`：从关轮瞬间 `held + at_counter + 本柜 + 未盘亏` 的实物生成结算清单，`seen`/`pending_review` 两栏，`off_counter_scans` 单列 |
| 只有本轮**这一次连续在柜期间**的有效扫描才算见到；先扫到后离柜不报短缺；同件离柜又回本柜要重扫，新登记/入柜的也须扫到 | 结算按每件的开放 stay 匹配扫描：扫描 `stay_id` 必须等于关轮时的 stay；此前期间的扫描不计；关轮时已不在本柜的件既不“见到”也不算短缺 |
| 已确认未找回的盘亏不重复列为本轮短缺 | 结算候选排除 `inventory_loss_review_id` 非空的件 |
| 结算清单封存，后续调拨与复核不能改写 | 结算行与关轮同事务写入；`BEFORE UPDATE/DELETE` 触发器 `settlement_sealed` 兜底；复核/找回写入独立只增表 |
| 待复核由本柜另一位工作人员（不是关轮人）核实实物在柜或确认盘亏 | `POST .../items/{ref}/review`，`outcome` = `confirmed_at_counter` \| `loss_confirmed`；关轮人 403 `reviewer_is_closer`；每项仅一次 409 `already_reviewed` |
| 结算后这一次在柜已结束，原差异不能再据以确认盘亏 | 复核要求开放 stay 等于结算时 stay，否则 409 `stay_ended_after_settlement` |
| 确认盘亏时仍可办理的预约一并取消 | 同一事务内取消 `active` 预约，`ended_reason = inventory_loss` |
| 禁止再次预约、报到、交付或发箱 | `assert_desk_can_handle_item()` 与发箱 `_dispatch_problem()` 均拒绝 409 `item_confirmed_lost`；位置视图 `reason_code = confirmed_lost` |
| 已发生的报到及接待占用保留 | 取消预约不删除 `reception_checkins`，值班名额仍占用到时段结束 |
| 找回由本柜登记，重新按原规则办理，不恢复旧预约 | `POST .../items/{ref}/find`：解除盘亏拦截并开新 stay；返回 `old_bookings_restored:false` |
| 复核和找回记录可查 | 结算视图与轮次视图内含每项 `review`（结论/复核人/时间）与 `find`（找回人/时间/柜台） |
| 结算、确认盘亏、找回与既有业务并发不留矛盾 | 全部走 `BEGIN IMMEDIATE`；关轮、复核、找回与发箱/交付/报到同事务串行，状态与记录一致 |
| 差异只向本柜工作人员与管理员开放；认领人只看到自己预约被取消的原因 | `/api/staff/stocktakes/*` 非本柜 403 `wrong_counter`，认领者令牌 401；认领者状态新增 `booking.ended_reason_message`，不含盘点/结算/复核字段 |

### 一次盘点的时间线（关轮前调走不算短缺）

设东门柜台有三件：杯（在架）、伞（在架）、包（关轮前新登记）。开轮时扫到杯，
随后伞被发往西门并被签收，包一直没扫：

1. 关轮时可在本柜领取的实物只剩杯和包 → 杯 `seen`，包 `pending_review`；
2. 伞已正常离柜，**不报短缺**，也不在结算清单里；
3. 结算清单封存；轮次内“盘外扫描”仍单独保留；
4. 另一位东门工作人员可对包确认盘亏（取消其仍有效的预约并拦截后续办理），
   或核实其实物仍在柜；
5. 若包在结算后才被调走，再拿这条差异确认盘亏会被 `stay_ended_after_settlement`
   拒绝；找回登记会开一次新的在柜期间，下一轮必须重新扫到。

## 柜台与调拨规则如何落地

升级后内置固定的**总台**（code `MAIN`，id `cnt_main`）：原有工作人员、原有在馆
失物在数据库迁移时全部归到总台；此后新登记的失物归登记人所属柜台保管。

| 需求 | 实现 |
| --- | --- |
| 管理员建柜台、给新工作人员指定柜台 | `POST /api/staff/counters`、`POST /api/staff/operators`（可选 `counter_id`，接受 id 或 code，缺省总台） |
| 发箱填写唯一箱号、目的柜台、非空不重复清单 | `POST /api/staff/boxes`；空单 422、重复 422、箱号内容冲突 409 |
| 出箱标签 = 箱号 + 失物编号（这一趟） | 签收标签 `{box_no, item_code}`；`item_code` 接受公开编号 `LF-xxxxxx` 或内部 id |
| 只能发走本柜台保管、可办理领取的失物 | 仅 `custody_state='at_counter'` 且 `custody_counter_id=本柜台` 可发；在途/缺件/隔离/他柜/已交付均拒绝 |
| 一件不能发 → 整箱不发、预约一个不取消 | 同一 `BEGIN IMMEDIATE` 事务内先全量校验 `problems`，再写箱、转在途、取消预约 |
| 发箱后全部在途，预约取消且不自动恢复 | 物品 `custody_state='in_transit'`；有效预约置 `cancelled / item_dispatched`，单向不恢复 |
| 不能同时交付和发走、不能进两个箱 | 状态门禁 + 部分唯一索引 `one_open_box_line_per_item`（每件至多一条未结在途清单行） |
| 只有目的柜台能签收、且只能签一次 | `POST /api/staff/boxes/{box_no}/receipts`；非目的柜台 403；每箱一行 `receipts` |
| 实际扫描清单可为空；扫到入本柜、没扫到记缺件 | 本箱标签 → `arrived` 入目的柜台；未扫清单行 → `missing` |
| 扫到另一箱尚未收妥这趟的标签 → 实际开箱柜保管并隔离 | 外来标签 → `receipt_scans.scan_kind='wrong_box'`，物品 `quarantined` 在开箱柜台 |
| 原箱也签缺件才解隔离，归找到它的柜台 | `mismatch_matches` 配对后物品置 `at_counter` 于**找到方柜台**；两箱先后顺序结果相同 |
| 只有缺件没找到 → 仍不可领取 | `missing` 状态预约/交付均 409 `item_missing` |
| 拒签未知标签 / 已入库旧标签 / 已在别处找到的标签 / 别箱已报标签签本箱实到 | 分别返回 `unknown_label`、`label_already_warehoused`、`label_already_found_elsewhere`、`label_reported_in_other_box` |
| 冲突或重复扫描 → 整张签收单不生效 | 所有校验先于写入；同单重复 `duplicate_scan`；标签全局唯一接受索引兜底 |
| 缺件与错箱原始记录对上后仍保留 | 签收单、扫描行、`box_items.result` 只增不改；对上仅写 `mismatch_matches` 与状态列 |
| 断线重送返回原发箱/签收记录，顺序无关，改内容不覆盖 | 规范化集合 SHA-256 `request_hash`；同哈希 200 + `replayed:true`，异内容 409 `*_content_conflict` |
| 查箱子发件人、实际签收人、清点结果 | `GET /api/staff/boxes/{box_no}`（含发件柜台/人、签收人/柜台、各项结果与配对） |
| 查单件在哪个柜台 / 为何不可领取 | `GET /api/staff/items/{id}/location` |
| 预约、交付只认真实保管且可办理的柜台 | `assert_desk_can_handle_item()`：非 `at_counter` → 409 状态码；他柜 → 403 `wrong_counter` |
| 不在公网/认领者接口暴露内部清点或私有线索 | 调拨/隔离/缺件明细仅 `/api/staff/*`；公网与认领者响应结构不新增这些字段 |

### 错箱对上的两种顺序（最终状态一致）

设 X 随 A 箱发往甲柜，却被物理装进发往乙柜的 B 箱，由乙柜开 B 时扫到标签 `(A, X)`：

1. **先开 B（找到先于缺件）**：X 在乙柜 `quarantined`；之后甲柜开 A，不扫 X
   （=签缺件）→ 立即配对，X 转为乙柜 `at_counter`。
2. **先开 A（缺件先于找到）**：A 签收单把 X 记 `missing`；之后乙柜开 B 扫到
   `(A, X)` → 立即配对，X 直接转为乙柜 `at_counter`。

两种顺序最终都是：**缺件记录在甲柜、错箱记录在乙柜、实物与办理权在找到它的乙柜**。
若甲柜开 A 时反而去扫 `(A, X)` 想签“本箱实到”，会因该标签已在 B 报到而整单被拒
（`label_reported_in_other_box`）；正确做法是不扫它、登记缺件完成对上。

## 接待排班与到场预约如何落地

原有资格规则不变；在此基础上，到场预约带**接待人力担保**：认领人拿到的时段，
一定能分给一名在本时段本柜台名单中的值班人，不会因为同一值班人挂在多个柜台
的名单里而拿到一张没人能接待的预约。

| 需求 | 实现 |
| --- | --- |
| 接待时段固定为 UTC 整点或半点开始的 30 分钟 | `slot_start` 接受带时区的 ISO 时间，必须落在 `:00` 或 `:30`；`slot_YYYYMMDDTHHMMZ` 亦可；已结束时段 409 `slot_ended` |
| 管理员设置/整体替换某时段接待名单 | `PUT /api/staff/slots/{slot}/roster`（仅管理员），`entries: [{staff_id, counter_ids[]}]` 整体替换 |
| 名单写明每名值班人能在哪些柜台接待 | `counter_ids` 每项接受柜台 id 或 code；同一人对同一柜台重复 422 |
| 同一人同一时段只能接待一单 | 每时段以“人”为名额（一人挂多柜台仍只算一个名额）；`reception_checkins` 唯一索引 `(slot_id, staff_id)` 兜底 |
| 排班不改变工作人员所属柜台与原有权限 | 名单独立于 `staff.counter_id`；不改权限、不改保管柜台 |
| 保管柜台按现有资格选择到场人 | 预约仍由实物保管柜台发起，仍要求两条有效私有线索 |
| 只有全部未报到预约都能分给不同可用值班人时才确认新预约 | 确认前对“现有未报到 + 新预约”做一一匹配；无解 409 `slot_capacity_unavailable` |
| 不能为接新单取消旧单 | 失败路径只读匹配，不写任何取消；旧预约保持原状 |
| 未报到的单不向认领人承诺具体值班人 | 认领者视图只给柜台、时段、状态、原因；不返回名单或人选 |
| 人在时段内到了，保管柜台登记实际接待人 | `POST /api/staff/items/{id}/bookings/{bid}/check-in`，仅该保管柜台可办 |
| 只有名单内、本时段未用过、且接待后不会让其他未报到预约无人可接才接受报到 | 依次校验 `not_on_counter_roster`、`reception_person_already_used`、`checkin_would_strand_other_bookings` |
| 实际接待一经登记，名额保留到时段结束 | 提前交付、资格/调拨/取消后该人名额仍占用；唯一索引保证不能再用 |
| 交付前必须报到 | 无报到 409 `not_checked_in`；仍按原规则复核资格与实物保管 |
| 排班调整不抹掉报到记录，单独改名单不能让已报到有效预约无法交付 | 缩减名单保留全部已报到预约；清空名单不影响已报到单交付 |
| 名单缩减：已报到不动，剩余人力保留尽可能多未报到预约 | 对未报到单做最大匹配；已报到人先占用名额 |
| 同样多时按确认先后保留较早的单 | 匹配按 `seq, id` 升序（先确认先保留） |
| 其余单显示因排班变更取消，恢复名单也不自动恢复 | `ended_reason = roster_changed`，单向 |
| 取消即释放承诺；时段结束仍未交付不能再报到/交付，也不阻挡之后的预约 | 有效期结束的预约扫为 `expired / slot_ended`；仅释放该件的 `one_active_booking_per_item` |
| 已登记的报到与交付历史保留 | `reception_checkins`、`deliveries` 只增不改，交付带 `slot_id`、`reception_staff_id` |
| 工作人员查各柜台在一个时段的预约及办理结果 | `GET /api/staff/slots/{slot}/bookings?counter=`（含名单与各单状态/报到/交付） |
| 认领者查自己的柜台、时段及取消/到期原因 | `GET /api/claimant/items/{code}/status` 的 `booking`（`counter`、`slot`、`ended_reason`） |
| 不向认领者暴露他人预约、内部排班或私有线索 | 认领者接口只回自己的预约，不含名单/人选/他人 |
| 升级保留旧记录；无时段旧预约可补约或取消 | `migrate()` 加列不删数据；`POST .../bookings/{bid}/slot` 补约（原认领人、仅保管柜台），或照常取消；未补时段不能报到/交付（`slot_required`） |

### 关键点：共享值班人只算一个名额

若值班员甲在本时段的名单里同时挂总台与东门台，而他只能接待一单，那么“甲的总台
预约 + 甲的东门台预约”仍然是不可能同时兑现的——系统把它按**一个人**匹配，第二个
预约会被 `slot_capacity_unavailable` 拒绝，且不会取消第一个。增加另一名值班人后
两个预约才能成立。

## 原有资格规则如何落地

| 需求 | 实现 |
| --- | --- |
| 细节初始不公开，公网只能看到公开内容 | `details.is_public`，公网接口只查询 `is_public = 1` |
| 公开用于寻找主人，公开后不能重新算秘密 | 只有 `publish`，没有取消公开接口；`published_seq` 一旦写入不再改变 |
| 公开后仍展示更正后的答案 | 公网接口读取 `details.answer` 当前值 |
| 细节带编号与文本答案 | `details.code` / `details.answer`，同一失物内编号唯一 |
| 工作人员可纠正核对答案 | `POST .../correct` 写入 `detail_corrections` 并递增 `correction_epoch` |
| 去掉首尾空白的文本精确匹配 | `norm()` 只做 `strip()`，大小写、内部空格均敏感 |
| 两条不同的有效私有线索才达标 | 按细节去重后统计 `valid_private`，阈值 2 |
| 公开前提交的正确回答仍有效 | 答案行记录提交事件序号，与 `details.published_seq` 比较 |
| 变更答案按新提交时点判断 | 每次变更插入新的答案行，按最新一行判断 |
| 原样重送不刷新提交时点 | 与最新答案 `strip` 后相同则复用原行，`replayed_identical_answer = true` |
| 更正使更正前的回答永久失效 | 答案行固化 `epoch_at_submission`；与当前 epoch 不符即失效，改回原答案也不恢复 |
| 认领者看不到他人提交 / 逐条比对 / 秘密答案 | 认领者接口只返回自己的答案与聚合状态；逐条结论仅在工作人员接口 |
| 多人达标时工作人员选一人 | 每个失物最多一条 `active` 预约（SQLite 部分唯一索引兜底） |
| 预约不改变实物持有 | 预约只写 `bookings`，`items.status` 保持 `held` |
| 失去资格自动取消，恢复后不自动回来 | 提交答案、更正答案时重算并取消；取消状态单向 |
| 工作人员也可取消 | `POST .../bookings/{id}/cancel` |
| 交付时再核对预约人与当前资格 | `deliver()` 在同一事务内复核预约归属与达标情况 |
| 答错/证据失效不能交付，不能给两人办理 | 复核失败返回 409 并取消预约；`deliveries.item_id` 唯一 |
| 交付后更正保留历史结论与对象，并标注后来失效 | `deliveries` + `delivery_evidence` 快照永不更新；读取时标注 `later_invalidated` |

### 一次关键判定

一条回答只有在下列条件同时满足时才算作**有效私有线索**：

1. `answers.epoch_at_submission == details.correction_epoch`（未被更正作废）；
2. `answers.text` 与提交当时的正确答案 `expected_at_submission` 匹配；
3. `answers.seq < details.published_seq`，或该细节从未公开。

> 关于“原样重送”：严格按“不刷新提交时点”实现。若该回答已被更正作废，
> 原样重送仍落在原答案行上、依旧无效；认领者需要**改变答案文本**形成新提交
> （再改回正确答案即可），才会按新时点重新判定。这一行为有专门测试覆盖。

## 运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export LOF_BOOTSTRAP_TOKEN='换成你自己的随机串'   # 仅首次建管理员用
export LOF_DB='./data/lost_found.db'
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- 交互式文档：<http://127.0.0.1:8000/docs>
- 令牌均为随机串，数据库只存 SHA-256 摘要。
- 认领者令牌由工作人员创建后一次性返回，不存明文。

```bash
# 1) 创建第一个（管理员）工作人员，响应中的 token 只显示一次
curl -X POST localhost:8000/api/staff/bootstrap \
  -H "X-Bootstrap-Token: $LOF_BOOTSTRAP_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"值班员"}'
```

## 跨柜台调拨与开箱签收用法

```bash
H='Authorization: Bearer <管理员token>'; J='Content-Type: application/json'

# 2) 建东门、西门两个柜台，并各招一名驻台工作人员（保存返回的 token）
curl -X POST localhost:8000/api/staff/counters -H "$H" -H "$J" \
  -d '{"code":"EAST","name":"东门服务台"}'
curl -X POST localhost:8000/api/staff/operators -H "$H" -H "$J" \
  -d '{"name":"小东（化名）","counter_id":"EAST"}'

# 3) 登记失物仍走原接口；新失物自动归登记人所在柜台。
#    管理员（总台）登记的杯子归 MAIN，可用 GET /api/staff/items/{id} 核对。

# 4) 总台发箱 BOX-A -> EAST，清单为内部 item id（也接受 LF- 公开编号）。
#    清单非空、不重复；其中任一件非本柜台可发，整箱 409 且不取消任何预约。
curl -X POST localhost:8000/api/staff/boxes -H "$H" -H "$J" -d '{
  "box_no": "BOX-A",
  "destination_counter_id": "EAST",
  "item_ids": ["<item_id>"]
}'
# 断线原样重送（清单顺序无关）-> 200 + "replayed": true，返回原发箱记录；
# 若 item_ids/目的柜台不同 -> 409 box_no_content_conflict，不能覆盖。

# 5) 只有 EAST 柜台的工作人员能签收，且只有一次。
#    scanned_labels 可为 []；标签 = 箱号 + 失物编号(public_code 或 id)。
curl -X POST localhost:8000/api/staff/boxes/BOX-A/receipts \
  -H "Authorization: Bearer <EAST工作人员token>" -H "$J" -d '{
  "scanned_labels": [
    {"box_no": "BOX-A", "item_code": "LF-XXXXXX"}
  ]
}'
# 没扫到的本箱清单件自动记 missing。
# 在 B 箱里扫到 A 箱标签时，照常放进 B 的签收单：
#   {"box_no":"BOX-A","item_code":"LF-YYYYYY"}  -> 错箱/隔离，
# 等 A 箱以“不扫该标签”的方式签收缺件后自动对上，归实际开箱柜台保管。
# 未知标签、已入库旧标签、已在别处报到的标签、单内重复扫描：
# 一律 409 且整张签收单不生效（无签收单、无任何状态变化）。
# 相同 scanned_labels 集合重送 -> 200 + replayed，返回原签收单；
# 不同清单重送 -> 409 receipt_content_conflict。

# 6) 查箱（发件人/实际签收人/清点/配对）与单件位置、不可领取原因
curl localhost:8000/api/staff/boxes/BOX-A -H "$H"
curl localhost:8000/api/staff/items/<item_id>/location -H "$H"

# 7) 预约/交付仍是原接口与原资格规则，但必须由当时 at_counter 保管该件的
#    柜台工作人员办理：在途/缺件/隔离 -> 409 item_in_transit/item_missing/
#    item_quarantined；其他柜台 -> 403 wrong_counter。

# 8) 接待排班：先由管理员写某个时段的名单（整体替换），再预约该时段。
#    时段为 UTC 整点/半点开始 30 分钟；同一值班人可挂多个柜台但仍只算一个名额。
SLOT="$(date -u '+%Y-%m-%dT%H:00:00Z')"           # 或先把分钟对齐到 :00/:30
curl -X PUT "localhost:8000/api/staff/slots/$SLOT/roster" -H "$H" -H "$J" -d '{
  "entries": [{"staff_id":"<值班人id>","counter_ids":["MAIN","EAST"]}]
}'
curl -X POST localhost:8000/api/staff/items/<item_id>/bookings -H "$H" -H "$J" \
  -d "{\"claimant_id\":\"<认领者id>\",\"slot_start\":\"$SLOT\"}"
# 若该时段剩余人力不足以让所有未报到预约各分到一名不同值班人 -> 409
# slot_capacity_unavailable，既有预约不会被取消。

# 9) 认领者在时段内到场，保管柜台登记实际接待人；随后才能交付。
curl -X POST localhost:8000/api/staff/items/<item_id>/bookings/<bid>/check-in \
  -H "$H" -H "$J" -d '{"reception_staff_id":"<值班人id>"}'
curl -X POST localhost:8000/api/staff/items/<item_id>/deliveries \
  -H "$H" -H "$J" -d '{"claimant_id":"<认领者id>"}'

# 10) 工作人员按柜台查该时段的预约与办理结果；认领者只看到自己的预约。
curl "localhost:8000/api/staff/slots/$SLOT/bookings?counter=EAST" -H "$H"
curl "localhost:8000/api/claimant/items/<public_code>/status" -H "<认领者令牌>"

# 11) 柜台盘点（营业中，不锁库存）：开轮 -> 逐件扫描 -> 关轮封存结算。
ROUND="$(curl -sX POST localhost:8000/api/staff/stocktakes -H '<东门工作人员令牌>')"
RID="$(printf '%s' "$ROUND" | python3 -c 'import sys,json;print(json.load(sys.stdin)["round_id"])')"
# 每件失物一次扫描；request_no 为客户端唯一请求号，同号同内容重送返原结果。
curl -X POST "localhost:8000/api/staff/stocktakes/$RID/scans" -H '<东门工作人员令牌>' -H "$J" \
  -d '{"request_no":"scan-0001","item_ref":"LF-XXXXXX"}'
# 关轮：按关轮时可在本柜领取的实物生成见到/待复核/盘外扫描清单并封存。
curl -X POST "localhost:8000/api/staff/stocktakes/$RID/close" -H '<东门工作人员令牌>'
curl "localhost:8000/api/staff/stocktakes/$RID/settlement" -H '<东门工作人员令牌>'

# 12) 差异复核：本柜另一位工作人员核实实物在柜或确认盘亏；找回单独登记。
curl -X POST "localhost:8000/api/staff/stocktakes/$RID/items/<item_id>/review" \
  -H '<东门另一位工作人员令牌>' -H "$J" -d '{"outcome":"loss_confirmed"}'
curl -X POST "localhost:8000/api/staff/stocktakes/$RID/items/<item_id>/find" \
  -H '<东门另一位工作人员令牌>' -H "$J" -d '{"note":"在库房找到"}'
```

可复跑的端到端演示（临时库、全虚构数据，含错箱两种开箱顺序的故事）：

```bash
python3 demo/transfer_demo.py
python3 demo/stocktake_demo.py   # 盘点：扫描、调走不算短缺、复核盘亏与找回
```

## 接口一览

公开（无需令牌）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/public/items?q=` | 公开告示列表（仅公开内容） |
| GET | `/api/public/items/{public_code}` | 单件失物公开详情 |

认领者（Bearer 令牌）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/claimant/answers` | 提交/更新一条答案；只返回聚合资格 |
| GET | `/api/claimant/items/{public_code}/status` | 自己的办理状态 |

工作人员（Bearer 令牌）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/staff/operators` | 管理员创建其他工作人员（可带 `counter_id` 指定柜台） |
| GET | `/api/staff/operators`、`/api/staff/me` | 工作人员及其所属柜台 |
| POST | `/api/staff/counters` | 管理员建柜台（`code` 可省略，自动生成） |
| GET | `/api/staff/counters`、`/{ref}`、`/{ref}/inventory` | 柜台列表/详情/在柜库存（ref 为 id 或 code） |
| POST | `/api/staff/operators/{id}/counter` | 管理员调整工作人员所属柜台 |
| POST | `/api/staff/boxes` | 原保管柜台发箱（唯一箱号、目的柜台、失物清单）；同内容重送 200 |
| GET | `/api/staff/boxes`、`/api/staff/boxes/{box_no}` | 箱列表 / 发件+签收人+清点结果+错箱配对 |
| POST | `/api/staff/boxes/{box_no}/receipts` | 目的柜台提交一次开箱签收（扫描清单可为空）；同内容重送 200 |
| GET | `/api/staff/boxes/{box_no}/receipt` | 原签收单与清点结果 |
| GET | `/api/staff/items/{id}/location` | 单件当前柜台 / 在途·缺件·隔离·已交付·盘亏原因 |
| POST | `/api/staff/stocktakes` | 在所属柜台开一轮盘点（每柜最多一轮未结） |
| GET | `/api/staff/stocktakes?counter=` | 盘点轮次列表（非管理员仅本柜台） |
| GET | `/api/staff/stocktakes/{round}` | 轮次详情（扫描、计数；关轮后含封存结算） |
| POST | `/api/staff/stocktakes/{round}/scans` | 提交一次扫描（请求号幂等、分类、关轮后拒收） |
| GET | `/api/staff/stocktakes/{round}/scans`、`/scans/{request_no}` | 本轮扫描列表 / 按请求号重取原结果 |
| POST | `/api/staff/stocktakes/{round}/close` | 关轮并封存结算清单（见到/待复核/盘外扫描） |
| GET | `/api/staff/stocktakes/{round}/settlement` | 封存结算清单（仅关轮后） |
| POST | `/api/staff/stocktakes/{round}/items/{ref}/review` | 本柜另一人复核待复核项：在柜或盘亏 |
| POST | `/api/staff/stocktakes/{round}/items/{ref}/find` | 本柜登记找回（解除盘亏拦截，不恢复旧预约） |
| POST | `/api/staff/claimants` | 创建认领者并发放令牌（化名即可） |
| POST | `/api/staff/items` | 登记失物与若干私有细节 |
| GET | `/api/staff/items`、`/{item_id}` | 内部全量视图（含答案与更正记录） |
| POST | `/api/staff/items/{id}/details` | 追加细节 |
| POST | `/api/staff/items/{id}/details/{detail_id}/publish` | 公开细节（不可撤销） |
| POST | `/api/staff/items/{id}/details/{detail_id}/correct` | 更正答案，作废旧回答 |
| GET | `/api/staff/items/{id}/claimants` | 内部逐条比对矩阵 |
| POST | `/api/staff/items/{id}/bookings` | 选择一名达标认领者并指定时段安排到场（需时段与接待人力） |
| GET | `/api/staff/items/{id}/bookings` | 预约历史（含柜台、时段、报到、交付） |
| POST | `/api/staff/items/{id}/bookings/{bid}/cancel` | 工作人员取消预约 |
| POST | `/api/staff/items/{id}/bookings/{bid}/check-in` | 保管柜台登记实际接待人（报到） |
| POST | `/api/staff/items/{id}/bookings/{bid}/slot` | 为无时段旧预约补约时段（或改走取消） |
| GET | `/api/staff/slots`、`/api/staff/slots/{slot}` | 时段列表 / 单时段名单与预约视图 |
| PUT | `/api/staff/slots/{slot}/roster` | 管理员整体替换该时段接待名单 |
| GET | `/api/staff/slots/{slot}/bookings?counter=` | 各柜台在该时段的预约及办理结果 |
| POST | `/api/staff/items/{id}/deliveries` | 交付（须先报到；复核后写快照） |
| GET | `/api/staff/items/{id}/delivery` | 历史交付与后来失效标注 |

## 演示

```bash
python3 demo/demo.py             # 资格/预约/更正/交付历史
python3 demo/transfer_demo.py    # 柜台/发箱/错箱隔离与两箱对上/找到柜台交付
python3 demo/reception_demo.py   # 共用值班人、时段名单、报到与名单缩减
python3 demo/stocktake_demo.py   # 营业中盘点、关轮封存、盘亏复核与找回
```

四个脚本都使用临时 SQLite 文件与“认领者甲/乙/丙（化名）”等虚构数据。
`demo/demo.py` 依次演示：

1. 工作人员登记失物与三条私有细节；
2. 甲、乙各自在**公开前**提交两条正确答案，均达标；
3. 公开“贴纸形状”，公开前的正确回答仍有效；
4. 工作人员选择甲并指定接待时段安排到场（实物仍由馆方持有）；
5. 更正“杯底刻字”，甲此前的回答失效、预约自动取消；
6. 向甲交付被拒；
7. 甲原样重送旧答案，仍无效；
8. 乙改答更正后的答案后重新达标；
9. 工作人员重新选择乙、指定时段；乙到场后由保管柜台登记实际接待人并交付；
10. 交付后再更正“贴纸形状”，历史交付与交付对象保持不变，仅标注该依据后来失效。

## 测试

```bash
python3 -m pytest
```

共 82 项测试。原有 23 项覆盖：隐私边界、空白/大小写匹配、公开时点、重送、
更正永久失效、预约互斥与自动取消、交付复核、交付后历史冻结。

`tests/test_reception_slots.py` 的 19 项额外覆盖接待排班：

- 时段必须是 UTC 整点/半点、已结束时段拒绝预约；
- 名单整体替换、版本、重复项/未知人/未知柜台校验、仅管理员可设置、不改所属柜台；
- **共享值班人只算一个名额**：同一人挂两个柜台仍不能让两单同时成立，且不取消旧单；
- 新预约绝不取消旧单（容量不足 409，旧单原样保留）；
- 报到需名单内、本柜台、本时段未用过，且不能让其他未报到预约无人可接；
- 提前交付或取消后，报到人的名额仍占用到时段结束；
- 名单缩减保留已报到与尽可能多、且较早确认的未报到单，其余 `roster_changed`
  取消且恢复名单不复活；清空名单只剩已报到单且仍可交付；
- 时段结束预约 `expired / slot_ended`，不能补报到/交付，也不阻挡新时段；
- 工作人员按柜台查一个时段的预约与报到/交付结果；
- 认领者只见自己的柜台、时段与原因，看不到名单/人选/他人预约；
- 调拨、资格变化与报到/交付不留下矛盾记录。
- **旧库升级**：加列不删旧预约/旧交付；无时段旧预约可补约（原认领人、仅保管柜台）
  或取消，未补时段不能报到/交付。

`tests/test_counters_transfers.py` 的 26 项额外覆盖：

- 建柜台、指定所属柜台、非管理员拒绝；新失物归登记人柜台；
- **旧库迁移**：旧工作人员与旧失物升级后归总台；
- 发箱在途、预约以 `item_dispatched` 取消且不恢复；
- 清单为空/重复/同柜台的校验；一件不可发时**整箱不发、预约不取消**；
- 幂等重送（清单顺序无关）与箱号内容冲突不可覆盖；一件不能进两个箱；
- 在途状态禁止预约/交付；
- 仅目的柜台可签收、仅一次、同内容重送、异内容冲突；
- 部分扫到 → 到达 + 缺件；空单 → 全部缺件；
- 未知标签、单内重复扫描、已入库旧标签、已在别处找到、别箱已报标签签本箱实到，
  均令**整张签收单失效**；
- 错箱两种开箱顺序得到完全相同的最终保管结果，原始缺件/错箱记录保留；
- 对上后由找到它的柜台完成预约与交付；
- 在途期间可更正线索，认领者重新达标后在找到柜台办理；
- 到达后可再次跨柜台转发；
- 公网/认领者接口不出现箱号、`quarantined/missing`、隔离/缺件/错箱等内部清点信息。

`tests/test_stocktakes.py` 的 14 项额外覆盖柜台盘点与差异复核：

- 每柜最多一轮未结、同柜可见/他柜 403/认领者 401、管理员可审阅；
- 扫描按采纳时账面分类（见到 / 他柜、在途、缺件、已交付、未知编号为盘外），
  盘外不移动实物；请求号幂等、改内容 409、关轮后拒收但原结果可重取；
- 关轮按当时可领取实物封存结算，先扫到后离柜不算短缺，新登记/回归须重扫；
- 结算清单数据库层只增不改（`settlement_sealed` 触发器）；
- 复核须本柜另一人且非关轮人、只对 pending、只一次；结算后 stay 已结束则拒绝；
- 确认盘亏取消仍有效预约并拦截预约/报到/交付/发箱，保留已发生报到；
- 找回解除拦截并开新 stay，不恢复旧预约；盘亏未找回不重复报短缺；
- 差异仅本柜与管理员可见，认领者只看到自己预约的取消原因；
- 旧库升级为在架实物补建 stay。

## 目录

```
app/
  main.py             应用入口、路由挂载与错误映射
  db.py               Schema、旧库迁移（总台兜底）、事务、审计、序号发生器
  security.py         Bearer 令牌与身份依赖
  schemas.py          请求模型（拒绝未知字段）
  services.py         领域规则（有效性、资格、预约、交付快照、柜台保管门禁、旧预约补约）
  scheduling.py       接待时段、名单、共享人力匹配、报到、名单缩减与到期
  transfers.py        柜台、发箱、签收、错箱双向对上、位置视图
  stocktakes.py       柜台盘点、扫描分类、关轮封存、差异复核与找回
  routers/
    public.py         公网与认领者接口
    staff.py          工作人员/柜台成员/失物/资格/预约/报到/时段名单/交付
    operations.py     柜台、发箱签收、箱子查询、单件位置
    stocktakes.py     柜台盘点、扫描、关轮结算、复核与找回
demo/
  demo.py             两名认领者的端到端演示
  transfer_demo.py    跨柜台调拨与错箱对上演示
  reception_demo.py   共用值班人、时段名单、报到与名单缩减演示
  stocktake_demo.py   营业中盘点、关轮封存、盘亏复核与找回演示
tests/                规则测试（82 项，可复跑）
```

## 设计说明与边界

- **SQLite 并发**：写操作使用 `BEGIN IMMEDIATE`，资格判定与状态变更同事务，
  避免“检查后失效”的竞态；单机部署足够。
- **审计**：关键状态变更写入 `audit_log`，含操作者与对象，便于复核。
- **不存真实个人数据**：接口只要求化名；演示与测试数据全部虚构。
- **未做**：短信/邮件通知、图片识别、跨馆调拨、令牌轮换与过期策略。
  上线前建议加 HTTPS、令牌过期与速率限制。
