# 展馆失物招领台 · 后端

Python + FastAPI + SQLite 实现的失物招领后端。不含图片识别；所有判断都基于
工作人员登记的文字细节与认领者提交的文字答案。

## 规则如何落地

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
| POST | `/api/staff/operators` | 管理员创建其他工作人员 |
| POST | `/api/staff/claimants` | 创建认领者并发放令牌（化名即可） |
| POST | `/api/staff/items` | 登记失物与若干私有细节 |
| GET | `/api/staff/items`、`/{item_id}` | 内部全量视图（含答案与更正记录） |
| POST | `/api/staff/items/{id}/details` | 追加细节 |
| POST | `/api/staff/items/{id}/details/{detail_id}/publish` | 公开细节（不可撤销） |
| POST | `/api/staff/items/{id}/details/{detail_id}/correct` | 更正答案，作废旧回答 |
| GET | `/api/staff/items/{id}/claimants` | 内部逐条比对矩阵 |
| POST | `/api/staff/items/{id}/bookings` | 选择一名达标认领者安排到场 |
| GET | `/api/staff/items/{id}/bookings` | 预约历史 |
| POST | `/api/staff/items/{id}/bookings/{bid}/cancel` | 工作人员取消预约 |
| POST | `/api/staff/items/{id}/deliveries` | 交付（复核后写快照） |
| GET | `/api/staff/items/{id}/delivery` | 历史交付与后来失效标注 |

## 演示

```bash
python3 demo/demo.py
```

脚本使用临时 SQLite 文件与“认领者甲/乙（化名）”等虚构数据，依次演示：

1. 工作人员登记失物与三条私有细节；
2. 甲、乙各自在**公开前**提交两条正确答案，均达标；
3. 公开“贴纸形状”，公开前的正确回答仍有效；
4. 工作人员选择甲安排到场（实物仍由馆方持有）；
5. 更正“杯底刻字”，甲此前的回答失效、预约自动取消；
6. 向甲交付被拒；
7. 甲原样重送旧答案，仍无效；
8. 乙改答更正后的答案后重新达标；
9. 工作人员重新选择乙并完成交付；
10. 交付后再更正“贴纸形状”，历史交付与交付对象保持不变，仅标注该依据后来失效。

## 测试

```bash
python3 -m pytest
```

23 项测试覆盖：隐私边界、空白/大小写匹配、公开时点、重送、更正永久失效、
预约互斥与自动取消、交付复核、交付后历史冻结。

## 目录

```
app/
  main.py         应用入口与错误映射
  db.py           Schema、事务、审计、序号发生器
  security.py     Bearer 令牌与身份依赖
  schemas.py      请求模型（拒绝未知字段）
  services.py     领域规则（有效性、资格、预约、交付快照）
  routers/        public / claimant / staff 三组接口
demo/demo.py      两名认领者的端到端演示
tests/            规则测试
```

## 设计说明与边界

- **SQLite 并发**：写操作使用 `BEGIN IMMEDIATE`，资格判定与状态变更同事务，
  避免“检查后失效”的竞态；单机部署足够。
- **审计**：关键状态变更写入 `audit_log`，含操作者与对象，便于复核。
- **不存真实个人数据**：接口只要求化名；演示与测试数据全部虚构。
- **未做**：短信/邮件通知、图片识别、跨馆调拨、令牌轮换与过期策略。
  上线前建议加 HTTPS、令牌过期与速率限制。
