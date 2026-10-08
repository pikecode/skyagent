"""Fail-closed policy after restoring snapshots with incomplete write history."""

RESTORE_BLOCK_KEY = "third_party_share/restore_block/v1"


def share_writes_blocked(database):
    # Presence, rather than a parsed/truthy value, is intentional: a corrupted
    # or blank marker must never turn a blocked database into an allowed one.
    marker = database.connection.execute(
        "SELECT 1 FROM settings WHERE name=?", (RESTORE_BLOCK_KEY,)
    ).fetchone()
    return marker is not None


def require_share_writes_allowed(database):
    if share_writes_blocked(database):
        raise ValueError(
            "本数据库已恢复备份，分享提交历史可能不完整；真实分享已阻断，需人工对账。"
            "当前没有解除入口，请勿通过再次恢复或修改记录绕过。"
        )
