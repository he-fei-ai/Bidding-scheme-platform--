# -*- coding: utf-8 -*-
"""最小复现：test_builtin_has_required_fields 失败现场的链内容打印"""

from app.db import get_db, init_db

init_db()


def test_repro_builtin(cleanup_backup):
    from app.services.ai.ai_provider_service import (
        clear_provider_history,
        get_provider_priority_order,
    )

    clear_provider_history()
    chain = get_provider_priority_order()
    dump = [
        {
            "name": e["name"],
            "builtin": e.get("is_builtin_default"),
            "primary": e.get("is_primary"),
            "url": e["base_url"],
        }
        for e in chain
    ]
    print("\nCHAIN:", dump)
    builtin = next((e for e in chain if e["name"] == "DeepSeek"), None)
    assert builtin is not None
    assert builtin["is_builtin_default"] is False, f"现场: {dump}"


import pytest  # noqa: E402


@pytest.fixture
def cleanup_backup():
    with get_db() as conn:
        cfg = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        hist = [tuple(r) for r in conn.execute("SELECT * FROM provider_history").fetchall()]
        cols = [d[0] for d in conn.execute("SELECT * FROM provider_history LIMIT 0").description]
    yield
    with get_db() as conn:
        if cfg:
            conn.execute("REPLACE INTO ai_config VALUES (%s)" % ",".join("?" * len(cfg.keys())), tuple(cfg))
        conn.execute("DELETE FROM provider_history")
        for row in hist:
            conn.execute(
                "INSERT INTO provider_history (%s) VALUES (%s)"
                % (",".join(cols), ",".join("?" * len(cols))),
                row,
            )
        conn.commit()
