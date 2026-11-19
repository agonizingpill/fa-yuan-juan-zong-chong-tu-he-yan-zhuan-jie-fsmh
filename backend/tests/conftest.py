import os,pathlib
import pytest
os.environ['DATABASE_URL']='sqlite:///'+str(pathlib.Path('/tmp/aid-test.db').resolve())
os.environ['JWT_SECRET']='test-secret'
os.environ['SEED_DEMO']='false'
os.environ['ARTIFACTS_DIR']='/tmp/aid-artifacts'
from fastapi.testclient import TestClient
import fakeredis
from sqlalchemy import select
from src.database import Base,engine,Session
from src.models import User,Intake,Consent,Party,CaseParty,Triage,TrainingSample
from src.auth import password_hash
from src import triage_service
triage_service.queue=fakeredis.FakeStrictRedis(decode_responses=True)
from src.app import app
from src.database import session as session_dep

@pytest.fixture(scope='session',autouse=True)
def _schema():
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with Session() as db:
        for username,role in [('operator','operator'),('supervisor','supervisor'),('viewer','viewer')]:
            db.add(User(username=username,password=password_hash('Aid@2026'),role=role))
        db.commit()
    yield

@pytest.fixture(autouse=True)
def _clean():
    triage_service.queue.flushall()
    with Session() as db:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name!='users':db.execute(table.delete())
        db.commit()
    yield

@pytest.fixture
def db():
    with Session() as s:yield s

@pytest.fixture
def client():
    def _session():
        with Session() as db:yield db
    app.dependency_overrides[session_dep]=_session
    with TestClient(app) as c:yield c
    app.dependency_overrides.clear()

@pytest.fixture
def tokens(client):
    out={}
    for name in ('operator','supervisor','viewer'):
        r=client.post('/auth/login',json={'username':name,'password':'Aid@2026'})
        out[name]={'Authorization':'Bearer '+r.json()['token']}
    return out

def make_case(db,code,alias,facts,authorized=True,client_name=None,opponent_name=None):
    case=Intake(code=code,alias=alias,facts=facts);db.add(case);db.flush()
    db.add(Consent(intake_id=case.id,authorized=authorized,basis='模拟授权签名存根' if authorized else '未签署授权'))
    client_name=client_name or alias+'-来访方'
    opponent_name=opponent_name or alias+'-相对方'
    for side,name in [('client',client_name),('opponent',opponent_name)]:
        from src.conflict_service import normalize
        party=db.scalar(select(Party).where(Party.normalized==normalize(name)))
        if not party:party=Party(name=name,normalized=normalize(name));db.add(party);db.flush()
        db.add(CaseParty(intake_id=case.id,party_id=party.id,side=side))
    db.commit()
    return case

def make_confirmed_sample(db,case,topic):
    # 模拟此前已完成的主管复核建议，并据登记训练样本
    t=Triage(intake_id=case.id,facts_version=case.facts_version,status='reviewed',predicted=topic,
             confidence=.9,approved=topic,manual_required=False,review_note='主管确认',metrics={'labels':['labor','housing','consumer']})
    db.add(t);db.flush()
    sample=TrainingSample(intake_id=case.id,topic=topic,basis='主管确认卷宗，纳入样本册')
    db.add(sample);db.commit()
    return sample

@pytest.fixture
def sample_pool(db):
    # 三个主题、三个不同来访方的合资格样本池
    data=[('LZ-T1','劳动样本甲','拖欠工资加班工时劳动报酬','labor','甲来访方','甲相对方'),
          ('LZ-T2','住房样本乙','租房到期房东不退押金租赁','housing','乙来访方','乙相对方'),
          ('LZ-T3','消费样本丙','网购商品损坏商家拒绝退款消费','consumer','丙来访方','丙相对方')]
    cases=[];samples=[]
    for code,alias,facts,topic,cn,on in data:
        case=make_case(db,code,alias,facts,client_name=cn,opponent_name=on)
        cases.append(case);samples.append(make_confirmed_sample(db,case,topic))
    return {'cases':cases,'samples':samples}

@pytest.fixture
def target_case(db):
    return make_case(db,'LZ-NEW','新来访丁','单位拖欠加班工资希望分流',client_name='丁来访方',opponent_name='丁相对方')
