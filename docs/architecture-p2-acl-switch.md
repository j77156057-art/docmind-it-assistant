# 架构设计 + 任务分解：DocMind「P2 ACL 切换」（核心切换 + 部门进 ACL）

> 作者：架构师 高见远（Gao） ｜ 对应需求：采购硬缺口域 B 组织模型 P2 工作
> 母体文档：`docs/architecture-procurement-gaps.md`（以下简称「母体」）
> 范围：本轮做「ACL 信任模型从 claims 切到 DB 权威」+「`department` principal_type 进 ACL」+ `oidc_sub` 列 + 部门维护写端。
> 红线：不破坏既有 user/group/role ACL 行为；`groups`/`departments` 不再信任 claims；`roles` 仍取 claims。

---

## 1. 方案概述

### 1.1 与母体的继承关系

| 母体 § | 本轮动作 |
|---|---|
| §441「HMAC 同构本轮不做外键化（留 P2-2）」 | **P2-2 落成本轮 T5**：`principal_id` 不做跨类型外键，改为**应用层校验 + 软约束**（下文 §5 详述）。 |
| §452 U2「部门维护端点留 P2」 | **本轮纳入 T8**：增删部门、增删部门成员（含 DB 方法与 admin 端点）。 |
| §454 U4「ACL 列表读侧富化留 P2」 | **本轮纳入 T6**：`document_acl()` 返回新增 `principal_name`，不改既有字段。 |
| §466 P1 推迟清单（P1-4 / P1-5） | P1-4（富化）、P1-5（部门维护）**均在前述纳入本轮**；其余 P1 项不在本轮。 |
| 母体 §3 `departments`/`user_department` 表 | 已在 `20260922_0013` 建好（纯元数据，**未进 ACL**）；本轮只把 `department` 接入 ACL 执行 + 维护写端，**不重建表**。 |

### 1.2 用户已锁定的 4 项决策（硬约束）

1. **范围 = 核心切换 + 部门进 ACL**
   - 检索 / ACL 执行的 `groups` 改为从 `user_group_memberships` **实时取**（DB 权威）。
   - 新增 `department` principal_type：经 `user_department` 展开 = 部门全员可见。
   - `principal_id` 外键化（P2-2）：本轮以应用层校验+软约束落地（见 §5）。
   - ACL 列表名称富化（P1-4）：读侧新增 `principal_name`。
2. **信任模型 = DB 权威**
   - 检索 / ACL 检查时调用方的 `groups` 与 `departments` 从组织表实时查询；**claims 仅登录时写入组织表，不再作为执行依据**。
   - `roles` 无对应表，仍来自 claims（`principal.acl_roles`），**不受切换影响，务必保留**。
3. **附加项（本轮纳入）**
   - `users` 加 `oidc_sub` 列存原始 OIDC `sub`（明文），与 HMAC `subject_id` 并存。
   - 部门维护写端（P1-5）：增删部门、增删部门成员。
4. **部门同步来源**
   - OIDC `department` 声明 → `user_department`（`sync_org_on_login` 扩展）；声明名**配置化**，默认 `department`，与现有 `group_claim`/`acl_groups` 处理一致；维护端点作为补充手段。

### 1.3 技术难点

1. **信任边界迁移**：4 条执行路径（:1474 / :1574 / :1615 / :1694-1768）当前 `groups` 来自 `principal.acl_groups`（claims）。需改为「`subject_id`（HMAC，来自 claims/session）+ `roles`（claims）+ `groups`/`departments`（DB 解析）」，且保证 `roles` 路径不变。
2. **方言兼容**：PG 原生 SQL 用 `string_to_array(:acl_departments, ',')` 增加 department 分支；portable 路径在 `entries` 并入 departments。`roles` 在 PG 分支已用 `acl_roles` 参数，沿用。
3. **`principal_id` 长度不一致**：`users.subject_id` 是 HMAC `String(64)`，而 `document_acl.principal_id` 是 `String(256)`；`groups.group_key`/`departments.department_key` 为 `String(256)`/`String(64)`。长度不匹配使跨类型真实外键不可行 → 采用应用层校验（§5）。
4. **平滑迁移**：`document_acl` 既有行（user/group/role）仅扩展 CHECK，零改写；`users.oidc_sub` 可空，旧行 NULL。
5. **无新依赖**：全部复用既有栈（FastAPI / SQLAlchemy 2.0 / Alembic / 标准库 csv+io）。

