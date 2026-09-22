# DocMind 正文脱敏（document_chunks.content 字段级加密）架构设计

> 范围：仅加密 `document_chunks.content` 列，复用既有 `IT_QUERY_FIELD_KEY` + `FieldEncryptor`。
> 同构对象：`QueryDatabase.record()` / `history()` 对 `query.question` 的字段级加密。
> 本文档只做设计与任务分解，不含实现代码。

## 1. 实现方案概述

### 1.1 目标
对 `document_chunks.content` 做字段级加密存储；`search_text` 保持归一化明文（词法/混合检索依赖它），`embedding` 向量不动（基于明文在写入前生成）。

### 1.2 与 `query.question` 加密的同构关系
完全镜像 `QueryDatabase.record()` / `history()` 范式：
- **写**：`record()` 在 `question=self._field_encryptor.encrypt(question)`（`backend/database.py:160`）；对应本特性在 `replace_document_chunks` / `replace_document_chunk_batch` 构造 `DocumentChunkRecord` 前对 `content` 调 `encrypt`。
- **读**：`history()` 在 `self._field_encryptor.decrypt(row.question)`（`backend/database.py:191`）后返回明文；对应本特性在审核预览/检索等读点 `decrypt(chunk.content)` 后返回。
- **不新增密钥/加密器**：`QueryDatabase.__init__` 已构造 `self._field_encryptor`（`backend/database.py:89`），直接复用。
- **旧明文向后兼容**：`FieldEncryptor.decrypt` 遇无 `enc:v1:` 前缀的值原样返回（`backend/crypto.py:57-65`），迁移回填前后的行、以及尚未加密的旧库都能正常读出。

### 1.3 关键技术决策（硬约束）
1. 只加密 `content`；`search_text` 用加密前的明文 `content` 计算（写点在加密前已持有明文）。
2. 复用 `IT_QUERY_FIELD_KEY` + `FieldEncryptor`，不新增环境变量/密钥。
3. 不加新表，`healthcheck()` 的 `required` 集合不变。
4. 向量与检索行为不变（embedding 基于明文生成；词法检索基于明文 `search_text`）。

## 2. 文件清单（新建 / 修改 + 职责）

| 文件 | 类型 | 职责 |
|------|------|------|
| `migrations/versions/20260922_0014_document_chunk_encryption.py` | 新建 | Alembic 迁移：存量 `document_chunks.content` 就地回填加密（幂等）；`down_revision="20260922_0013"`、`revision="20260922_0014"`。 |
| `backend/database.py` | 修改 | 写点（`replace_document_chunks` ~451、`replace_document_chunk_batch` ~487）加密 `content`；读点（`document_version_chunks` ~1051、`lexical_search` PG 分支 ~1564、`_postgres_hybrid_search` ~1642、`_portable_hybrid_search` ~1742）解密 `content`。复用 `self._field_encryptor`。 |
| `worker/graph.py` | 不改（已知限制） | `staging` 落盘 JSON 仍含明文 `content`（~94）。属临时产物，记入 §8 待明确事项 ①。 |
| `ingestion/service.py` | 不改 | `:89`/`:208` 用内存明文 chunk 生成 embedding，写入前完成，无 DB 读，向量正确。 |
| `app.py` / `admin_app.py` | 不改（依赖 DB 层解密） | 端点经 `db.citations()` / `db.document_version_chunks()` / `db.hybrid_search()` 取得已解密的明文，无需各自解密。 |
| `tests/test_chunk_content_encryption.py` | 新建 | 字段加密测试范式（加解密往返、空值、旧明文回退、错误密钥、生产必配校验），对齐 `tests/test_field_encryption.py`。 |

**依赖结论**：本特性不引入任何新第三方依赖（仅复用既有 `cryptography` 与 `IT_QUERY_FIELD_KEY`）。

## 3. 写点 / 读点精确列表

### 3.1 写点（加密，明文 → 密文入库）
| # | 文件:行号 | 方法 | 当前 | 改动 |
|---|-----------|------|------|------|
| W1 | `backend/database.py:451`（块 446-457） | `replace_document_chunks` | `content=chunk.content` | 改为 `content=self._field_encryptor.encrypt(chunk.content)` |
| W2 | `backend/database.py:487`（块 482-493） | `replace_document_chunk_batch` | `content=str(row.get("content") or "")` | 改为 `content=self._field_encryptor.encrypt(str(row.get("content") or ""))` |

**保持不变（明文 / 原样）**：
- `backend/database.py:452` `search_text=lexical_text(f"{chunk.heading} {chunk.content}")` —— 使用加密前的明文 `chunk.content`。
- `backend/database.py:488` `search_text=lexical_text(f"{row.get('heading') or ''} {row.get('content') or ''}")` —— 使用明文。
- `embedding=list(vector)` / `list(row["embedding"])` —— 向量基于写入前明文生成，不动。

