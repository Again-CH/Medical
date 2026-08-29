# 数据合规深化：PHI 静态加密 · 留存最小化 · 删除权

本文件说明医疗 Agent 项目（`/Users/mac/Documents/Medical-care`）在**患者健康信息（PHI）**
合规上的三项核心能力，对应资深岗面试常问的「你们怎么保护患者数据 / 患者能不能要求删数据」。

> 设计总原则：**对象级授权（OLP）决定「谁能读」，本文件的能力决定「落盘后是什么形态、留多久、能不能抹掉」**。
> 两者正交、互补——OLP 防越权访问，加密/留存/删除权防「数据落地后的泄露与无限留存」。

---

## 1. PHI 静态加密（列级透明加密）

### 1.1 加密了哪些列（PHI 清单）

| 表 | 列 | 类别 | 为何加密 |
|----|----|------|----------|
| `users`（主库） | `phone`, `full_name` | 直接标识符（PII） | 手机号/姓名是最直接的个人身份信息 |
| `chat_logs`（主库） | `input_text`, `output_text` | 对话 PHI | 患者主诉与系统回复均可能含健康信息 |
| `approvals`（主库） | `payload` | 敏感动作参数 | 锁号/结算等动作参数可能含患者上下文 |
| `exam_steps`（主库） | `note` | 医生备注 | 可能含诊断/注意事项 |
| `conversation_memory`（每患者私有库） | `value` | 自由文本临床笔记 | 随访笔记、病例小结 |
| `lab_reports`（每患者私有库） | `result` | 检验数值 | 患者健康数据 |
| `vital_signs`（每患者私有库） | `value` | 生命体征 | 患者健康数据 |
| `reminders`（每患者私有库） | `content` | 提醒内容 | 可能含健康信息 |
| `emergency_events`（每患者私有库） | `content` | 紧急事件 | 敏感 |

**刻意不加密的列**：`username`（登录标识 / 物理隔离分区键）、`patient_id`（外键定位键）、
知识库语料（`knowledge_documents.content`，属企业资产非患者数据、需明文检索）。
标识符用于定位患者，但其**真实姓名/手机号**等已加密；生产应对 `username` 与院内
MRN（病案号）做外部映射，使库内仅有伪名分区键。

### 1.2 实现方式：应用层透明列加密

`src/phi.py` 提供 SQLAlchemy `TypeDecorator`（`EncryptedText`），业务代码**零改动**：

- 写库：`process_bind_param` 调 `encrypt_field` 加密；
- 读库：`process_result_value` 调 `decrypt_field` 解密；
- 底层仍是 `TEXT`，**无需改 schema / 迁移**（仅 `users.phone/full_name` 因原是定长
  `VARCHAR` 改为 `TEXT`，已用迁移 `g7b8c9d0e1f2` 处理）。

### 1.3 加密后端（可插拔，按可用性自动选择）

| 后端 | 算法 | 何时启用 |
|------|------|----------|
| `FernetBackend` | AES-128-CBC + HMAC-SHA256（`cryptography.Fernet`） | 安装了 `cryptography` 时（**生产首选**，硬件加速） |
| `StdlibBackend` | HMAC-SHA256 计数器流密码 + Encrypt-then-MAC | 未安装 `cryptography` 的离线/受限环境（零依赖降级） |

两种后端产出带方案前缀的令牌（`enc:v1:` / `enc:f1:`），`decrypt_field` 按前缀分发，
**存量明文行向后兼容**（无前缀按明文返回）。密钥来自 `PHI_ENCRYPTION_KEY`（任意长度
秘密串，统一派生 32 字节）；`PHI_ENCRYPTION_ENABLED=1` 且密钥在场才加密新写入，
**密钥缺失即 fail-closed 拒绝启动**。

### 1.4 启用步骤（生产）

```bash
# 1) 生成主密钥（一次性）
python -c "from src.phi import generate_secret; print(generate_secret())"
# 2) 写入 .env（已被 .gitignore 排除，切勿提交）
PHI_ENCRYPTION_ENABLED=1
PHI_ENCRYPTION_KEY=<上一步输出的密钥>
# 3) 对存量明文数据一次性补加密（幂等，已加密行跳过）
PHI_ENCRYPTION_ENABLED=1 PHI_ENCRYPTION_KEY=<key> python scripts/encrypt_existing_phi.py
```

---

## 2. 留存最小化（retention / data minimization）

易变的**对话类 PHI** 设留存上限（默认 `PHI_RETENTION_DAYS=365`），超期即处理：

- `chat_logs` 超期 → **脱敏**而非删除：把 `input_text/output_text` 置为
  `[redacted by retention policy]`，保留「对话次数 / 耗时」等运营与合规指标；
- `conversation_memory` / `emergency_events`（每患者私有库）超期 → **直接删除行**。

**临床记录**（`lab_reports` / `vital_signs` / `appointments` / `exam_steps`）按更长法定
留存期（默认 `PHI_CLINICAL_RETENTION_DAYS=2555`，约 7 年）保留，**不参与自动清理**，
仅随「删除权」整体抹除——符合医疗记录法定留存要求。

运维入口：
- 端点：`POST /api/admin/retention?dry_run=1`（需 `X-Admin-Key`）
- 脚本：`python scripts/retention.py retention --dry-run --days 180`

---

## 3. 删除权（right-to-erasure / 被遗忘权）

患者或管理员可触发**整体抹除**，对应 GDPR 第 17 条 / 《个人信息保护法》第 47 条。

抹除范围（`src/retention.erase_patient`）：
1. 删除该患者的独立 SQLite 私有库文件（`data/<username>.db`）；
2. 主库删除一切可定位记录：账号、刷新令牌、预约、检查单、审批、对话日志、知情同意；
3. 对含该标识符的**历史审计日志做盐哈希假名化**（用不可逆令牌替换明文用户名），
   保留可追溯性但不留存直接标识符；
4. 写入一条「删除权执行」审计记录（同样只存假名令牌），证明「删除发生在何时、由谁请求」。

运维入口：
- 患者自助：`DELETE /api/patient/me`（JWT 鉴权，删除本人数据）；
- 管理员：`POST /api/admin/erase`（需 `X-Admin-Key`，`{"username","confirm":true}`）；
- 脚本：`python scripts/retention.py erase --username alice --confirm`。

---

## 4. 合规映射（面试话术素材）

| 法规要求 | 本项目对应能力 |
|----------|----------------|
| 静态加密（rest encryption） | `EncryptedText` 列级透明加密，Fernet/AES-GCM 优先 |
| 数据最小化 / 留存限制 | `apply_retention` 超期清理/脱敏，临床记录按法定留存期 |
| 被遗忘权 / 删除权 | `erase_patient` + `DELETE /api/patient/me` |
| 访问授权（最小权限） | 对象级授权 OLP（`integrations._resolve_patient`）+ RBAC |
| 可审计（audit trail） | `audit_logs` + 删除权执行留痕 + 审计假名化 |
| 密钥管理 | `PHI_ENCRYPTION_KEY` 环境变量 + fail-closed；生产建议接 KMS |

**一句话总结（面试）**：我们做了三层——传输/调用层靠对象级授权防越权；落盘层靠
列级透明加密让 PHI「写即密」且密钥缺失 fail-closed；生命周期层靠留存策略与删除权让
数据「留有时限、患者能要回」，且所有销毁动作留痕但不留存明文标识符。