---

## 2. 文件清单（新建 / 修改 + 职责）

| 文件 | 类型 | 职责 |
|---|---|---|
| `migrations/versions/20260922_0015_document_acl_org_switch.py` | **新建** | ① 扩展 `document_acl` CHECK 含 `department`；② `users` 加 `oidc_sub` 列；③ `downgrade()` 反向。幂等、可回滚。 |
| `backend/db_models.py` | **修改** | `UserRecord` 加 `oidc_sub: Mapped[str\|None]`；`DocumentAclRecord.__table_args__` CHECK 改为含 `department`（与迁移一致，双写约束）。 |
| `backend/auth.py` | **修改** | `Principal` 加 `departments`/`oidc_sub` 字段；`OIDCAuthenticator` 加 `department_claim`（默认 `department`）；`_principal_from_claims`/`_principal` 提取部门与 oidc_sub。 |
| `backend/database.py` | **修改** | ① 新增 `_resolve_principal_groups`/`_resolve_principal_departments`（T2）；② 改造 4 执行路径（T3）；③ `set_document_acl` 加 `department` + 校验（T4）；④ `principal_id` 应用层校验（T5）；⑤ `document_acl`/`document_access` 富化 `principal_name`（T6）；⑥ `sync_org_on_login` 部门同步 + oidc_sub 写入（T7）；⑦ 部门维护 4 方法 + `subject_id_by_oidc_sub`（T8）。 |
| `assistant/service.py` | **修改** | `query()` 停止向 `accessible_document_outline`/`retriever.retrieve` 传 `groups`（T3 最小改动）。 |
| `backend/retrieval.py` | **修改** | `retrieve()` 停止向前向 DB 检索方法传 `groups`（T3）。 |
| `admin_app.py` | **修改** | 新增 4 个部门维护端点（鉴权 + 自审计 + 沿用 `_export_rows`/utf-8-sig）（T8）；`AclEntryReq` 的 `principal_type` Literal 扩到含 `department`（T4）。 |
| `tests/test_p2_acl_switch.py` | **新建** | T9：覆盖 DB 解析、4 执行路径、set_document_acl department 校验、principal_id 校验、富化、sync 部门、端点鉴权/审计/导出。本地 SQLite 全量；PG 专属（真 ANY/级联）按 `@pytest.mark.skipif` 跳过。 |
| `docs/architecture-p2-acl-switch.md` | **新建** | 本文档。 |
| `README.md` / `docs/` 相关迁移说明 | **修改**（可选） | T10：同步信任模型变更、department principal_type、oidc_sub、部门维护端点语义。 |

---

## 3. 精确锚点列表

> 格式：文件:行号 — 改动类型 — 改动摘要

### 写点（ACL 写入）
- `backend/database.py:1770` `set_document_acl` — T4：① `allowed_types` 加 `"department"`；② `normalized` 校验通过后对 `department` 型校验 `department_key` 存在于 `departments` 表；③ `principal_id` 长度 ≤256 已含 department_key(64)。
- `backend/database.py:1803` `document_acl` — T6：返回 dict 增加 `principal_name`（按 type 查 `users.display_name`/`groups.display_name`/`departments.name`）。
- `backend/database.py:1812` `document_access` — T6：复用 `document_acl`，自动带 `principal_name`。
- `backend/db_models.py:237-239` `DocumentAclRecord.__table_args__` — T4/Migration：CHECK 改为 `IN ('user','group','role','department')`。
- `backend/db_models.py:555` `UserRecord` — T1：`oidc_sub: Mapped[str | None]`（可空）。

