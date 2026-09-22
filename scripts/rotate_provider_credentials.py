"""一次性迁移：将 runtime_provider_credentials 中用旧密钥（IT_AUTH_SUBJECT_SALT 派生）加密的行，
用新密钥（IT_PROVIDER_CREDENTIAL_KEY 派生）重新加密。

背景：决策 #1 将单枚 IT_AUTH_SUBJECT_SALT 拆分为三枚独立密钥。凭证加密从
``secret_key = sha256(IT_AUTH_SUBJECT_SALT)`` 改为 ``credential_key = sha256(IT_PROVIDER_CREDENTIAL_KEY)``。
QueryDatabase 的读取路径已向后兼容（先试新密钥、再试旧密钥），因此本脚本是可选的清理步骤；运行它会把
所有存量密文统一重写为新密钥，从而彻底消除对旧密钥的残留依赖。

运行（在已正确配置环境的机器上）：
    python scripts/rotate_provider_credentials.py

前置条件：
    * 必须同时设置 IT_AUTH_SUBJECT_SALT（旧密钥，用于读出现有密文）与
      IT_PROVIDER_CREDENTIAL_KEY（新密钥，用于重写密文）。
    * IT_PROVIDER_CREDENTIAL_KEY 未设置时直接退出（说明凭证仍挂在旧密钥上，无需迁移）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import AppSettings
from backend.database import QueryDatabase


def main() -> int:
    config = AppSettings.from_environment()
    old_key = config.auth_subject_salt.get_secret_value()
    new_key = config.provider_credential_key.get_secret_value()
    if not new_key:
        print("IT_PROVIDER_CREDENTIAL_KEY 未设置：凭证当前仍使用 IT_AUTH_SUBJECT_SALT 派生密钥，无需重加密。")
        return 0
    if not old_key:
        print("IT_AUTH_SUBJECT_SALT 未设置：无法确定旧密钥，中止。")
        return 1

    db = QueryDatabase(
        config.database_url,
        credential_key=new_key,
        legacy_credential_key=old_key,
        query_field_key=config.query_field_key.get_secret_value(),
    )
    creds = db.runtime_provider_credentials()
    if not creds:
        print("无存量供应商凭证需要重加密。")
        return 0
    for provider, api_key in creds.items():
        db.set_runtime_provider_credential(
            provider=provider, api_key=api_key,
            actor_subject_id="key-rotation", request_id="rotate-credentials",
        )
    print(f"已用新密钥（IT_PROVIDER_CREDENTIAL_KEY）重加密 {len(creds)} 条供应商凭证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
