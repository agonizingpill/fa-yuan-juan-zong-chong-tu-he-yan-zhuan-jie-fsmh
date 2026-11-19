"""种子数据下的端到端冒烟：覆盖隔离、撤回作废与重算全链路。"""
import os,pathlib
os.environ['DATABASE_URL']='sqlite:///'+str(pathlib.Path('/tmp/aid-e2e.db').resolve())
os.environ['JWT_SECRET']='test-secret';os.environ['SEED_DEMO']='false';os.environ['ARTIFACTS_DIR']='/tmp/aid-e2e-artifacts'
import fakeredis
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.database import Base,engine,Session
import src.models
from src import triage_service
triage_service.queue=fakeredis.FakeStrictRedis(decode_responses=True)
from src.app import app
from src.database import session as session_dep
from src.seed import seed
from src.models import Intake,Triage,TrainingSample,TriageSample
from src import worker
Base.metadata.drop_all(engine);Base.metadata.create_all(engine);seed()
def _session():
    with Session() as db:yield db
app.dependency_overrides[session_dep]=_session
c=TestClient(app)
sup={'Authorization':'Bearer '+c.post('/auth/login',json={'username':'supervisor','password':'Aid@2026'}).json()['token']}
op={'Authorization':'Bearer '+c.post('/auth/login',json={'username':'operator','password':'Aid@2026'}).json()['token']}
with Session() as db:
    target=db.scalar(select(Intake).where(Intake.code=='LZ033'))
    labor_sample_case=db.scalar(select(Intake).where(Intake.code=='LZ030'))
# LZ033 入队：样本册含 LZ030/LZ031(labor)、LZ034(housing)、LZ035(consumer)，且与 LZ033 不同来访方
r=c.post('/triages',headers=op,json={'intake_id':target.id});assert r.status_code==200,r.text
tid=r.json()['id']
raw=triage_service.queue.blpop('aid:triage',timeout=1)
with Session() as db:
    row=db.get(Triage,tid);worker.run(db,row);db.commit()
    assert row.status=='done',row.error
    used={l.intake_id for l in db.scalars(select(TriageSample).where(TriageSample.triage_id==tid)).all()}
    assert used=={x.intake_id for x in db.scalars(select(TrainingSample)).all()}
    print('E2E 训练样本数:',row.metrics['train_count'],'预测:',row.predicted,round(row.confidence,3))
# 主管复核确认
r=c.post(f'/triages/{tid}/review',headers=sup,json={'topic':row.predicted,'note':'主管端到端确认'});assert r.status_code==200,r.text
# 撤回劳动样本卷授权：已确认建议失效并自动重算，重算批不含该卷
r=c.post(f'/intakes/{labor_sample_case.id}/consent',headers=op,json={'authorized':False,'basis':'端到端撤回授权说明'});assert r.status_code==200
with Session() as db:
    old=db.get(Triage,tid);assert old.status=='invalid' and old.invalid_reason
    new=db.scalar(select(Triage).where(Triage.recomputed_from==tid))
    assert new is not None and new.status=='queued'
    used_new={l.intake_id for l in db.scalars(select(TriageSample).where(TriageSample.triage_id==new.id,TriageSample.excluded==False))}
    assert labor_sample_case.id not in used_new and len(used_new)==3
    revoked=db.get(TrainingSample,db.scalar(select(TrainingSample).where(TrainingSample.intake_id==labor_sample_case.id)).id)
    assert revoked.status=='revoked'
raw=triage_service.queue.blpop('aid:triage',timeout=1);assert raw
with Session() as db:
    row=db.get(Triage,new.id);worker.run(db,row);db.commit()
    assert row.status=='done',row.error
    print('E2E 重算完成:',row.predicted,round(row.confidence,3),'train_count',row.metrics['train_count'])
# 留痕：保留卷宗编号、不显示撤回卷原话、给出重算链
trace=c.get(f'/triages/{new.id}/trace',headers=sup).json()
assert trace['recomputed_from']['triage_id']==tid
codes={s['intake_code'] for s in trace['samples']}
assert 'LZ030' in codes
with Session() as db:
    facts=db.get(Intake,labor_sample_case.id).facts
assert facts not in str(trace)
print('E2E 留痕样本:',[(s['intake_code'],s['excluded'],s['exclude_reason'] or '采用') for s in trace['samples']])
print('E2E OK')