### 读点（ACL 执行 / 富化）
- `backend/database.py:1474` `accessible_document_outline` — T3：去掉 `groups` 参数；内部 `groups=self._resolve_principal_groups(subject_id)`、`departments=self._resolve_principal_departments(subject_id)`；`acl_matches` 增 `department` 分支；`roles` 仍来自 claims 参数。
- `backend/database.py:1559-1594` `lexical_search`（PG 分支）— T3：① `:1575-1577` 增加 `(a.principal_type='department' AND a.principal_id=ANY(string_to_array(:acl_departments, ',')))`；② `:1587-1588` 传参增加 `"acl_departments": ",".join(departments)`；`groups` 改为从 DB 解析。
- `backend/database.py:1602-1672` `_postgres_hybrid_search` — T3：① `:1615-1617` 增加 department 分支；② `:1664-1665` 传参增 `acl_departments`。
- `backend/database.py:1674-1768` `_portable_hybrid_search` + `_document_allowed` — T3：① `:1694` 的 `acl_by_document` 不变；② `:1696-1697` 改为 `roles`/`groups`/`departments` 均从 DB 解析（去掉入参 groups）；③ `:1756` `_document_allowed` 去掉 `groups` 入参、加 `departments` 入参；④ `:1764-1768` 增加 `any(("department", d) in entries for d in departments)`。

### 解析点（DB 权威）
- `backend/database.py` 新增 `_resolve_principal_groups(subject_id)->set[str]`（查 `user_group_memberships`，T2）。
- `backend/database.py` 新增 `_resolve_principal_departments(subject_id)->set[str]`（查 `user_department`，T2）。

### 同步点（登录写组织表）
- `backend/database.py:2303` `sync_org_on_login` — T7：① 写 `user_department`（对每个 `principal.departments`）；② 写 `users.oidc_sub`（`principal.oidc_sub`，非空且旧值为空时写入）。
- `backend/auth.py:173` `Principal` — T7：加 `departments: frozenset[str]`、`oidc_sub: str | None`。
- `backend/auth.py:656` `_principal_from_claims` / `:666` `_principal` — T7：从 `self.department_claim` 提部门；`oidc_sub=str(claims.get("sub"))`。
- `backend/auth.py:267` `OIDCAuthenticator.__init__` — T7：加 `department_claim: str = "department"`，存 `self.department_claim`。

### 端点（部门维护写端）
- `admin_app.py:577` `replace_document_acl` — T4：`payload.entries` 的 `principal_type` 现可含 `department`（`AclEntryReq` Literal 扩展）。
- `admin_app.py` 新增 4 端点（T8）：`POST /api/admin/org/departments`、`DELETE /api/admin/org/departments/{department_key}`、`POST /api/admin/org/departments/{department_key}/members`、`DELETE /api/admin/org/departments/{department_key}/members/{subject_id}`。

### 调用方（最小改动）
- `assistant/service.py:159-164` `query` — T3：保留 `roles = principal.acl_roles`（claims）；删除 `groups = principal.acl_groups` 局部变量。
- `assistant/service.py:170-171,187-188` — T3：去掉两处 `groups=groups` 关键字参数（`accessible_document_outline` 已无 `groups` 形参）。
- `assistant/service.py:220-221` — T3：`retriever.retrieve(...)` 去掉 `groups=groups`。
- `backend/retrieval.py:23-25,45,53` — T3：`retrieve()` 保留 `groups` 形参（兼容 `evaluation.py:113` 调用），但不再前向 DB；DB 方法签名已无 `groups`。

---

## 4. 新 DB 方法签名与调用位置

