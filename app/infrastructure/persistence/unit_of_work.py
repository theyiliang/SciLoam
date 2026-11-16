"""One SQLite connection and serialized transaction per research operation."""
from .database import Database
from .repositories import SQLiteResearchRepository


class SQLiteResearchUnitOfWork:
    def __init__(self, database: Database):
        self._database = database

    def __enter__(self):
        self._transaction = self._database.transaction()
        connection = self._transaction.__enter__()
        self.repository = SQLiteResearchRepository(connection)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._transaction.__exit__(exc_type, exc_value, traceback)
