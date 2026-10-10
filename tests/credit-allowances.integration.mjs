// Disposable real PostgreSQL, including multiple independent connections.
// Runtime dependencies: embedded-postgres and pg (installed outside the application).
import assert from 'node:assert/strict';
import { randomUUID, randomBytes } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(process.env.JEWEL_TEST_RUNTIME ? path.join(process.env.JEWEL_TEST_RUNTIME, '../package.json') : import.meta.url);
const EmbeddedPostgres = require('embedded-postgres').default;
const { Client } = require('pg');
const workspace = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const testDir = await mkdtemp(path.join(tmpdir(), 'jewel-credit-sharing-'));
const port = 55000 + Math.floor(Math.random() * 5000);
const database = new EmbeddedPostgres({ databaseDir: path.join(testDir, 'db'), user: 'postgres', password: 'local-tests-only',
  port, persistent: false, postgresFlags: ['-h', '127.0.0.1', '-k', testDir], onLog: () => {}, onError: () => {} });
const connections = [];
async function connect(role) {
  const client = new Client({ host: '127.0.0.1', port, user: 'postgres', password: 'local-tests-only', database: 'postgres' });
  await client.connect(); connections.push(client);
  if (role) await client.query(`SET ROLE ${role}`);
  await client.query("SELECT set_config('request.jwt.claim.role',$1,false)",[role || 'service_role']);
  return client;
}
const hash = () => randomBytes(32).toString('hex');
let checks = 0;
function check(name, fn) { fn(); checks += 1; console.log(`PASS ${name}`); }
const q = (client, text, values) => client.query(text, values);
async function rpc(client, name, args = []) {
  const { rows } = await client.query(`SELECT public.${name}(${args.map((_, i) => `$${i + 1}`).join(',')}) AS result`, args);
  return rows[0].result;
}