### 4.1 解析方法（T2，新增于 `QueryDatabase`）
```
_resolve_principal_groups(subject_id: str) -> set[str]
    # SELECT group_key FROM user_group_memberships WHERE subject_id = :subject_id
    # 返回小写 group_key 集合；作为 ACL 执行的 groups 唯一来源（替代入参 groups）

_resolve_principal_departments(subject_id: str) -> set[str]
    # SELECT department_key FROM user_department WHERE subject_id = :subject_id
    # 返回小写 department_key 集合；department 型 ACL 展开用
```
调用位置：4 条执行路径（§3 读点）内部，按 `subject_id` 调用，取代原 `principal.acl_groups`。

### 4.2 部门维护方法（T8，新增于 `QueryDatabase`）
```
create_department(department_key: str, name: str, *, actor_subject_id: str, request_id: str) -> None
    # upsert departments(department_key, name)；审计 org_department.create；FK 级联 user_department

delete_department(department_key: str, *, actor_subject_id: str, request_id: str) -> None
    # 删 departments(department_key)；user_department 随 ON DELETE CASCADE 清理；审计 org_department.delete

add_user_to_department(department_key: str, *, subject_id: str | None = None,
                       oidc_sub: str | None = None, actor_subject_id: str, request_id: str) -> None
    # 若给 oidc_sub：先用 subject_id_by_oidc_sub 解析为 subject_id（查不到抛 ValueError）
    # upsert user_department(subject_id, department_key)；审计 org_department.member_add

remove_user_from_department(department_key: str, subject_id: str, *,
                            actor_subject_id: str, request_id: str) -> None
    # 删 user_department(subject_id, department_key)；审计 org_department.member_remove

subject_id_by_oidc_sub(oidc_sub: str) -> str | None
    # SELECT subject_id FROM users WHERE oidc_sub = :oidc_sub（端点按 oidc_sub 加成员时解析）
```

### 4.3 `sync_org_on_login` 变化（T7）
- 入参不变（仍为 `principal: Principal`）。
- 新增：在写 `user` 后，对每个 `principal.departments` 写 `user_department`（沿用既有 upsert 范式：先 `session.get` 后 `add`）。
- 新增：`user.oidc_sub = principal.oidc_sub`（仅当 `principal.oidc_sub` 非空且 `user.oidc_sub` 为空时写入，保证幂等不覆盖）。
- 失败仍仅告警、不影响登录（沿用 :265 既有的 try/except）。

---

## 5. principal_id 外键化决策（P2-2）

### 5.1 为何不做法拉第三列外键
`document_acl.principal_id` 是**单一 `String(256)` 列**，需同时承载 4 类主体：
- `user` 型 → 引用 `users.subject_id`（HMAC，`String(64)`）；
- `group` 型 → 引用 `groups.group_key`（`String(256)`）；
- `role` 型 → **无表**（仅 claims），无法外键；
- `department` 型 → 引用 `departments.department_key`（`String(64)`）。

真实外键要求「一列指向一个目标表的一列，且类型/长度一致」。本场景一列指向 4 张表、长度不一致（64 vs 256）、且 role 无表——**任何单列 FK 都不可行**。若拆成 4 列（user_id/group_id/role_id/department_id）则破坏 `uq_document_acl_principal` 同构与全部既有 `principal_type/principal_id` 读写路径，改动面过大且违背「不破坏既有 ACL 行为」红线。故**不做跨类型外键**。

### 5.2 应用层校验 + 软约束（本轮采用）
- 在 `set_document_acl` 写入前，按 `principal_type` 校验被引用主体存在：
  - `user` → `session.get(UserRecord, value)` 必须存在；
  - `group` → `session.get(GroupRecord, value)` 必须存在；
  - `department` → `session.get(DepartmentRecord, value)` 必须存在；
  - `role` → 不做存在性校验（无表；保留现有语义）。