### 3.2 读点（解密，密文 → 明文返回给用户 / 导出）
> 说明：任务描述中"`citations()` 约行 1051" 实际对应的是 `document_version_chunks()`。真正的 `citations()`（`backend/database.py:2126`）返回 `source/section/page_number` 等元数据、不含正文，无需解密。下表以实际代码为准。

| # | 文件:行号 | 方法 | 改动 |
|---|-----------|------|------|
| R1 | `backend/database.py:1051`（方法 `document_version_chunks`, 1033） | `"content": chunk.content` | 改为 `self._field_encryptor.decrypt(chunk.content)`（审核预览返回明文） |
| R2 | `backend/database.py:1564`（方法 `lexical_search` PG 分支, 1559） | 原始 SQL 返回 `c.content` | 取回行后对每个 `row["content"]` 调 `decrypt` 再返回 |
| R3 | `backend/database.py:1642`（方法 `_postgres_hybrid_search`, 1599） | 原始 SQL 返回 `e.content` | 取回行后对每个 `row["content"]` 调 `decrypt` 再返回 |
| R4 | `backend/database.py:1742`（方法 `_portable_hybrid_search`, 1668） | `"content": chunk.content` | 改为 `self._field_encryptor.decrypt(chunk.content)`（SQLite/便携路径） |

**扫描结论（全读点）**：正文对外暴露仅经由以上 4 个 DB 读点。HTTP 端点层无直接 `chunk.content` 读取（已用 Grep 全仓核验：`app.py` 仅 `record_citations` 写入引用元数据、`admin_app.py` 仅调用 DB 方法）。因此解密集中在 DB 层即可，端点/导出层（T4）无需改动——见 §6 任务 T4 说明。

### 3.3 非用户面、但需留意的 chunk.content 使用（不改，记入限制）
- `worker/graph.py:94`：将明文 `chunk.content` 写入磁盘 staging JSON（worker 断点续传用）。本特性不加密该文件，属已知明文落盘点 → §8 待明确事项 ①。
- `ingestion/service.py:89`、`:208`：用内存明文 chunk 生成 embedding，发生在入库前，无 DB 读，向量正确；若将来改为"从 DB 读 chunk 重新向量化"会拿到密文 → §8 待明确事项 ②（仅注释提醒，不纳入本期）。

## 4. 迁移设计

文件：`migrations/versions/20260922_0014_document_chunk_encryption.py`
- 头：`revision = "20260922_0014"`、`down_revision = "20260922_0013"`。
- 依赖：仅复用 `backend.crypto.FieldEncryptor` 与 `IT_QUERY_FIELD_KEY` 环境变量；不新增表/索引，`healthcheck` 无需变更。

`upgrade()`（就地回填，幂等）：
1. 从环境读取密钥 `key = os.environ.get("IT_QUERY_FIELD_KEY", "") or None`，构造 `FieldEncryptor(key)`（空密钥走开发弱密钥并告警，与运行时一致）。
2. 分批（每批 1000）`SELECT id, content FROM document_chunks WHERE content NOT LIKE 'enc:v1:%'`，对每行 `enc = encryptor.encrypt(content)`（空值原样跳过），`UPDATE document_chunks SET content = :enc WHERE id = :id`。
3. `WHERE content NOT LIKE 'enc:v1:%'` 保证已加密行不重复加密 → 幂等、可重复运行。
- SQLite / PostgreSQL 均适用（Python 层加密 + 参数化 UPDATE），PG 用例由 CI 验证。

`downgrade()`（解密回明文，最佳努力）：
- 分批 `SELECT id, content FROM document_chunks WHERE content LIKE 'enc:v1:%'`，对每行 `dec = encryptor.decrypt(content)`（无前缀明文原样返回），`UPDATE ... SET content = :dec WHERE id = :id`。
- **注释明确**：生产环境回滚不推荐——解密需与加密相同的 `IT_QUERY_FIELD_KEY`；若密钥曾轮换则旧数据无法解密；建议生产仅向前（`upgrade`）滚动。如团队倾向更保守，可改为 `pass` 空实现并注释"保留密文、不回滚"。

## 5. 关键接口 / 方法签名

- `QueryDatabase` **无需**新增字段加密辅助方法：`__init__` 已有 `self._field_encryptor`（`backend/database.py:89`）。写点直接 `self._field_encryptor.encrypt(...)`，读点直接 `self._field_encryptor.decrypt(...)`，与 `record()` / `history()` 完全一致（零新增 API 面）。
- 可选（非必须）：为统一三个读点的旧明文回退语义，可加私有方法 `def _decrypt_chunk_content(self, value: str) -> str: return self._field_encryptor.decrypt(value)`；但内联调用已足够，建议保持与 `query.question` 同构（内联）。
- 读点解密位置：
  - `document_version_chunks`（R1）：构造返回 dict 时 `self._field_encryptor.decrypt(chunk.content)`。
  - `lexical_search` PG 分支（R2）：`[dict(row) for row in ...]` 之后，遍历把 `row["content"]` 解密。
  - `_postgres_hybrid_search`（R3）：`[dict(row) for row in rows]` 之后，遍历把 `row["content"]` 解密。
  - `_portable_hybrid_search`（R4）：构造 results 元素时 `self._field_encryptor.decrypt(chunk.content)`。
