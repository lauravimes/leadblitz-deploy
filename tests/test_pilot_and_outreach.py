from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from bs4 import BeautifulSoup
from app.models import PilotPlan, User, Lead, LeadOutcome, Campaign
from app.services.pilot import COOKIE
from app.services.prospect_brief import prospect_brief


def plan(client):
    client.cookies.clear()
    r=client.get('/client-plan?utm_source=linkedin&utm_campaign=designer_pilot&utm_content=launch')
    assert r.status_code==200
    token=BeautifulSoup(r.text,'html.parser').select_one('input[name=csrf]')['value']
    answers=dict(csrf=token,experience='occasional',offer='redesign',obstacle='message',niche='trades',location='Reading, UK')
    return token,answers


def test_questionnaire_application_and_signup_handoff(client,db,register,monkeypatch):
    calls=[]
    monkeypatch.setattr('app.routers.pilot.notify_application',lambda x:calls.append(x))
    token,answers=plan(client)
    assert client.post('/client-plan/start',data={'csrf':token}).status_code==204
    r=client.post('/client-plan',data=answers)
    assert r.status_code==200 and 'Your first prospecting session' in r.text
    assert r.headers['cache-control']=='no-store'
    assert db.query(PilotPlan).one().source=='linkedin'
    application=dict(csrf=token,name='Pilot Designer',email='pilot@example.com',portfolio='https://example.com',ready='yes',consent='yes')
    r=client.post('/client-plan/apply',data=application)
    assert 'Your application is saved' in r.text
    client.post('/client-plan/apply',data=application)
    assert len(calls)==1
    _,_,uid=register()
    db.expire_all();assert db.query(PilotPlan).one().user_id==uid
    r=client.get('/search')
    assert 'value="plumber"' in r.text and 'value="Reading, UK"' in r.text
    assert client.get('/admin/pilot').status_code==403
    user=db.get(User,uid);user.is_admin=True;db.commit()
    r=client.get('/admin/pilot');assert 'Pilot Designer' in r.text and 'linkedin' in r.text
    assert r.headers['cache-control']=='no-store'


def test_pilot_validation_and_csrf(client,db,monkeypatch):
    monkeypatch.setattr('app.routers.pilot.notify_application',lambda x:None)
    token,answers=plan(client)
    assert client.post('/client-plan',data={**answers,'csrf':'forged'}).status_code==403
    assert client.post('/client-plan',data={**answers,'experience':'forged'}).status_code==422
    assert client.post('/client-plan',data=answers).status_code==200
    r=client.post('/client-plan/apply',data=dict(csrf=token,name='A',email='a@example.com',portfolio='javascript:alert(1)',consent='yes'))
    assert 'full portfolio URL' in r.text
    r=client.post('/client-plan/apply',data=dict(csrf=token,name='A',email='a@example.com',portfolio='http://[',consent='yes'))
    assert 'full portfolio URL' in r.text
    assert db.query(PilotPlan).one().applied_at is None
    client.cookies.clear()
    assert client.post('/client-plan/apply',data=dict(csrf=token,name='A',email='a@example.com',consent='yes')).status_code==403


def test_plan_cannot_be_reassigned_to_second_user(client,db,register):
    _,answers=plan(client);client.post('/client-plan',data=answers)
    _,_,first=register();_,_,second=register()
    db.expire_all();assert db.query(PilotPlan).one().user_id==first
    assert first!=second


def test_outcome_observation_and_value_authorization(client,db,register):
    _,_,uid=register()
    lead=Lead(user_id=uid,name='Example business',website='https://example.com',score=35,score_breakdown={'evidence':{'title':'Example business'},'rendering_limitations':True})
    db.add(lead);db.commit();lid=lead.id
    assert client.patch(f'/api/leads/{lid}/observation',data={'observation':'The quote link returns 404.'}).status_code==422
    assert client.patch(f'/api/leads/{lid}/observation',data={'observation':'The quote link returns 404.','checked':'yes'}).status_code==200
    for stage in ['contacted','replied','meeting','won','won']:
        assert client.patch(f'/api/leads/{lid}/stage',data={'stage':stage},headers={'HX-Target':'stage-confirm'}).status_code==200
    assert db.query(LeadOutcome).count()==4
    assert client.patch(f'/api/leads/{lid}/deal',data={'value':'NaN','currency':'GBP'}).status_code==422
    assert client.patch(f'/api/leads/{lid}/deal',data={'value':'1.001','currency':'GBP'}).status_code==422
    assert client.patch(f'/api/leads/{lid}/deal',data={'value':'1500.50','currency':'GBP'}).status_code==200
    r=client.get('/api/stats');assert '1500.50' in r.text and 'Meeting booked' in r.text
    assert 'Copy email draft' in client.get('/email?lead_id='+lid).text
    assert 'The quote link returns 404.' in client.get('/leads/'+lid).text
    register()
    assert client.patch(f'/api/leads/{lid}/observation',data={'observation':'bad','checked':'yes'}).status_code==404
    assert client.patch(f'/api/leads/{lid}/deal',data={'value':'9'}).status_code==404


def test_evidence_prompt_and_stale_observation(monkeypatch):
    lead=SimpleNamespace(score_breakdown={'evidence':{'title':'Example'},'rendering_limitations':True},website='https://example.com',score=25,review_count=20,email='hello@example.com',phone='',last_scored_at=datetime.now(timezone.utc),verified_issue='Quote link is broken',verified_issue_at=datetime.now(timezone.utc)-timedelta(days=31))
    brief=prospect_brief(lead)
    assert brief['verified_issue']=='' and brief['verification_stale']
    lead.verified_issue_at=datetime.now(timezone.utc)
    brief=prospect_brief(lead)
    from app.services.ai_email import generate_personalized_email
    fake=MagicMock();fake.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"subject":"A quick question","body":"Hi, would you like a suggestion?"}'))])
    monkeypatch.setattr('app.services.ai_email.OpenAI',lambda **kw:fake)
    result=generate_personalized_email({'name':'Example','website':lead.website,'score':25,'brief':brief},'Website design')
    payload=fake.chat.completions.create.call_args.kwargs
    assert 'Quote link is broken' in payload['messages'][1]['content']
    assert 'NOT buying intent' in payload['messages'][0]['content']
    assert result['body'].startswith('Hi,')


def test_pilot_admin_rejects_other_account_and_csrf(client,db,register):
    token,answers=plan(client);client.post('/client-plan',data=answers)
    _,_,uid=register();u=db.get(User,uid);u.is_admin=True;db.commit()
    pid=db.query(PilotPlan).one().id
    assert client.post('/admin/pilot/'+pid,data={'csrf':'bad','status':'invited'}).status_code==403