- 不存在则抛 `ValueError("文档 ACL 主体不存在")`，由端点转 400（与现有 `set_document_acl` 校验风格一致）。
- 该校验为「软约束」：DB 层不强制 FK，但**写路径保证不会写入悬空 principal_id**；既有历史行（user/group/role）因早已存在而天然合法，不受影响。
- 读路径（`document_acl` 富化）对查不到名称的主体，回退 `principal_name = principal_id`（不抛错，保证界面/导出不崩）。

### 5.3 可选迁移层约束（取舍说明，本轮不强制实现）
- **方案 A（CHECK/触发器，PG 专属）**：在 `document_acl` 上建触发器函数，按 `principal_type` 查对应表存在性，不存在则拒绝写入。优点：DB 层硬保证；缺点：SQLite 不支持、需方言分支、与「应用层校验」重复，且 role 无表无法统一表达。→ **本轮不采用**，仅在文档记录为可选增强。
- **方案 B（应用层校验，采用）**：如上 §5.2。优点：双方言一致、零迁移风险、覆盖全部 4 型（含 role 的「不校验」语义）。→ **本轮落地于 T5**。

> 结论：采用方案 B（应用层校验 + 软约束），不建跨类型 FK、不加 DB 触发器。

---

## 6. 迁移设计（`20260922_0015_document_acl_org_switch`）

`down_revision = "20260922_0014"`，`revision = "20260922_0015"`。

### 6.1 upgrade()
1. **扩展 `document_acl` CHECK**（双方言用 `batch_alter_table`）：
   - `batch.drop_constraint("ck_document_acl_principal_type", type_="check")`
   - `batch.create_check_constraint("ck_document_acl_principal_type", "principal_type IN ('user', 'group', 'role', 'department')")`
   - 说明：`op.batch_alter_table("document_acl")` 在 SQLite 会重建表（数据保全），在 PG 为就地操作——与母体 `0004` 用法一致。
2. **`users` 加 `oidc_sub` 列**：
   - `op.add_column("users", sa.Column("oidc_sub", sa.String(length=256), nullable=True))`
   - 旧行 `oidc_sub` 为 NULL，兼容。
3. `departments`/`user_department` 已在 `0013` 建好，**不重建**。

### 6.2 downgrade()
1. `op.drop_column("users", "oidc_sub")`
2. `batch_alter_table("document_acl")`：`drop_constraint` 新 CHECK → `create_check_constraint` 回退为 `IN ('user','group','role')`。

### 6.3 幂等 / 可回滚
- Alembic 线性迁移，单次应用；CHECK 修改在 `batch_alter_table` 内完成，SQLite 重建表时保全数据。
- `downgrade` 完整反向，可 `alembic downgrade -1` 干净回退。
- PG 方言行为由 CI 校验；本地 SQLite 跑 `alembic upgrade head` / `downgrade -1` 验证（本机无 PG，注明「由 CI 验证」）。

---

## 7. 端点规格（部门维护写端，T8）

> 复用 `require_capability`（admin_app.py:238）+ `record_audit_event` + `_export_rows`（utf-8-sig）既有范式。请求体沿用 Pydantic `BaseModel`。

| 端点 | Method | 鉴权 capability | 审计 action | 请求体 | 说明 |
|---|---|---|---|---|---|
| `/api/admin/org/departments` | POST | `acl.write` | `org_department.create` | `{department_key:str, name:str}` | 调 `create_department`；`department_key` 长度 ≤64、非空。 |
| `/api/admin/org/departments/{department_key}` | DELETE | `acl.write` | `org_department.delete` | — | 调 `delete_department`；`user_department` 随级联清理。 |
| `/api/admin/org/departments/{department_key}/members` | POST | `acl.write` | `org_department.member_add` | `{subject_id?:str, oidc_sub?:str}`（二选一） | 调 `add_user_to_department`；先 `subject_id_by_oidc_sub` 解析（给了 oidc_sub 时）。 |
| `/api/admin/org/departments/{department_key}/members/{subject_id}` | DELETE | `acl.write` | `org_department.member_remove` | — | 调 `remove_user_from_department`。 |

