import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
_url=os.environ['DATABASE_URL']
_args={'pool_pre_ping':True}
if not _url.startswith('sqlite'):_args['isolation_level']='READ COMMITTED'
engine=create_engine(_url,**_args)
Session=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
def session():
    with Session() as db:
        try:yield db
        except Exception:db.rollback();raise
