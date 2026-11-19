import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.database import Session
from src.models import Intake,Consent,TrainingSample,Triage,TriageSample,CaseParty
from src import triage_service
from src import worker
from src.training_service import eligible_samples,recheck_snapshot
from src.constants import TriageState,SampleState

def q():return triage_service.queue

def enqueue_triage(client,tokens,intake_id):
    r=client.post('/triages',headers=tokens['operator'],json={'intake_id':intake_id})
    assert r.status_code==200,r.text
    return r.json()

def pop_and_run():
    raw=q().blpop('aid:triage',timeout=1)
    assert raw,'队列中没有任务'
    with Session() as db:
        row=db.get(Triage,int(raw[1]))
        worker.run(db,row);db.commit()
        return row.id

def get_row(triage_id):
    with Session() as db:return db.get(Triage,triage_id)

# 1. 样本登记：只有主管、需授权、需已有确认建议且主题一致
def test_register_sample_rules(client,tokens,target_case):
    body={'intake_id':target_case.id,'topic':'labor','basis':'主管确认样本'}
    r=client.post('/training-samples',headers=tokens['operator'],json=body)
    assert r.status_code==403
    r=client.post('/training-samples',headers=tokens['viewer'],json=body)
    assert r.status_code==403
    r=client.post('/training-samples',headers=tokens['supervisor'],json=body)
    assert r.status_code==409 and '主管确认的主题建议' in r.json()['detail']

# 2. 合资格样本口径：授权有效 + 样本有效 + 排除同来访方
def test_eligible_excludes_same_client_and_unauthorized(db,sample_pool,target_case):
    eligible=eligible_samples(db,target_case.id)
    assert {s.id for s in eligible}=={s.id for s in sample_pool['samples']}
    # 目标卷与 LZ-T1 归并到同一来访方后，该样本须隔离
    shared=db.scalar(select(CaseParty).where(CaseParty.intake_id==sample_pool['cases'][0].id,CaseParty.side=='client'))
    target_client=db.scalar(select(CaseParty).where(CaseParty.intake_id==target_case.id,CaseParty.side=='client'))
    target_client.party_id=shared.party_id;db.commit()
    eligible=eligible_samples(db,target_case.id)
    assert sample_pool['samples'][0].id not in {s.id for s in eligible}
    # 样本卷授权撤回后不再合资格
    consent=db.scalar(select(Consent).where(Consent.intake_id==sample_pool['cases'][1].id))
    consent.authorized=False;db.commit()
    eligible=eligible_samples(db,target_case.id)
    assert {s.id for s in eligible}=={sample_pool['samples'][2].id}

# 3. 样本不足两个主题时，分流拒绝并提示
def test_enqueue_requires_enough_scope(db,client,tokens,target_case):
    # 新建仅单一主题样本的环境（另一卷，与目标不同来访方）
    from tests.conftest import make_case,make_confirmed_sample
    c1=make_case(db,'LZ-S1','单主题甲','工资劳动报酬',client_name='甲单来访',opponent_name='甲单相对')
    make_confirmed_sample(db,c1,'labor')
    r=client.post('/triages',headers=tokens['operator'],json={'intake_id':target_case.id})
    assert r.status_code==409 and '合资格训练样本不足' in r.json()['detail']
    c2=make_case(db,'LZ-S2','单主题乙','押金租赁住房',client_name='乙单来访',opponent_name='乙单相对')
    make_confirmed_sample(db,c2,'housing')
    r=client.post('/triages',headers=tokens['operator'],json={'intake_id':target_case.id})
    assert r.status_code==200

# 4. 入队即定样本，快照不随后续撤回改变
def test_snapshot_fixed_at_enqueue(db,client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id)
    with Session() as d:
        links=d.scalars(select(TriageSample).where(TriageSample.triage_id==row['id'])).all()
        assert len(links)==3
        assert {l.intake_id for l in links}=={c.id for c in sample_pool['cases']}
        assert all(l.excluded is False for l in links)

# 5. 排队/运行中的任务在样本撤回后作废，不能再用该样本（worker 出队即作废）
def test_queued_task_invalidated_on_revocation(client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id)
    r=client.post(f"/intakes/{sample_pool['cases'][0].id}/consent",headers=tokens['operator'],
                  json={'authorized':False,'basis':'来访人撤回授权'})
    assert r.status_code==200
    pop_and_run()
    updated=get_row(row['id'])
    assert updated.status==TriageState.invalid.value
    assert '作废' in updated.invalid_reason
    with Session() as d:
        links=d.scalars(select(TriageSample).where(TriageSample.triage_id==row['id'])).all()
        assert next(l for l in links if l.intake_id==sample_pool['cases'][0].id).excluded is True