补充约定：
- 读端点 `GET /api/admin/org/departments`（已存在，:1159）不变，仅元数据。
- 端点错误：`ValueError` → 400（与 `replace_document_acl` 一致）；鉴权不足 → 403（`require_capability` 统一）。
- 审计 `target_type` 用 `department`/`department_member`，`target_ref` 用 `department_key`（成员操作用 `department_key:subject_id`）。

> capability 选择说明：部门直接决定「谁可见文档」（department 型 ACL），与既有 `replace_document_acl` 同受 `acl.write` 管辖，故部门结构写端沿用 `acl.write`（而非 `document.write`）。**列为待确认项**（见 §10）。

---

## 8. 有序任务列表（T1–T10）

> 规则：T1（底座）→ T2/T4/T5/T6/T7/T8 可并行依赖 T1；T3 依赖 T2；T7 依赖 T8 的 auth.py 部分（Principal/OIDCAuthenticator 字段，可并入 T7 同批）；T9 依赖全部实现；T10 收尾。

### T1 · 迁移 + oidc_sub 列
- **文件**：`migrations/versions/20260922_0015_document_acl_org_switch.py`（新建）、`backend/db_models.py`（CHECK + oidc_sub 双写）。
- **做什么**：按 §6 扩展 `document_acl` CHECK 含 `department`、给 `users` 加可空 `oidc_sub`；`UserRecord` 加字段、`DocumentAclRecord` CHECK 改 `('user','group','role','department')`；`downgrade` 反向。
- **依赖**：无。
- **优先级**：P0。
- **验收**：`alembic upgrade head` 在 SQLite 生成 oidc_sub 列、CHECK 更新；`alembic downgrade -1` 干净回退；`import backend.db_models` 通过；PG 行为由 CI 验证。

### T2 · DB 解析方法
- **文件**：`backend/database.py`（新增 `_resolve_principal_groups` / `_resolve_principal_departments`）。
- **做什么**：两个方法按 `subject_id` 查 `user_group_memberships` / `user_department`，返回小写集合。
- **依赖**：T1（仅逻辑，无需迁移）。
- **优先级**：P0。
- **验收**：单测（SQLite）给定 memberships，返回正确集合；空 subject 返回空集。

### T3 · 4 条执行路径改造（DB 权威）
- **文件**：`backend/database.py`（:1474 / :1574 / :1615 / :1694-1768）、`assistant/service.py`（:159-171,187-188,220-221）、`backend/retrieval.py`（:23-25,45,53）。
- **做什么**：4 路径去掉 `groups` 入参，内部用 `_resolve_principal_groups`/`_resolve_principal_departments`；PG SQL 增 department 分支 + 传 `acl_departments`；`_document_allowed` 增 department 判定；`roles` 仍来自 claims 入参。`service.py`/`retrieval.py` 停止前向 `groups`。
- **依赖**：T2。
- **优先级**：P0。
- **验收**：单测验证「用户属某 group/department → 该 group/department 型 ACL 文档可见；直接 claims groups 不再生效（DB 无 membership 则不可见）」；`roles` 型 ACL 仍按 claims 生效；`hybrid_search`/`lexical_search`/`accessible_document_outline` 三条路径均覆盖。

### T4 · set_document_acl + CHECK 扩展
- **文件**：`backend/database.py`（:1770 `set_document_acl`）、`admin_app.py`（:58 `AclEntryReq` principal_type Literal）。
- **做什么**：`allowed_types` 加 `"department"`；`department` 型校验 `department_key` 存在于 `departments` 表（错误 → 400）；`AclEntryReq.principal_type` Literal 扩到含 `"department"`。
- **依赖**：T1。
- **优先级**：P0。
- **验收**：写含 department 的 ACL 成功且落地 `document_acl`；不存在的 department_key 被拒（400）；user/group/role 行为不变。

