"""pytest 共享 fixture：测试用临时 SQLite + Mock seed.

环境变量必须在 ``app.*`` 任何模块被导入前设定，否则 ``app.core.config.Settings``
会缓存默认 ``sqlite:///./bike.db``，导致 engine 指向错误数据库。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator

# ⚠️ 必须在 import app.* 前完成 env 设置
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="bike-test-"))
_TEST_DB_PATH = _TEST_DB_DIR / "bike-test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
os.environ["SEED_MOCK_ON_STARTUP"] = "true"

import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _bootstrap_database() -> Iterator[None]:
    """会话级初始化：建表 + 装载 Mock 数据，所有测试共享。"""
    from app.db.init_db import init_db
    from app.db.session import engine
    from app.mock.seed import seed_mock
    from app.models.station import Station
    from sqlmodel import Session, select

    init_db()
    with Session(engine) as session:
        has_station = session.exec(select(Station).limit(1)).first() is not None
    if not has_station:
        seed_mock()
    yield


@pytest.fixture
def tmp_apikey_file(tmp_path: Path) -> Iterator[Path]:
    """创建一个临时 apikey 文件供 secrets 测试注入."""
    p = tmp_path / "apikey.txt"
    yield p
