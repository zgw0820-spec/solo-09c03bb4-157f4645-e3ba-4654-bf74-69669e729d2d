# 在线加外键 API（FastAPI + PostgreSQL）

巡检库 `inspections.device_id` 历史上没有外键，采集程序直连 PostgreSQL 写库，
留下了一批指向已删除设备的**孤儿记录**。本服务用 PostgreSQL 官方的在线两步法
（`NOT VALID` → `VALIDATE CONSTRAINT`）在**不锁表、不中断采集**的前提下补上外键。

## 实现原理

| 步骤 | SQL | 锁行为 | 效果 |
|---|---|---|---|
| 开启保护 | 已有等价外键则沿用；否则 `ALTER TABLE inspections ADD CONSTRAINT fk_inspections_device FOREIGN KEY (device_id) REFERENCES devices(id) NOT VALID` | 仅短暂目录锁，**不扫表** | 立即约束所有新写入/更新（含绕过 API 直连写库的采集程序）；数据库拒绝删除仍被引用的设备 |
| 全表验证 | `LOCK TABLE inspections IN SHARE UPDATE EXCLUSIVE MODE` + `ALTER TABLE inspections VALIDATE CONSTRAINT <选中的约束名>`（同一事务） | `SHARE UPDATE EXCLUSIVE`，**不阻塞读写** | 扫描存量数据；扫描期间的合法写入照常提交（新行在写入时已被约束检查，MVCC 保证组合安全） |
| 验证失败 | — | — | 约束保持 `NOT VALID`，**写入保护不解除**；清完孤儿可重验 |

状态全部来自 PostgreSQL 系统目录 `pg_constraint`（`convalidated`），
API 进程无内存状态，**重启后查到的就是数据库里的真实状态**。

### 外键的认定：按定义，不按名字

线上库可能已经被人工补过约束，名字也不统一。本服务只认**形状**：

| 条件 | 说明 |
|---|---|
| `inspections.device_id` → `devices.id` | 单列外键，目标必须是 `devices.id` |
| `NOT DEFERRABLE` | 不可延迟 |
| `ON UPDATE NO ACTION` + `ON DELETE NO ACTION` | 更新、删除动作都是 NO ACTION |

不满足这些条件的一律不算数——包括指向归档设备表（如 `device_archive`）的
同名外键、`DEFERRABLE`、`ON DELETE CASCADE`、多列外键。

- **status**：任意名字的等价外键都会报 `exists/enforced:true`；多条等价时
  优先报**已验证**的那条（`equivalent_constraints` 列出全部）。
- **enable**：已有等价外键就沿用（`changed:false`，不新建）。没有等价外键
  时才按默认名 `fk_inspections_device` 新建；默认名被别的约束占用 → **409**，
  不删不改原约束。
- **validate**：按目录选中等价外键后，在**同一事务**里
  `LOCK TABLE inspections IN SHARE UPDATE EXCLUSIVE MODE`（只挡
  `RENAME/DROP/REPLACE`，不挡普通读写），再按 OID 重读、执行
  `VALIDATE CONSTRAINT <实际名字>`。若选定后约束在拿到锁前被改名或替换：
  没被替换就按新名字验证；已被替换（OID 消失）则重查目录重新选择，连续
  3 次都在变才返回 409。**不会验证错约束。**
- **首次 enable 不扫存量**：带 `NOT VALID`，旧孤儿原样保留，只保护新写入；
  之后的全表验证发现旧孤儿 → 409，约束保持 `NOT VALID`，写入保护不解除；
  扫描本身不阻塞合法写入。

## 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /foreign-key/status` | 等价外键的实际启用/验证状态（读系统目录，按定义识别，重启后仍准确） |
| `POST /foreign-key/enable` | 第 1 步：有等价外键就沿用，否则 `ADD CONSTRAINT ... NOT VALID`，幂等；默认名被占用 → 409 |
| `POST /foreign-key/validate` | 第 3 步：锁定并全表验证**选中的那条**约束；有孤儿返回 409（附孤儿数量与样本），保护保持开启 |
| `GET /inspections/orphans` | 运维查孤儿记录（`device_id` 无对应设备的行） |
| `POST /inspections/{id}/reassign` | 凭记录号 + 所见设备号改挂：`UPDATE ... WHERE id=? AND device_id=?`；设备号已变返回 409 并附当前值；目标设备不存在返回 409 |
| `POST /inspections` / `GET /inspections` | 录巡检；保护开启后指向不存在设备 → 409（数据库拒绝） |
| `POST /devices` / `GET /devices` / `DELETE /devices/{id}` | 设备管理；删除仍被引用的设备 → 409（数据库拒绝） |

错误映射：PG `23503`（外键违反）→ HTTP 409；乐观锁失配 → 409；记录不存在 → 404。

## 启动

### Docker Compose（推荐）

```bash
docker compose up -d --build
# Swagger UI: http://localhost:8000/docs
```

`db` 首次启动会用 `db/init.sql` 灌入样例库：5 台在役设备 + 4 条孤儿巡检
（指向已删除的设备 6/7/8）。

### 无 Docker 的环境（本地直跑）

需要 `pip install -r requirements-dev.txt pgserver`（pgserver 自带 PG 16 二进制）：

```bash
bash scripts/local/run_local.sh     # 起 PG(55432) + uvicorn(8000) + 跑测试
bash scripts/local/stop_all.sh      # 停掉
```

## 在 Swagger 演示迁移

打开 `http://localhost:8000/docs`，按顺序操作（与 `scripts/demo.sh` 逐步对应，
可先看 `bash scripts/demo.sh` 的真实输出）：