### T5 · principal_id 应用层校验（P2-2 软约束）
- **文件**：`backend/database.py`（`set_document_acl` 内新增 `_validate_acl_principal` 助手）。
- **做什么**：按 §5.2，写入前按 principal_type 校验 user→users / group→groups / department→departments 存在；role 不校验；不存在抛 `ValueError`。
- **依赖**：T4（在同一写入路径）。
- **优先级**：P0。
- **验收**：悬空 user/group/department principal_id 被拒；既有合法行不受影响；单测覆盖三类拒绝 + role 放行。

### T6 · ACL 列表富化（P1-4）
- **文件**：`backend/database.py`（:1803 `document_acl`、:1812 `document_access`）。
- **做什么**：`document_acl()` 返回每行增加 `principal_name`（user→users.display_name / group→groups.display_name / department→departments.name；查不到回退 principal_id）；`document_access` 自动继承。不改既有字段。
- **依赖**：T1。
- **优先级**：P1（本轮纳入）。
- **验收**：管理端 `/api/admin/documents/{id}/acl` 返回含 `principal_name`；既有 `principal_type`/`principal_id` 字段顺序/结构不变；导出模板无需改（仅多一列）。

### T7 · sync_org_on_login 部门同步 + oidc_sub
- **文件**：`backend/auth.py`（:173 `Principal`、:267 `OIDCAuthenticator`、:656/:666 提取）、`backend/database.py`（:2303 `sync_org_on_login`）。
- **做什么**：`Principal` 加 `departments`/`oidc_sub`；`OIDCAuthenticator` 加 `department_claim`（默认 `department`），`_principal_from_claims` 提取部门与 oidc_sub；`sync_org_on_login` 写 `user_department` + `users.oidc_sub`（幂等）。non-OIDC 登录 departments/oidc_sub 为空（默认），不写。
- **依赖**：T1（oidc_sub 列）、T8 的 auth.py 字段（可同批）；建议与 T8 的 auth 改动一并。
- **优先级**：P0。
- **验收**：OIDC 登录含 `department` 声明 → `user_department` 出现对应行；`users.oidc_sub` 写入原始 sub；二次登录幂等不重复建行、不覆盖已有 oidc_sub；同步失败登录仍成功。

### T8 · 部门维护端点 + DB 方法
- **文件**：`admin_app.py`（新增 4 端点）、`backend/database.py`（新增 `create_department`/`delete_department`/`add_user_to_department`/`remove_user_from_department`/`subject_id_by_oidc_sub`）。
- **做什么**：按 §7 实现 4 端点（`acl.write` 鉴权 + 自审计 + utf-8-sig 沿用）；DB 方法 upsert/删除 departments 与 user_department，oidc_sub 解析；`subject_id_by_oidc_sub` 查不到抛错。
- **依赖**：T1（oidc_sub 列）、T4（department 校验范式）。
- **优先级**：P1（本轮纳入）。
- **验收**：管理员可建/删部门、加/删成员（按 subject_id 或 oidc_sub）；viewer 访问 403；每次写产生 `org_department.*` 审计行；FK 级联清理成员。

### T9 · 测试
- **文件**：`tests/test_p2_acl_switch.py`（新建）。
- **做什么**：覆盖 T2/T3/T4/T5/T6/T7/T8 行为；端点鉴权 403、审计行、导出 header/BOM；PG 专属（真 `string_to_array`/级联）`@pytest.mark.skipif` 跳过并注明「由 CI 验证」；保留并核对既有 `test_sso_rbac_acl.py`/`test_document_classification.py` 等因签名变化需更新的用例。
- **依赖**：T3、T4、T5、T6、T7、T8。
- **优先级**：P0。
- **验收**：`.venv` 内 `pytest tests/test_p2_acl_switch.py` 全绿；改动后本地跑**全量** `pytest` 通过。

