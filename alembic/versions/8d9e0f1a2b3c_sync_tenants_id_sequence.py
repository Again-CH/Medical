"""同步 tenants 主键序列（修复全新库首插租户必失败的 bug）

Revision ID: 8d9e0f1a2b3c
Revises: 76e2eddf3450
Create Date: 2026-08-30 13:50:00.000000

背景（真实事故，不是理论问题）
------------------------------
迁移 ``0b8ce330fa1c`` 为了给存量数据兜底，用**裸 SQL 显式指定 id** 插入了默认租户::

    INSERT INTO tenants (id, code, name, is_default) VALUES (1, 'DEFAULT', ...)

Postgres 的 ``SERIAL`` 底层是序列（``tenants_id_seq``）。**显式给 id 赋值不会推进序列**，
于是在一个全新库上：

1. 迁移插入默认租户 ``id=1``，序列仍停在 1（未使用）；
2. 应用或测试用 ORM 插入第二个租户 → 序列吐出 ``id=1`` →
   ``UniqueViolation: duplicate key value violates unique constraint "tenants_pkey"``；
3. 再插一次才拿到 2，肉眼看起来像"偶发"，实际是**必现**。

为什么 `alembic check` 查不出来
-------------------------------
它只比对「ORM 模型 vs 迁移脚本」的 schema 差异，**序列的当前值属于数据状态，
不在 schema 比对范围内**。同理，SQLite 也不复现（SQLite 的 rowid 分配策略不同），
所以这个 bug 只在全新 Postgres 库上暴露——也就是「新环境部署 / 灾备恢复建库」时。

修法
----
不改写已应用的旧迁移（生产库已跑过，改了历史迁移不生效且危险），
而是加一条**后续迁移**把序列对齐到 ``MAX(id)``。对存量库是幂等的无害操作。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d9e0f1a2b3c"
down_revision: Union[str, None] = "76e2eddf3450"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / 其他方言没有 SERIAL 序列，无需处理
        return
    op.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('tenants', 'id'),
            COALESCE((SELECT MAX(id) FROM tenants), 0) + 1,
            false
        )
        """
    )


def downgrade() -> None:
    # 序列回退没有实际意义（回退后仍会自增长到正确值），留空即可。
    pass
