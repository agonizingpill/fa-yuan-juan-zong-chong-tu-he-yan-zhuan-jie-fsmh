import logging,time,json,os
from pathlib import Path
from .database import Session
from .models import Triage,Intake
from .triage_service import queue
from .training_service import recheck_snapshot,bundle_from_snapshot
from .algorithm import fit_bundle,apply_bundle
from .constants import TriageState
ARTIFACTS_DIR=os.getenv('ARTIFACTS_DIR','/artifacts')
logging.basicConfig(level=logging.INFO)
def run(db,row):
    if row.status!=TriageState.queued.value:return
    row.status=TriageState.running.value;db.commit()
    try:
        case=db.get(Intake,row.intake_id)
        if case.facts_version!=row.facts_version:raise ValueError('事实版本已更新，请重新提交')
        from .intake_service import authorized
        if not authorized(db,case.id):raise ValueError('来访授权已撤回，分流作废')
        ok,reason=recheck_snapshot(db,row)
        if not ok:raise ValueError('训练样本已变更，本建议作废：'+reason)
        samples=bundle_from_snapshot(db,row)
        if len(samples)<2 or len({t for _,t in samples})<2:raise ValueError('合资格样本不足两个主题，无法训练')
        pipeline,metrics=fit_bundle(samples)
        label,confidence,manual=apply_bundle(pipeline,case.facts)
        # 训练期间授权/样本可能已被撤回：以锁内最新状态为准，失效行不允许被结果覆盖
        db.refresh(row)
        if row.status!=TriageState.running.value:
            logging.info('triage %s 训练期间已失效（%s），丢弃本次结果',row.id,row.invalid_reason)
            return
        row.predicted,row.confidence,row.manual_required,row.metrics=label,confidence,manual,metrics
        row.status=TriageState.done.value
        Path(ARTIFACTS_DIR).mkdir(exist_ok=True);Path(f'{ARTIFACTS_DIR}/triage-{row.id}.json').write_text(json.dumps(metrics,ensure_ascii=False))
        db.commit()
    except Exception as e:
        message=str(e)[:200]
        row.status=TriageState.invalid.value if '作废' in str(e) else 'failed'
        row.invalid_reason=message;row.error=message;db.commit()
def main():
  while True:
    Path('/tmp/worker-heartbeat').touch()
    try:
        item=queue.blpop('aid:triage',timeout=2)
        if not item:continue
        with Session() as db:
            row=db.get(Triage,int(item[1]))
            if not row or row.status!=TriageState.queued.value:continue
            run(db,row)
    except Exception:logging.exception('triage loop');time.sleep(1)
if __name__=='__main__':main()