### T10 · 文档同步
- **文件**：`docs/`（更新迁移说明/变更日志）、`README.md`（如有相关章节）。
- **做什么**：记录信任模型切换、department principal_type、oidc_sub、部门维护端点与 capability 语义；最终跑全量 `pytest`。
- **依赖**：T9。
- **优先级**：P0。
- **验收**：文档与实现一致；全量 pytest 通过。

---

## 9. 共享约定（跨文件）

1. **审计写法**：所有写/导出操作经 `record_audit_event`（actor 截断 64、action 截断 64、target_type 截断 32、target_ref 截断 512、request_id 取 `request_id_context.get()`）。部门写端 action 命名 `org_department.create/delete/member_add/member_remove`。
2. **能力校验**：端点统一 `require_capability(...)`；不足 403。部门写端沿用 `acl.write`（同 `replace_document_acl`）。
3. **配置化声明名**：`OIDCAuthenticator` 新增 `department_claim`（默认 `department`），与既有 `group_claim` 处理同构；解析用既有 `_claim_values(claims, path)`（支持 list/单值/逗号串）。
4. **utf-8-sig 导出**：部门相关导出（如有）复用 `_export_rows`，列序 `department_key` 首、`created_at` 末，BOM 便于 Excel CJK。
5. **写入非致命**：`sync_org_on_login` 失败仅 `log_event(WARNING,...)` 后继续登录（沿用 :265 范式），不阻断主流程。
6. **HMAC 同构与零迁移**：既有 `document_acl` 的 user/group/role 行因仅扩展 CHECK 不受影响；`subject_id`（HMAC）与 `principal_id`(user) 同构保持不变。
7. **幂等范式**：部门 upsert 用「先 `session.get` 后更新/插入」（参考 `set_runtime_model_config`/`sync_org_on_login`）；`oidc_sub` 仅空值时写入，避免覆盖。
8. **无新第三方依赖**：全部复用既有栈（FastAPI / SQLAlchemy 2.0 / Alembic / 标准库 csv+io）。

---

## 10. 待明确事项（含默认建议）

| # | 事项 | 默认建议（本轮采用） |
|---|---|---|
| U1 | 部门维护端点 capability | 默认 `acl.write`（部门直接决定文档可见性，与 `replace_document_acl` 同管辖）。备选 `document.write`；若后续引入 `org.write` 再迁移。 |
| U2 | `department_claim` 默认值 | 默认 `"department"`，配置项可覆盖（与 `group_claim` 同机制）。 |
| U3 | `add_user_to_department` 同时给 `subject_id` 与 `oidc_sub` | 二选一；都给时以 `subject_id` 优先，`oidc_sub` 忽略（或校验一致）。 |
| U4 | department 型 ACL 是否支持 `parent_key` 层级展开 | 本轮**不展开**（仅直接部门成员可见）；父部门可见性留后续（需递归展开，非本轮范围）。 |
| U5 | `principal_id` 应用层校验对 `user` 型是否强制 | 本轮**强制**（user/group/department 均校验存在）；role 不校验（无表）。与既有「不校验」相比更严格，但仅拒绝悬空值，不影响存量。 |
| U6 | `oidc_sub` 唯一性 | 不加唯一约束（同一 OIDC sub 可能因 salt 变化产生不同 subject_id，罕见）；仅作登录同步写入与端点解析用。如需唯一可后续评估。 |

---

## 11. 任务总数与依赖结论

- **任务总数**：10 个（T1–T10）。P0 主线 = T1/T2/T3/T4/T5/T7/T9/T10；P1 纳入本轮 = T6（ACL 富化）、T8（部门维护）。
- **新依赖**：无。
- **principal_id 外键化最终方案**：不做跨类型外键（单列 `principal_id` 长度 64/256 不一致且 role 无表，真 FK 不可行）；采用**应用层校验 + 软约束**——`set_document_acl` 写入前按 principal_type 校验 user/group/department 主体存在于对应组织表（role 不校验），拒绝悬空值；DB 触发器/CHECK 方案列为可选增强、本轮不实现。