- `record_citations` / 引用流程不受影响：引用存 `source/section`，不含正文（`app.py:360`）。

## 6. 有序任务列表

> 顺序：迁移与代码同 PR 发布。T1 先行建立回填能力，T2/T3 改 DB 读写，T4 复核端点层（预期无需改动），T5 补测试，T6 同步文档。

### T1：建迁移 + 存量回填
- **文件**：`migrations/versions/20260922_0014_document_chunk_encryption.py`（新建）。
- **做什么**：按 §4 实现 `upgrade()`（分批幂等加密回填）、`downgrade()`（解密回明文 + 注释）。
- **验收**：`alembic upgrade head` 后存量行全部带 `enc:v1:` 前缀；重复运行幂等；`alembic downgrade -1` 可回明文（在测试库验证）。

### T2：DB 写点加密
- **文件**：`backend/database.py`（W1 ~451、W2 ~487）。
- **做什么**：两处 `content=` 改为 `self._field_encryptor.encrypt(...)`；`search_text` 与 `embedding` 保持明文/原样。
- **验收**：新写入的 `document_chunks.content` 入库为 `enc:v1:` 密文；`search_text`、`embedding` 不变；词法/语义检索召回与加密前一致（由既有检索测试覆盖）。

### T3：DB 读点解密 + 全读点扫描复核
- **文件**：`backend/database.py`（R1 ~1051、R2 ~1564、R3 ~1642、R4 ~1742）。
- **做什么**：四个读点返回前 `decrypt` `content`；并复核 §3.3 的 staging/embedding 非用户面读取，确认无需改动。
- **验收**：`document_version_chunks`、PG/便携 hybrid、lexical 返回给前端的 `content` 为明文；旧明文行（无前缀）原样返回；全仓 Grep 复核无遗漏读点。

### T4：端点 / 导出补解密复核
- **文件**：`app.py`、`admin_app.py`（复核，预期无改动）。
- **做什么**：确认所有对外端点/导出经 DB 方法取得已解密明文；审计导出（citations/audit，`utf-8-sig` 沿用）不含 chunk 正文。
- **验收**：管理端文档预览、检索接口、审计导出返回/导出的正文为明文且正确；无明文/密文泄漏。

### T5：测试
- **文件**：`tests/test_chunk_content_encryption.py`（新建）。
- **做什么**：对齐 `tests/test_field_encryption.py` 范式——加密往返、空值、旧明文回退、错误密钥不泄露、默认开发密钥、迁移回填幂等；跑全量 `pytest`（PG 用例自动跳过）。
- **验收**：新增测试全绿；既有 `test_document_ingestion_retrieval` / 检索 / 审核预览测试仍通过（说明解密无回归）。

### T6：文档同步
- **文件**：`docs/architecture-body-redaction.md`（本文件）；必要时更新 `README`/运维手册的"生产必配 `IT_QUERY_FIELD_KEY`"说明、迁移记录。
- **做什么**：落盘本设计；在运维文档注明"正文 `content` 现以 `IT_QUERY_FIELD_KEY` 加密存储，密钥缺失将用开发弱密钥并告警"。
- **验收**：文档与代码一致；密钥配置要求在运维文档显式出现。

## 7. 共享约定（沿用既有）
- 加密前缀 `enc:v1:`；无前缀值视为旧明文、原样返回（向后兼容）。
- 生产环境强制配置 `IT_QUERY_FIELD_KEY`（`config.py` 已校验）；缺失时 `FieldEncryptor` 用固定开发弱密钥并告警，仅用于本地/测试。
- 导出 CSV 沿用 `utf-8-sig`（含 BOM），与既有审计导出一致。
- 密钥从 `IT_QUERY_FIELD_KEY` 读取，排除出 `public()`/`repr`，不落日志。
- 提交信息单行祈使 ≤56 字符；改动后本地跑全量 `pytest`；无法实测 PG 的能力标注"由 CI 验证"。

## 8. 待明确事项
1. **staging 明文落盘**：`worker/graph.py:94` 将明文 `content` 写入磁盘 staging JSON（用于断点续传）。本期不加密（属临时产物，随任务完成清理），但属已知明文落盘点；是否纳入后续加固待定。
2. **重新向量化风险**：`ingestion/service.py:89/208` 用内存明文 chunk 生成 embedding，正确；若未来改为"从 DB 读 chunk 重算 embedding"将拿到密文。仅注释提醒，不纳入本期。
3. **迁移与代码部署顺序**：迁移加密存量行后，若旧版代码（未部署 T2/T3）仍在运行会读到密文显示异常；需迁移与代码同 PR/同批次发布。
4. **downgrade 保守策略**：若团队偏好"生产不回滚"，可令 `downgrade()` 为 `pass`（保留密文），按 §4 注释执行。