# 6. 撤回与训练并发：已算完待复核的旧建议不能送审（接口拒绝 + worker 复核逻辑）
def test_done_suggestion_blocked_from_review_after_revoke(client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id)
    pop_and_run()
    assert get_row(row['id']).status==TriageState.done.value
    client.post(f"/intakes/{sample_pool['cases'][1].id}/consent",headers=tokens['operator'],
                json={'authorized':False,'basis':'撤回授权'})
    r=client.post(f"/triages/{row['id']}/review",headers=tokens['supervisor'],
                  json={'topic':'labor','note':'主管复核说明'})
    assert r.status_code==409 and '作废' in r.json()['detail']
    assert get_row(row['id']).status==TriageState.invalid.value

# 7. 已在待复核的旧建议失效：状态转 invalid，且只用原批其余合资格样本自动重算
def test_reviewed_invalidated_and_recomputed_with_remaining(db,client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id);pop_and_run()
    r=client.post(f"/triages/{row['id']}/review",headers=tokens['supervisor'],
                  json={'topic':'labor','note':'主管确认'})
    assert r.status_code==200 and r.json()['status']=='reviewed'
    client.post(f"/intakes/{sample_pool['cases'][2].id}/consent",headers=tokens['operator'],
                json={'authorized':False,'basis':'消费样本卷撤回'})
    old=get_row(row['id'])
    assert old.status==TriageState.invalid.value
    with Session() as d:
        new=d.scalar(select(Triage).where(Triage.recomputed_from==old.id))
        assert new is not None and new.status==TriageState.queued.value
        links=d.scalars(select(TriageSample).where(TriageSample.triage_id==new.id)).all()
        used={l.intake_id for l in links if not l.excluded}
        excluded={l.intake_id for l in links if l.excluded}
        assert sample_pool['cases'][2].id not in used
        assert sample_pool['cases'][2].id in excluded
        assert len(used)==2 and len(links)==3
    # 重算任务已自动入队并可正常完成
    pop_and_run()
    assert get_row(new.id).status==TriageState.done.value

# 8. 重算若剩余样本不足两个主题，不自动重算，旧建议标注原因
def test_no_auto_recompute_when_scope_insufficient(db,client,tokens,target_case):
    from tests.conftest import make_case,make_confirmed_sample
    c1=make_case(db,'LZ-R1','重算甲','工资劳动报酬',client_name='甲重来访',opponent_name='甲重相对')
    c2=make_case(db,'LZ-R2','重算乙','押金租赁住房',client_name='乙重来访',opponent_name='乙重相对')
    make_confirmed_sample(db,c1,'labor');make_confirmed_sample(db,c2,'housing')
    row=enqueue_triage(client,tokens,target_case.id);pop_and_run()
    rr=client.post(f"/triages/{row['id']}/review",headers=tokens['supervisor'],json={'topic':'labor','note':'主管确认'})
    assert rr.status_code==200
    client.post(f"/intakes/{c2.id}/consent",headers=tokens['operator'],json={'authorized':False,'basis':'撤回授权说明'})
    old=get_row(row['id'])
    assert old.status==TriageState.invalid.value and '不足' in old.invalid_reason
    with Session() as d:
        assert d.scalar(select(Triage).where(Triage.recomputed_from==old.id)) is None

# 9. 已确认建议保留样本编号；撤回卷原话不再显示（留痕不含事实原文）
def test_trace_keeps_codes_but_hides_facts(db,client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id);pop_and_run()
    client.post(f"/intakes/{sample_pool['cases'][0].id}/consent",headers=tokens['operator'],
                json={'authorized':False,'basis':'撤回授权不再使用'})
    r=client.get(f"/triages/{row['id']}/trace",headers=tokens['viewer'])
    assert r.status_code==200
    data=r.json()
    codes={s['intake_code'] for s in data['samples']}
    assert codes=={c.code for c in sample_pool['cases']}  # 样本编号保留
    revoked=[s for s in data['samples'] if s['current_status']==SampleState.revoked.value]
    assert len(revoked)==1 and revoked[0]['excluded'] is True
    blob=str(data)
    assert sample_pool['cases'][0].facts not in blob  # 撤回卷原话不显示
    assert '拖欠工资加班工时劳动报酬' not in blob

