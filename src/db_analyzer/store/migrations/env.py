from alembic import context
from sqlalchemy import create_engine

from db_analyzer.store.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url")
assert url is not None

with create_engine(url).connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