try {
  await database.initialise(); await database.start();
  const admin = await connect();
  await admin.query(`
    CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE sql STABLE AS
      $$ SELECT nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
    CREATE FUNCTION auth.role() RETURNS TEXT LANGUAGE sql STABLE AS
      $$ SELECT nullif(current_setting('request.jwt.claim.role', true),'') $$;
    CREATE TABLE public.wholesalers(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),user_id UUID REFERENCES auth.users,
      verification_status TEXT,full_name TEXT,business_name TEXT,phone TEXT,email TEXT,business_logo_url TEXT);
    CREATE TABLE public.retailers(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),user_id UUID REFERENCES auth.users,
      verification_status TEXT,full_name TEXT,business_name TEXT,selected_theme TEXT,referred_by UUID,referral_code TEXT,rejection_reason TEXT,notification_message TEXT,notified BOOLEAN,admin_notes TEXT,rejected_documents TEXT[]);
    CREATE TABLE public.employees(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),auth_user_id UUID REFERENCES auth.users,
      retailer_id UUID REFERENCES public.retailers,status TEXT,full_name TEXT);
    CREATE TABLE public.products(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),title TEXT,jewellery_type TEXT,net_weight NUMERIC,
      is_published BOOLEAN,processed_image_url TEXT,raw_image_url TEXT,wholesaler_email TEXT,
      showcase_image_urls TEXT[],generated_image_urls TEXT[],created_at TIMESTAMPTZ DEFAULT now());
    GRANT USAGE ON SCHEMA auth,public TO anon,authenticated,service_role;
    CREATE SCHEMA jewel_test;
    CREATE TABLE jewel_test.time(value TIMESTAMPTZ);
    INSERT INTO jewel_test.time VALUES('2026-10-01 18:29:00+00');
    CREATE FUNCTION jewel_test.clock() RETURNS TIMESTAMPTZ LANGUAGE sql VOLATILE AS $$ SELECT value FROM jewel_test.time $$;
  `);
  await admin.query(`CREATE TABLE referral_links(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),wholesaler_id UUID REFERENCES wholesalers(id),code TEXT UNIQUE,uses_count INT DEFAULT 0,max_uses INT DEFAULT 1,is_active BOOLEAN DEFAULT true,created_at TIMESTAMPTZ DEFAULT now());`);
  const migrations = [
    'ai-pipeline/migrations/004a_tables_and_rls.sql', 'ai-pipeline/migrations/004b_functions.sql',
    'ai-pipeline/migrations/004c_trigger_seed_grants.sql', 'ai-pipeline/migrations/006_razorpay_purchases.sql',
    'ai-pipeline/migrations/007_rupee_denominated_credits.sql', 'ai-pipeline/migrations/008_welcome_grant_2000.sql',
    'wholesaler ios/supabase/migrations/20260919_01_retailer_wallet_entitlements.sql',
    'wholesaler ios/supabase/migrations/20260919_02_customer_wishlists.sql',
    'wholesaler ios/supabase/migrations/20260919_03_retailer_plans.sql',
    'wholesaler ios/supabase/migrations/20260926_01_apple_iap_credits.sql',
    'ai-pipeline/migrations/014_wishlist_sharing.sql', 'ai-pipeline/migrations/016_credit_history_rpc.sql',
    'ai-pipeline/migrations/015_daily_credit_program.sql','ai-pipeline/migrations/012_retailer_referrals.sql','ai-pipeline/migrations/017_invitation_gifts.sql','ai-pipeline/migrations/018_preserve_purchased_credits.sql',
  ];
  for (const file of migrations) {
    const sql = await readFile(path.join(workspace, file), 'utf8');
    // Boundary tests use one shared controlled clock. All other SQL/locking is unchanged.
    await admin.query(sql.replaceAll('clock_timestamp()', 'jewel_test.clock()'));
    console.log(`APPLIED ${path.basename(file)}`);

  }

  await admin.query('ALTER TABLE wholesalers ADD COLUMN city TEXT, ADD COLUMN state TEXT');
  // Existing source tables need service privileges; new private tables must retain
  // the restrictive grants installed by the real migration, including audit immutability.
  await admin.query('GRANT ALL ON ALL TABLES IN SCHEMA public,auth TO service_role; GRANT USAGE ON SCHEMA jewel_test TO service_role; GRANT SELECT ON jewel_test.time TO service_role');
  await admin.query('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO anon,authenticated');
  const sql=await readFile(path.join(workspace,'ai-pipeline/migrations/023_wholesaler_credit_allowance_policy.sql'),'utf8');
  assert.equal(sql,await readFile(path.join(workspace,'wholesaler ios/supabase/migrations/20261010_01_wholesaler_credit_allowance_policy.sql'),'utf8'));
  await admin.query(sql.replaceAll('clock_timestamp()','jewel_test.clock()'));
  // Applying the deployment mirror again must parse and neither reset policies nor grant.
  await admin.query(sql.replaceAll('clock_timestamp()','jewel_test.clock()'));
  await admin.query('ALTER TABLE wholesalers RENAME COLUMN phone TO phone_number');
  const businesses=await readFile(path.join(workspace,'ai-pipeline/migrations/024_business_credit_allowances.sql'),'utf8');
  assert.equal(businesses,await readFile(path.join(workspace,'wholesaler ios/supabase/migrations/20261010_02_business_credit_allowances.sql'),'utf8'));
  await admin.query(businesses.replaceAll('clock_timestamp()','jewel_test.clock()'));
  await admin.query(businesses.replaceAll('clock_timestamp()','jewel_test.clock()'));
  const service=await connect('service_role'),anon=await connect('anon'),client=await connect('authenticated');
  const time=async value=>q(admin,'UPDATE jewel_test.time SET value=$1',[value]);
  const make=async(name,status='verified')=>{
    const user=randomUUID(); await q(admin,'INSERT INTO auth.users VALUES($1)',[user]);
    const id=(await q(admin,'INSERT INTO wholesalers(user_id,verification_status,business_name) VALUES($1,$2,$3) RETURNING id',[user,status,name])).rows[0].id;
    return {user,id};
  };
  const set=(w,amount,version,key=randomUUID(),reset=false,reason='Test policy change')=>rpc(service,'admin_set_credit_allowance',[w.id,amount,reason,version,key,reset]);
  const list=w=>rpc(service,'admin_list_credit_allowances',[null,0,25,w.id]);
  const wallet=async w=>{await q(client,"SELECT set_config('request.jwt.claim.sub',$1,false)",[w.user]);return rpc(client,'credits_wallet');};
  const w=await make('Allowance test');
  const before=(await q(admin,'SELECT count(*)::int n FROM credit_lots WHERE account_id=$1',[w.user])).rows[0].n;
  assert.equal((await set(w,5000,0)).ok,true);
  assert.equal((await q(admin,'SELECT count(*)::int n FROM credit_lots WHERE account_id=$1',[w.user])).rows[0].n,before);
  const listed=await list(w); assert.equal(listed.items.length,1);assert.equal(listed.items[0].wholesaler_id,w.id);assert.equal(listed.program_active,false);
  check('saving and ID-filtered reporting do not issue grants or activate the program',()=>{});
  assert.equal((await rpc(service,'credits_activate_daily')).ok,true);
  const first=await wallet(w);assert.equal(first.daily_available,5000);assert.equal(first.policy_type,'rolling_24h');
  const end=first.resets_at;assert.equal(Date.parse(end)-Date.parse(first.issued_at),86400000);
  const lot=(await q(admin,"SELECT * FROM credit_lots WHERE account_id=$1 AND source='daily'",[w.user])).rows[0];
  const charge=await rpc(service,'spend_credits',[w.user,'chamak.generate','custom-spend','test','job',{}]);assert.equal(charge.balance,4800);
  assert.equal((await rpc(service,'spend_credits',[w.user,'chamak.generate','custom-spend','test','job',{}])).replayed,true);
  const lower=await set(w,4900,1); assert.equal(lower.ok,true);
  const up=await set(w,5000,2);assert.equal(up.ok,true);
  assert.equal((await wallet(w)).available,4800);
  assert.equal((await q(admin,'SELECT credits_granted FROM credit_lots WHERE id=$1',[lot.id])).rows[0].credits_granted,5000);
  check('lowering and raising an allowance cannot edit issued grants or replenish spent credits',()=>{});
  const pauseKey=randomUUID();const paused=await set(w,0,3,pauseKey);assert.equal(paused.ok,true);
  const scheduled=await wallet(w);assert.equal(scheduled.daily_allowance,5000);assert.equal(scheduled.next_allowance,0);assert.equal(scheduled.pause_scheduled,true);assert.equal(scheduled.is_paused,false);assert.equal(scheduled.resets_at,end);
  assert.equal((await set(w,0,3,pauseKey)).replayed,true);
  assert.equal((await set(w,1000,3,pauseKey)).error,'IDEMPOTENCY_CONFLICT');
  assert.equal((await set(w,6000,3)).error,'VERSION_CONFLICT');
  assert.equal((await q(admin,'SELECT count(*)::int n FROM credit_allowance_audit WHERE wholesaler_user_id=$1',[w.user])).rows[0].n,4);
  check('pause preserves the active allowance; retries replay once and conflicting edits are rejected',()=>{});
  await time('2026-10-01 18:30:01+00');assert.equal((await wallet(w)).daily_available,4800);assert.equal((await wallet(w)).resets_at,end);
  await time(new Date(Date.parse(end)-1000).toISOString());assert.equal((await wallet(w)).daily_available,4800);
  check('midnight and 23:59:59 after issuance cannot bypass the 24-hour deadline',()=>{});
  await time(end);const stopped=await wallet(w);assert.equal(stopped.daily_available,0);assert.equal(stopped.is_paused,true);assert.equal(stopped.resets_at,null);
  assert.equal((await q(admin,"SELECT count(*)::int n FROM credit_lots WHERE account_id=$1 AND source='daily'",[w.user])).rows[0].n,1);
  const late=await rpc(service,'refund_debit',[charge.ledger_id,'Late failed job']);assert.equal(late.refunded,0);
  check('pause takes effect at expiry without a zero-credit lot; late daily refunds cannot resurrect it',()=>{});
  // A genuinely paused business can resume at its next eligible access: no fake
  // waiting window is started when no credits were issued.
  const resumed=await set(w,null,4,randomUUID(),true);assert.equal(resumed.ok,true);
  const resetRow=(await list(w)).items[0];assert.equal(resetRow.is_custom,false);assert.equal(resetRow.policy_version,5);assert.equal(resetRow.daily_allowance,2000);
  const resumedWallet=await wallet(w);assert.equal(resumedWallet.daily_available,2000);assert.equal(resumedWallet.is_paused,false);
  check('reset restores inherited default and resumes an eligible paused business',()=>{});
  const b=await make('Concurrent issue');await set(b,3000,0);
  const racers=await Promise.all(Array.from({length:8},()=>connect('service_role')));
  const budgets=await Promise.all(racers.map(c=>rpc(c,'credits_ensure_daily_budget',[b.user])));assert(budgets.every(x=>x.balance===3000));
  assert.equal((await q(admin,'SELECT count(*)::int n FROM credit_allowance_cycles WHERE account_id=$1',[b.user])).rows[0].n,1);
  const target=budgets[0].resets_at;
  await time(target);await set(b,1000,1);
  const nextBudgets=await Promise.all(racers.map(c=>rpc(c,'credits_ensure_daily_budget',[b.user])));assert(nextBudgets.every(x=>x.daily_allowance===1000));
  assert.equal((await q(admin,'SELECT count(*)::int n FROM credit_allowance_cycles WHERE account_id=$1',[b.user])).rows[0].n,2);
  check('concurrent initial/refill requests issue one grant with the scheduled amount',()=>{});
  const spendRaces=await Promise.all(racers.map((c,i)=>rpc(c,'spend_credits',[b.user,'chamak.generate','race-'+i,'test','race-'+i,{}])));
  assert.equal(spendRaces.filter(x=>x.ok).length,5);assert.equal((await wallet(b)).available,0);
  const refunds=await Promise.all(racers.map(c=>rpc(c,'refund_debit',[spendRaces.find(x=>x.ok).ledger_id,'Failure'])));
  assert.equal(refunds.reduce((sum,x)=>sum+x.refunded,0),200);
  check('concurrent spends cannot overdraw and duplicate refunds restore the original allocation once',()=>{});
  const c=await make('Concurrent policies');const key=randomUUID();
  const writes=await Promise.all(racers.map(conn=>rpc(conn,'admin_set_credit_allowance',[c.id,7500,'Concurrent request',0,key,false])));
  assert(writes.every(x=>x.ok));assert.equal(writes.filter(x=>!x.replayed).length,1);
  const conflicting=await Promise.all(racers.slice(0,2).map((conn,i)=>rpc(conn,'admin_set_credit_allowance',[c.id,8000+i,'Concurrent edit',1,randomUUID(),false])));
  assert.equal(conflicting.filter(x=>x.ok).length,1);assert.equal(conflicting.filter(x=>x.error==='VERSION_CONFLICT').length,1);
  const unconfigured=await make('Version check');assert.equal((await set(unconfigured,2000,999)).error,'VERSION_CONFLICT');
  assert.equal((await set(unconfigured,2000,null)).error,'INVALID_ARGUMENTS');
  assert.equal((await set(unconfigured,-1,0)).error,'INVALID_ARGUMENTS');assert.equal((await set(unconfigured,100001,0)).error,'INVALID_ARGUMENTS');
  assert.equal((await set(unconfigured,2000,0,randomUUID(),false,'')).error,'INVALID_ARGUMENTS');
  const unverified=await make('Pending business','pending');assert.equal((await set(unverified,2000,0)).error,'NOT_VERIFIED');
  check('versions cover absent/default policies, concurrent editors, bounds and verification',()=>{});
  for(const conn of [anon,client]) {
    for(const table of ['credit_allowance_policies','credit_allowance_audit','credit_allowance_requests','credit_allowance_cycles']) {
      await assert.rejects(()=>q(conn,`SELECT * FROM ${table}`),e=>e.code==='42501');
      await assert.rejects(()=>q(conn,`DELETE FROM ${table}`),e=>e.code==='42501');
    }
    await assert.rejects(()=>rpc(conn,'admin_set_credit_allowance',[b.id,100000,'Tampered',2,randomUUID(),false]),e=>e.code==='42501');
    await assert.rejects(()=>rpc(conn,'credits_ensure_daily_budget',[b.user]),e=>e.code==='42501');
  }
  await assert.rejects(()=>q(service,'DELETE FROM credit_allowance_audit'),e=>e.code==='42501');
  assert((await q(admin,"SELECT relrowsecurity FROM pg_class WHERE relname IN ('credit_allowance_policies','credit_allowance_audit','credit_allowance_requests','credit_allowance_cycles')")).rows.every(x=>x.relrowsecurity));
  check('inherited public privileges cannot bypass RLS, privileged RPCs or append-only audit',()=>{});
  // Fixture paid and gift lots isolate allowance pause/spend/refund behavior;
  // full receipt-preservation reconciliation is covered by referral-gifts.integration.
  await q(admin,"INSERT INTO credit_lots(account_id,source,credits_granted,credits_remaining,paid_preserved_at) VALUES($1,'purchase',500,500,jewel_test.clock())",[b.user]);
  await q(admin,"INSERT INTO credit_lots(account_id,source,credits_granted,credits_remaining) VALUES($1,'invitation_gift',300,300)",[b.user]);
  await set(b,0,2);await time(nextBudgets[0].resets_at);
  const paid=await wallet(b);assert.equal(paid.daily_available,0);assert.equal(paid.paid_available,500);assert.equal(paid.gift_available,300);assert.equal(paid.available,800);
  const paidCharge=await rpc(service,'spend_credits',[b.user,'chamak.generate','paused-paid','test','paid-job',{}]);assert.equal(paidCharge.ok,true);
  await time('2026-10-15 18:30:00+00');assert.equal((await rpc(service,'refund_debit',[paidCharge.ledger_id,'Late bonus failure'])).refunded,200);
  assert.equal((await wallet(b)).available,800);
  check('paid/gift credits remain spendable while paused and preserve their late-refund rules',()=>{});
  const returned=await wallet(c);assert.equal(returned.daily_available,8000 + (conflicting[1].ok ? 1 : 0));
  assert.equal((await q(admin,"SELECT count(*)::int n FROM credit_lots WHERE account_id=$1 AND source='daily'",[c.user])).rows[0].n,1);
  check('inactivity issues one allowance without accumulating missed days',()=>{});
  const old=await make('Calendar transition');await rpc(admin,'credits_ensure_account',[old.user]);
  await q(admin,"INSERT INTO credit_lots(account_id,source,credits_granted,credits_remaining,granted_at,expires_at,budget_date) VALUES($1,'daily',2000,1500,'2026-10-15 18:29:00+00','2026-10-15 18:30:00+00','2026-10-15')",[old.user]);
  const adopted=await wallet(old);assert.equal(adopted.daily_available,1500);assert.equal(adopted.resets_at,'2026-10-16T23:59:00+05:30');
  assert.equal((await q(admin,"SELECT count(*)::int n FROM credit_lots WHERE account_id=$1 AND source='daily'",[old.user])).rows[0].n,1);
  check('calendar transition retains issued units and original issuance anchor without double granting',()=>{});
  const retailer=randomUUID(),staff=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1),($2)',[retailer,staff]);
  const store=(await q(admin,"INSERT INTO retailers(user_id,verification_status) VALUES($1,'verified') RETURNING id",[retailer])).rows[0].id;
  await q(admin,"INSERT INTO employees(auth_user_id,retailer_id,status) VALUES($1,$2,'active')",[staff,store]);
  assert.equal((await rpc(service,'admin_set_business_credit_allowance',['retailer',store,1500,'Store allowance',0,randomUUID(),false])).ok,true);
  const beforeRetailer=(await rpc(service,'admin_list_business_credit_allowances',['retailer',null,0,25,store]));assert.equal(beforeRetailer.items[0].daily_allowance,1500);assert.equal(beforeRetailer.items[0].current_allowance,null);
  const rw=await wallet({user:retailer});assert.equal(rw.daily_available,1500);assert.equal(new Date(rw.resets_at)-new Date(rw.issued_at),86400000);const sw=await wallet({user:staff});assert.equal(rw.resets_at,sw.resets_at);assert.equal(sw.shared_business_wallet,true);assert.equal(sw.policy_type,'rolling_24h');
  check('retailer allocation issues one 24-hour allowance shared with staff',()=>{});
  assert.equal((await rpc(service,'admin_set_business_credit_allowance',['retailer',store,0,'Pause after this cycle',1,randomUUID(),false])).ok,true);
  assert.equal((await wallet({user:staff})).daily_available,1500);
  await time(rw.resets_at);assert.equal((await wallet({user:retailer})).daily_available,0);
  assert.equal((await rpc(service,'admin_set_business_credit_allowance',['retailer',store,null,'Restore default',2,randomUUID(),true])).ok,true);
  assert.equal((await wallet({user:retailer})).daily_available,2000);
  assert.equal((await rpc(service,'admin_set_business_credit_allowance',['employee',store,1500,'Invalid role',3,randomUUID(),false])).error,'INVALID_ARGUMENTS');
  await assert.rejects(()=>rpc(anon,'admin_set_business_credit_allowance',['retailer',store,100000,'Tampered',3,randomUUID(),false]),e=>e.code==='42501');
  check('retailer pause/reset preserves current grants and rejects unsupported roles/direct API access',()=>{});
  // Hold the account lock across expiry; server time must be read after the wait.
  const locker=await connect(),blocked=await connect('service_role');const pid=(await q(blocked,'SELECT pg_backend_pid() pid')).rows[0].pid;
  await q(locker,'BEGIN');await q(locker,'SELECT 1 FROM credit_accounts WHERE wholesaler_id=$1 FOR UPDATE',[old.user]);
  const waiting=rpc(blocked,'spend_credits',[old.user,'chamak.generate','waiting-expiry','test','wait-job',{}]);
  for(let i=0;i<100;i++) {if((await q(admin,'SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted',[pid])).rowCount) break;await new Promise(resolve=>setTimeout(resolve,10));}
  await time(adopted.resets_at);await q(locker,'COMMIT');assert.equal((await waiting).balance,1800);
  const after=await wallet(old);assert.equal(after.daily_available,1800);assert.equal(after.issued_at,adopted.resets_at);
  check('a spend waiting across expiry charges the new 24-hour cycle',()=>{});
  await q(admin,"UPDATE wholesalers SET verification_status='banned' WHERE id=$1",[old.id]);assert.equal((await wallet(old)).error,'NOT_VERIFIED');
  check('suspended businesses cannot access recurring grants',()=>{});
  console.log(`${checks} allowance integration scenarios passed using real PostgreSQL.`);
} finally {await Promise.allSettled(connections.map(c=>c.end()));await database.stop().catch(()=>{});await rm(testDir,{recursive:true,force:true});}
