"""
Test infrastructure: isolated DB per test via transaction rollback.
Each test gets a clean session; all writes are rolled back on exit.
"""
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from app.database import Base
from app.main import app
from app.database import get_db

# pearl_test — отдельная БД (создать: ALTER USER pearl CREATEDB; CREATE DATABASE pearl_test OWNER pearl;)
# Пока используем основную pearl — каждый тест откатывает транзакцию, данные не сохраняются
TEST_DB_URL = "postgresql://pearl:pearl_dev@localhost:5432/pearl"

engine = create_engine(TEST_DB_URL)
TestingSession = sessionmaker(bind=engine)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test as hitting real external APIs (OpenRouter etc.)")


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    Base.metadata.create_all(bind=engine)
    yield
    # Не дропаем — удобно смотреть данные после прогона


@pytest.fixture()
def db():
    """Session with automatic rollback after each test.

    Wraps the test in an outer transaction + a SAVEPOINT that's restarted
    after every session.commit() — so code under test (e.g. route handlers)
    can call db.commit() freely without ending the outer transaction early
    and leaking rows into the dev DB. Standard SQLAlchemy test recipe.
    """
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = TestingSession(bind=connection)
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, trans):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    yield session
    session.close()
    outer_transaction.rollback()
    connection.close()


@pytest.fixture()
def client(db):
    """TestClient wired to the test DB session."""
    def override_get_db():
        yield db
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()