1. **`GET /foreign-key/status`** → `exists:false`（还没有约束）
2. **`GET /inspections/orphans`** → 4 条孤儿（device_id 6/6/7/8）
3. **`POST /inspections`** `{"device_id":9999,"note":"..."}` → 201（保护开启前还能产生孤儿）
4. **`POST /foreign-key/enable`** → `changed:true, enforced:true, validated:false`
   （定义里带 `NOT VALID`）
5. **`POST /inspections`** 同样报文 → **409**（数据库拒绝）；`DELETE /devices/1` → **409**（仍被引用）
6. **`POST /foreign-key/validate`** → **409**：`orphan_count:5` + 孤儿样本，写入保护保持
7. **`POST /devices`** 建一台 `decommissioned-pool` 设备，记下 id
8. **`POST /inspections/{id}/reassign`**：故意填错 `expected_device_id` → **409**
   返回当前真实设备号；逐条按所见设备号改挂 → 200
9. **`GET /inspections/orphans`** → `count:0`
10. **`POST /foreign-key/validate`** → 200，`validated:true`
11. `docker compose restart app`（或本地 `bash scripts/local/restart_app.sh`）后
    **`GET /foreign-key/status`** → 依旧 `exists/enforced/validated:true`

### 已补过外键的库（异名复用 / 同名冲突）

`scripts/demo_conflicts.sh`（破坏性，跑完用 `reset_db.sh` 或 `down -v` 重置）
演示上面「按定义认定」的规则：

1. 人工建 `fk_manual_2024`（正确形状、异名）→ status 直接报它，enable 沿用
   且**不新建**默认名约束；改名后 validate 仍验证的是同一条（按 OID）。
2. 建指向 `device_archive` 的同名约束 → status 报 `exists:false`，
   enable 返回 **409** 并附占位约束定义，原约束**原样保留**。

## 自测

```bash
bash scripts/selftest.sh          # Docker 版：重建全新卷 → 起栈 → pytest
bash scripts/local/run_local.sh   # 无 Docker 版：同一套 pytest
```

`tests/test_migration.py`（28 项，按序有状态）覆盖：

- 开启前：可查到孤儿、可新造孤儿（复现现状）
- 开启保护：幂等；API 与**直连 SQL** 的非法写入都被 23503 拒绝；被引用设备删除被拒
- 验证失败：有孤儿时 409，保护不解除
- 改挂：设备号已变 → 409 冲突；目标设备不存在 → 409；记录不存在 → 404；逐条补正到 0 孤儿
- **验证与写入交错**：先批量灌 150 万行让全表验证有可观耗时，后台线程调
  `/foreign-key/validate`，主线程用 `pg_stat_activity` 确认 `VALIDATE CONSTRAINT`
  真正在跑后持续直连写库——合法行全部提交、非法行全部被拒
- 验证幂等；**重启后**状态仍为已启用已验证、保护仍生效
- **按定义认定**：异名等价外键复用（不再建默认名）；多条等价优先报已验证；
  指向归档表/`DEFERRABLE`/`CASCADE` 的同名或同目标约束都不算等价；
  默认名被无关约束占用 → enable 409 且原约束不变（可重复触发）
- **首次 enable 不扫存量**：带 `NOT VALID`，旧孤儿保留，新写入立刻被拒
- **验证交错**：选定约束后改名（同 OID）→ 按新名字验证；拿到锁前被
  drop+重建（新 OID）→ 重查目录改验证新约束（或冲突），绝不误验旧选择；
  凡失败均保持 `NOT VALID`，写入保护不解除、合法写入不受阻

### 实测结果（本仓库提交时的运行环境：Python 3.11 + PostgreSQL 16.2，无 Docker）

```
tests/test_migration.py .... 28 passed in ~28s
交错实测：VALIDATE 扫描 150 万行期间，合法直连写入照常提交、非法写入逐条被拒
（本机每次窗口内约 2 + 2 条；条目数取决于扫描时长，关键是两类都非零）
重启 API 进程后：GET /foreign-key/status → exists/enforced/validated 全为 true，
非法写入仍 409
```

## 待办（TODO）

- **鉴权与审计**：迁移/改挂接口目前无认证，需接 SSO/RBAC，并记录谁在何时改挂了哪条
- **改挂留痕**：改挂会覆盖原 `device_id`，应加 `original_device_id`/`reassigned_*` 审计列或审计表
- **采集程序改造**：直连写库在约束生效后会收到 23503，采集端需处理该错误（重试/告警），长期应改走 API 或消息队列
- **锁等待防护**：`enable` 的短暂目录锁在业务高峰可能排队，建议 `lock_timeout` + 重试；`validate` 遇长事务持锁冲突时同理
- **大表观测**：`VALIDATE CONSTRAINT` 无进度视图，超大表需靠 `pg_stat_activity` 观测耗时；必要时选低峰窗口
- **删除策略**：当前 FK 为默认 `NO ACTION`；如业务需要退役设备，可评估 `ON DELETE SET NULL` 或软删除
- **批量改挂**：孤儿量大时需要分页 + 批量改挂接口与限速
- **多约束并存**：若库中同时存在多条等价外键（如人工遗留 + 本服务新建的），
  当前只验证/报告优先级最高的那条（已验证优先），其余保持原状；需要时再提供
  「清理重复外键」的运维脚本
- **锁等待防护（本环境未做）**：`validate` 的 `LOCK TABLE` 未设
  `lock_timeout`，遇到别人长事务持 `ACCESS EXCLUSIVE` 会一直排队；建议上线前
  加超时与明确的 503/409 映射
- **CI**：本环境无 Docker 守护进程，`docker compose` 栈未实际启动过；
  `docker-compose.yml` 仅做了 YAML 校验，完整验证请在有 Docker 的环境跑
  `scripts/selftest.sh`（本地无 Docker 路径已跑通全部 28 项）
