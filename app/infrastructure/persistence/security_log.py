"""安全事件落库：只写元数据，不写问题/回答正文。

灰度期间由飞书告警脚本轮询 ``user_action_events`` 里 ``security.*`` 前缀的行，
所以这里刻意复用同一张表，不新建表、不改 schema。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from threading import Lock

logger = logging.getLogger(__name__)

_INSERT = (
    "INSERT INTO user_action_events (event_type, action_type, actor_id, space_id, "
    "conversation_id, run_id, document_version_id, payload_json, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)


class SecurityEventLog:
    """把安全信号写进 ``user_action_events``，供告警脚本实时轮询。

    写入失败只记日志：告警链路的故障不允许把业务回答打挂。
    """

    _MAX_DEDUPE = 2000

    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)
        self._seen: set[tuple[str, str]] = set()
        self._lock = Lock()

    def record(self, event_type: str, *, detail: str = "", actor_id: str = "",
               space_id: str = "", conversation_id: str = "", run_id: str = "") -> None:
        if not event_type.startswith("security."):
            raise ValueError("event_type must start with 'security.'")
        # 同一次运行里的同类信号只落一行，避免批量枚举把告警刷成噪音。
        key = (run_id or "-", event_type)
        with self._lock:
            if key in self._seen:
                return
            if len(self._seen) >= self._MAX_DEDUPE:
                self._seen.clear()
            self._seen.add(key)
        try:
            payload = json.dumps({"detail": detail[:200]}, ensure_ascii=False)
            connection = sqlite3.connect(self._db_path, timeout=5)
            try:
                connection.execute(_INSERT, (
                    event_type, event_type.split(".", 1)[-1], actor_id or "-",
                    space_id or None, conversation_id or None, run_id or None,
                    None, payload, time.time(),
                ))
                connection.commit()
            finally:
                connection.close()
        except Exception as error:  # noqa: BLE001 - 告警写入不能打断回答
            logger.warning("security event persist failed: %s", error)
