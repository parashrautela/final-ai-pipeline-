"""Real PostgreSQL + HTTP tests for parallel quotations, RLS, retries and award races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import io
from pathlib import Path
from unittest.mock import patch
import uuid
import httpx
import psycopg
from PIL import Image
from test_manufacturing_api import setup_test_db, bootstrap_db, PostgresClientAdapter
from app.routers import manufacturing as api
from app.main import app
ROOT=Path(__file__).resolve().parents[1]

async def run():
    server, uri, _ = setup_test_db()
    try:
        bootstrap_db(uri)
        with psycopg.connect(uri,autocommit=True) as c:
            c.execute((ROOT/'migrations/021_parallel_manufacturing_quotes.sql').read_text())
            c.execute((ROOT/'migrations/021_parallel_manufacturing_quotes.sql').read_text()) # rerunnable
            c.execute((ROOT/'migrations/022_simplified_manufacturing_requirements.sql').read_text())
            c.execute((ROOT/'migrations/022_simplified_manufacturing_requirements.sql').read_text())
            # Model Supabase's existing default authenticated read grants for the RLS test.
            # The feature migration does not introduce any table privileges.
            c.execute('GRANT SELECT ON manufacturing_quotes,manufacturing_offers,manufacturing_requests TO authenticated')
            users=[str(uuid.uuid4()) for _ in range(5)]
            ret,other,ws1,ws2,pending=users
            retailer,otherret,w1,w2,w3=[str(uuid.uuid4()) for _ in users]
            for u in users:c.execute('INSERT INTO auth.users(id) VALUES(%s)',(u,))
            for rid,uid in [(retailer,ret),(otherret,other)]:c.execute("INSERT INTO retailers(id,user_id,business_name,verification_status) VALUES(%s,%s,'Retailer','verified')",(rid,uid))
            for wid,uid,status in [(w1,ws1,'verified'),(w2,ws2,'verified'),(w3,pending,'pending')]:c.execute("INSERT INTO wholesalers(id,user_id,business_name,verification_status) VALUES(%s,%s,'Supplier',%s)",(wid,uid,status))
        service=PostgresClientAdapter(uri,None)
        def identity(uid,rid,role):return {'user_id':uid,role+'_id':rid,'client':PostgresClientAdapter(uri,uid),'token':'token-'+uid}
        retail=identity(ret,retailer,'retailer')
        def as_ret(who=retail):app.dependency_overrides[api.require_verified_retailer]=lambda:who
        def as_ws(uid,wid):app.dependency_overrides[api.require_verified_wholesaler]=lambda:identity(uid,wid,'wholesaler')
        def rpc(uid,name,params):return PostgresClientAdapter(uri,uid).rpc(name,params).execute().data
        due=(date.today()+timedelta(days=14)).isoformat()
        quote={'making_charge_mode':'per_gram','making_charge_amount':400,'metal_estimate_amount':90000,'gemstone_estimate_amount':5000,'other_estimate_amount':100,'proposed_delivery_date':due,'comments':'Hallmarked'}
        def quote_params(oid,amount=400):return {'p_offer_id':oid,**{'p_'+k:(amount if k=='making_charge_amount' else v) for k,v in quote.items()}}
        with patch.object(api,'get_supabase',return_value=service),patch('app.services.manufacturing_scheduler.get_supabase',return_value=service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                assert (await client.post('/api/retailer/manufacturing-requests/'+str(uuid.uuid4())+'/award',json={'quote_id':str(uuid.uuid4())})).status_code==401
                assert (await client.post('/api/wholesaler/manufacturing-offers/'+str(uuid.uuid4())+'/quotes',json=quote)).status_code==401
                as_ret()
                async def create(key=None, unbudgeted=False):
                    photo=io.BytesIO();Image.new('RGB',(32,32),'gold').save(photo,'JPEG')
                    asset=(await client.post('/api/retailer/manufacturing-assets',files={'file':('photo.jpg',photo.getvalue(),'image/jpeg')})).json()['asset_id']
                    payload={'asset_id':asset,'category':'Necklace','min_weight_grams':8,'max_weight_grams':10,'material':'Gold','purity':'22kt','making_budget_mode':'per_gram','making_budget_amount':450,'delivery_needed_date':due,'quotation_window_hours':24}
                    if unbudgeted:
                        payload.pop('making_budget_mode'); payload.pop('making_budget_amount'); payload.pop('purity')
                    key=key or str(uuid.uuid4());hdr={'Idempotency-Key':key}
                    res=await client.post('/api/retailer/manufacturing-requests',json=payload,headers=hdr)
                    assert res.status_code==200,res.text
                    replay=await client.post('/api/retailer/manufacturing-requests',json=payload,headers=hdr)
                    assert res.json()==replay.json()
                    payload['quantity']=2
                    assert (await client.post('/api/retailer/manufacturing-requests',json=payload,headers=hdr)).status_code==422
                    return res.json()['request_id']
                rid=await create()
                with psycopg.connect(uri) as c:
                    offers=c.execute('SELECT id,wholesaler_id,status,expires_at FROM manufacturing_offers WHERE request_id=%s',(rid,)).fetchall()
                assert len(offers)==2 and all(o[2]=='open' for o in offers) and offers[0][3]==offers[1][3]
                ids={str(o[1]):str(o[0]) for o in offers};o1,o2=ids[w1],ids[w2]
                print('PASS simultaneous invitations, common deadline, eligibility and creation retries')
                as_ws(ws1,w1)
                assert (await client.post(f'/api/wholesaler/manufacturing-offers/{o1}/accept',json=quote)).status_code==403
                assert (await client.post(f'/api/wholesaler/manufacturing-offers/{o2}/quotes',json=quote)).status_code==404
                assert (await client.post(f'/api/wholesaler/manufacturing-offers/{o1}/quotes',json={**quote,'metal_estimate_amount':-1})).status_code==422
                first=await client.post(f'/api/wholesaler/manufacturing-offers/{o1}/quotes',json=quote)
                assert first.status_code==200,first.text
                assert first.json()==(await client.post(f'/api/wholesaler/manufacturing-offers/{o1}/quotes',json=quote)).json()
                assert (await client.post(f'/api/wholesaler/manufacturing-offers/{o1}/quotes',json={**quote,'making_charge_amount':390})).status_code==409
                q1=first.json()['quote_id']
                as_ws(ws2,w2)
                second=await client.post(f'/api/wholesaler/manufacturing-offers/{o2}/quotes',json={**quote,'making_charge_amount':420})
                assert second.status_code==200,second.text
                q2=second.json()['quote_id']
                as_ret()
                detail=(await client.get(f'/api/retailer/manufacturing-requests/{rid}')).json()['request']
                assert detail['state']=='collecting' and len(detail['quotes'])==2 and all(q['wholesaler'] for q in detail['quotes'])
                print('PASS multiple quotes, no supplier assignment, duplicate/conflicting retries and owned API access')
                # Enforce the actual authenticated SQL role, not just adapter identity.
                with psycopg.connect(uri,autocommit=True) as c:
                    c.execute('SET ROLE authenticated');c.execute("SELECT set_config('request.jwt.claim.sub',%s,false)",(ws1,))
                    assert len(c.execute('SELECT id FROM manufacturing_quotes').fetchall())==1
                    c.execute('RESET ROLE')
                    assert c.execute("SELECT has_function_privilege('authenticated','public.manufacturing_close_quotation_windows(integer)','EXECUTE')").fetchone()[0] is False
                as_ret(identity(other,otherret,'retailer'))
                assert (await client.get(f'/api/retailer/manufacturing-requests/{rid}')).status_code==404
                assert (await client.post(f'/api/retailer/manufacturing-requests/{rid}/award',json={'quote_id':q1})).status_code==404
                as_ret()
                with ThreadPoolExecutor(2) as pool:
                    results=list(pool.map(lambda qid:rpc(ret,'manufacturing_quote_award',{'p_request_id':rid,'p_quote_id':qid}),[q1,q2]))
                assert sum(bool(r['ok']) for r in results)==1,results
                winner=next(r['quote_id'] for r in results if r['ok'])
                assert (await client.post(f'/api/retailer/manufacturing-requests/{rid}/award',json={'quote_id':winner})).status_code==200
                assert (await client.post(f'/api/retailer/manufacturing-requests/{rid}/cancel')).status_code==409
                print('PASS retailer-only award, quote privacy, worker permissions and exactly one concurrent winner')
                # Decline leaves all other suppliers open; cancellation invalidates quotes.
                rid2=await create()
                rows=service.table('manufacturing_offers').select('*').eq('request_id',rid2).execute().data
                as_ws(ws1,w1);one=next(o for o in rows if o['wholesaler_id']==w1)
                assert (await client.post(f"/api/wholesaler/manufacturing-offers/{one['id']}/decline",json={'reason':'Busy'})).status_code==200
                assert service.table('manufacturing_requests').select('*').eq('id',rid2).execute().data[0]['state']=='collecting'
                as_ret();assert (await client.post(f'/api/retailer/manufacturing-requests/{rid2}/cancel')).status_code==200
                two=next(o for o in rows if o['wholesaler_id']==w2)
                assert not rpc(ws2,'manufacturing_quote_submit',quote_params(two['id']))['ok']
                print('PASS decline independence and cancellation closure')
                # New simplified form sends no gemstone preference or budget.
                unbudgeted=await create(unbudgeted=True)
                record=service.table('manufacturing_requests').select('*').eq('id',unbudgeted).execute().data[0]
                assert record['making_budget_mode'] is None and record['making_budget_amount'] is None
                assert record['gemstone_preference']=='unspecified'
                assert record['purity'] is None
                rows=service.table('manufacturing_offers').select('*').eq('request_id',unbudgeted).execute().data
                assert len(rows)==2 and all(o['status']=='open' for o in rows)
                with psycopg.connect(uri) as c:
                    count=c.execute("SELECT count(DISTINCT recipient_user_id) FROM manufacturing_notification_outbox WHERE kind='NEW_MANUFACTURING_OFFER' AND payload->>'request_id'=%s",(unbudgeted,)).fetchone()[0]
                assert count==2
                for index,row in enumerate(rows):
                    who=ws1 if row['wholesaler_id']==w1 else ws2
                    params=quote_params(row['id'],900)
                    params['p_making_charge_mode']='fixed_total' if index else 'per_gram'
                    if index:
                        as_ws(who, row['wholesaler_id'])
                        total_payload={'total_quote_amount':99000,'proposed_delivery_date':due,'comments':'Entire quantity'}
                        endpoint=f"/api/wholesaler/manufacturing-offers/{row['id']}/quotes"
                        bad=await client.post(endpoint,json={**total_payload,'total_quote_amount':0})
                        assert bad.status_code==422,bad.text
                        response=await client.post(endpoint,json=total_payload)
                        assert response.status_code==200,response.text
                        assert (await client.post(endpoint,json=total_payload)).json()==response.json()
                        stored=service.table('manufacturing_quotes').select('*').eq('id',response.json()['quote_id']).execute().data[0]
                        assert stored['making_charge_mode']=='total_quote' and stored['making_charge_amount']==99000
                        assert stored['metal_estimate_amount']==stored['gemstone_estimate_amount']==stored['other_estimate_amount']==0
                    else:
                        assert rpc(who,'manufacturing_quote_submit',params)['ok']
                as_ret(); assert (await client.get(f'/api/retailer/manufacturing-requests/{unbudgeted}')).status_code==200
                assert (await client.post(f'/api/retailer/manufacturing-requests/{unbudgeted}/cancel')).status_code==200
                print('PASS unbudgeted creation, unconstrained quote basis, and outbox notification for every eligible wholesaler')

                # Award versus cancellation: no cancelled request can retain an assignment.
                race_request=await create()
                race_offer=service.table('manufacturing_offers').select('*').eq('request_id',race_request).execute().data
                race_one=next(o for o in race_offer if o['wholesaler_id']==w1)
                race_quote=rpc(ws1,'manufacturing_quote_submit',quote_params(race_one['id']))['quote_id']
                with ThreadPoolExecutor(2) as pool:
                    award_future=pool.submit(rpc,ret,'manufacturing_quote_award',{'p_request_id':race_request,'p_quote_id':race_quote})
                    cancel_future=pool.submit(rpc,ret,'manufacturing_request_cancel',{'p_request_id':race_request})
                    results=[award_future.result(),cancel_future.result()]
                assert sum(bool(r['ok']) for r in results)==1,results
                state=service.table('manufacturing_requests').select('*').eq('id',race_request).execute().data[0]
                assert state['state'] in ('assigned','cancelled')
                if state['state']=='cancelled':assert state['accepted_quote_id'] is None
                # A quote racing retailer selection cannot reopen the project.
                race_request=await create()
                rows=service.table('manufacturing_offers').select('*').eq('request_id',race_request).execute().data
                one=next(o for o in rows if o['wholesaler_id']==w1);two=next(o for o in rows if o['wholesaler_id']==w2)
                existing=rpc(ws1,'manufacturing_quote_submit',quote_params(one['id']))['quote_id']
                with ThreadPoolExecutor(2) as pool:
                    submit=pool.submit(rpc,ws2,'manufacturing_quote_submit',quote_params(two['id']))
                    award_result=pool.submit(rpc,ret,'manufacturing_quote_award',{'p_request_id':race_request,'p_quote_id':existing})
                    submit.result();assert award_result.result()['ok']
                rows=service.table('manufacturing_offers').select('*').eq('request_id',race_request).execute().data
                assert sum(o['status']=='accepted' for o in rows)==1 and not any(o['status'] in ('open','quoted') for o in rows)
                print('PASS concurrent cancellation/award and quote/award races')
                # Deadline closes submission, retains received quotes for retailer review.
                rid3=await create();rows=service.table('manufacturing_offers').select('*').eq('request_id',rid3).execute().data
                one=next(o for o in rows if o['wholesaler_id']==w1);two=next(o for o in rows if o['wholesaler_id']==w2)
                q=rpc(ws1,'manufacturing_quote_submit',quote_params(one['id']))
                with psycopg.connect(uri,autocommit=True) as c:c.execute("UPDATE manufacturing_requests SET quotation_deadline=clock_timestamp()-interval '1 second' WHERE id=%s",(rid3,))
                assert not rpc(ws2,'manufacturing_quote_submit',quote_params(two['id']))['ok']
                assert service.rpc('manufacturing_close_quotation_windows',{}).execute().data['closed_count']==1
                assert rpc(ret,'manufacturing_quote_award',{'p_request_id':rid3,'p_quote_id':q['quote_id']})['ok']
                rid4=await create()
                with psycopg.connect(uri,autocommit=True) as c:c.execute("UPDATE manufacturing_requests SET quotation_deadline=clock_timestamp()-interval '1 second' WHERE id=%s",(rid4,))
                service.rpc('manufacturing_close_quotation_windows',{}).execute()
                assert service.table('manufacturing_requests').select('*').eq('id',rid4).execute().data[0]['state']=='exhausted'
                print('PASS deadline authority, review after close, empty-window exhaustion and rerunnable migration')
        print('ALL PARALLEL BROADCAST TESTS PASSED')
    finally:
        app.dependency_overrides.clear()
        server._cleanup()

if __name__=='__main__':asyncio.run(run())