# 10. 留痕可查实际用过哪些样本、为何重算（含父子链）
def test_trace_explains_recompute_chain(db,client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id);pop_and_run()
    rr=client.post(f"/triages/{row['id']}/review",headers=tokens['supervisor'],json={'topic':'labor','note':'主管确认'})
    assert rr.status_code==200
    client.post(f"/intakes/{sample_pool['cases'][1].id}/consent",headers=tokens['operator'],
                json={'authorized':False,'basis':'住房样本撤回说明'})
    with Session() as d:new_id=d.scalar(select(Triage).where(Triage.recomputed_from==row['id'])).id
    data=client.get(f"/triages/{new_id}/trace",headers=tokens['supervisor']).json()
    assert data['recomputed_from']['triage_id']==row['id']
    assert '作废' in data['recomputed_from']['invalid_reason'] or '撤回' in data['recomputed_from']['invalid_reason']
    used={s['intake_code'] for s in data['samples'] if not s['excluded']}
    assert sample_pool['cases'][1].code not in used
    parent=client.get(f"/triages/{row['id']}/trace",headers=tokens['supervisor']).json()
    assert parent['recomputed_to'][0]['triage_id']==new_id

# 11. worker 正常路径：真实按快照样本训练，写指标与产物
def test_worker_trains_on_snapshot(client,tokens,sample_pool,target_case):
    row=enqueue_triage(client,tokens,target_case.id)
    tid=pop_and_run()
    updated=get_row(tid)
    assert updated.status==TriageState.done.value
    assert updated.predicted in ('labor','housing','consumer')
    assert updated.metrics['train_count']==3
    assert updated.metrics['label_counts']['labor']==1
    assert updated.metrics['dataset_sha256']

# 12. 未授权目标卷不能入队；事实版本不符任务失败
def test_enqueue_guard_and_stale_version(db,client,tokens,sample_pool):
    from tests.conftest import make_case
    c=make_case(db,'LZ-U','未授权卷','工资劳动报酬纠纷',authorized=False,client_name='未授来访',opponent_name='未授相对')
    r=client.post('/triages',headers=tokens['operator'],json={'intake_id':c.id})
    assert r.status_code==409 and '授权' in r.json()['detail']

# 13. 重新登记已撤回样本：同卷可再次纳入，授权与确认建议须仍有效
def test_reregister_revoked_sample(db,client,tokens,sample_pool,target_case):
    case=sample_pool['cases'][0]
    client.post(f"/intakes/{case.id}/consent",headers=tokens['operator'],json={'authorized':False,'basis':'撤回授权说明'})
    with Session() as d:
        assert d.get(TrainingSample,sample_pool['samples'][0].id).status==SampleState.revoked.value
    # 未重新授权前不能登记
    r=client.post('/training-samples',headers=tokens['supervisor'],
                  json={'intake_id':case.id,'topic':'labor','basis':'重新纳入'})
    assert r.status_code==409 and '授权' in r.json()['detail']
    client.post(f"/intakes/{case.id}/consent",headers=tokens['operator'],
                json={'authorized':True,'basis':'重新取得授权签名'})
    r=client.post('/training-samples',headers=tokens['supervisor'],
                  json={'intake_id':case.id,'topic':'labor','basis':'重新纳入样本册'})
    assert r.status_code==200 and r.json()['status']==SampleState.active.value

# 14. 训练进行中样本被撤回：worker 不得用算完的结果覆盖失效状态（并发守卫）
def test_worker_discards_result_after_mid_training_revoke(db,client,tokens,sample_pool,target_case,monkeypatch):
    from src import algorithm
    row=enqueue_triage(client,tokens,target_case.id)
    # 模拟训练耗时窗口内发生撤回：进入 fit_bundle 前把行置为 running，撤回使其 invalid
    original_fit=algorithm.fit_bundle
    class Marker:fired=False
    def racing_fit(samples):
        if not Marker.fired:
            Marker.fired=True
            client.post(f"/intakes/{sample_pool['cases'][0].id}/consent",headers=tokens['operator'],
                        json={'authorized':False,'basis':'训练中途撤回授权说明'})
        return original_fit(samples)
    monkeypatch.setattr(worker,'fit_bundle',racing_fit)
    raw=q().blpop('aid:triage',timeout=1)
    with Session() as d:
        t=d.get(Triage,int(raw[1]))
        worker.run(d,t);d.commit()
    updated=get_row(row['id'])
    assert updated.status==TriageState.invalid.value
    assert updated.predicted is None
