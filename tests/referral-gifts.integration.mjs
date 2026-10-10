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
    ...(process.env.JEWEL_ALLOWANCE_MIGRATION==='1' ? ['ai-pipeline/migrations/023_wholesaler_credit_allowance_policy.sql','ai-pipeline/migrations/024_business_credit_allowances.sql','ai-pipeline/migrations/026_admin_credit_grants.sql'] : []),
  ];
  for (const file of migrations) {
    const sql = await readFile(path.join(workspace, file), 'utf8');
    // Boundary tests use one shared controlled clock. All other SQL/locking is unchanged.
    await admin.query(sql.replaceAll('clock_timestamp()', 'jewel_test.clock()'));
    console.log(`APPLIED ${path.basename(file)}`);

  }
  await admin.query('GRANT USAGE ON SCHEMA jewel_test TO service_role; GRANT SELECT ON jewel_test.time TO service_role; GRANT ALL ON ALL TABLES IN SCHEMA public,auth TO service_role');
  const gates=await readFile(path.join(workspace,'Admin-Panel-for-jewel-India-/supabase/migrations/onboarding_three_doors.sql'),'utf8');
  await admin.query(gates.slice(gates.indexOf('create or replace function public.gate_retailer_insert()'),gates.indexOf('-- The admin columns')));
  const insertGuard=await readFile(path.join(workspace,'Admin-Panel-for-jewel-India-/supabase/migrations/guard_retailer_privileged_columns.sql'),'utf8');
  await admin.query(insertGuard);
  await admin.query(gates.slice(gates.indexOf('create or replace function public.jewel_trusted()'),gates.indexOf('-- 1. ROLE')));
  await admin.query(gates.slice(gates.indexOf('create or replace function public.guard_retailer_privileged_columns()'),gates.indexOf('-- A retailer who applied')));
  const service=await connect('service_role');
  const inviter=randomUUID();
  await q(admin,'INSERT INTO auth.users VALUES($1)',[inviter]);
  const ws=(await q(admin,"INSERT INTO wholesalers(user_id,verification_status,business_name) VALUES($1,'verified','Inviter') RETURNING id",[inviter])).rows[0].id;
  const paidUser=randomUUID();
  await q(admin,'INSERT INTO auth.users VALUES($1)',[paidUser]);
  await q(admin,"INSERT INTO wholesalers(user_id,verification_status,business_name) VALUES($1,'verified','Paid owner')",[paidUser]);
  await rpc(admin,'credits_ensure_account',[paidUser]);
  const purchase=(await q(admin,"INSERT INTO credit_purchases(account_id,provider,provider_txn_id,pack_key,amount_inr,credits,status) VALUES($1,'manual',$2,'preservation-test',500,500,'paid') RETURNING id",[paidUser,randomUUID()])).rows[0].id;
  await rpc(service,'grant_credits',[paidUser,500,'purchase','paid-preservation-fixture','2026-10-02T00:00:00Z',purchase]);
  const paidLot=(await q(admin,'SELECT * FROM credit_lots WHERE purchase_id=$1',[purchase])).rows[0];
  const totals=(await q(admin,'SELECT lifetime_granted,lifetime_spent FROM credit_accounts WHERE wholesaler_id=$1',[paidUser])).rows[0];
  assert.equal((await rpc(service,'credits_activate_daily')).error,'LEGACY_PAID_BALANCES_REQUIRE_DECISION');
  await q(admin,"UPDATE credit_purchases SET status='refunded' WHERE id=$1",[purchase]);
  assert.equal((await rpc(service,'credits_preserve_purchased')).error,'PAID_RECEIPTS_REQUIRE_RECONCILIATION');
  assert.equal((await q(admin,'SELECT count(*)::int n FROM credit_paid_preservation')).rows[0].n,0);
  await q(admin,"UPDATE credit_purchases SET status='paid' WHERE id=$1",[purchase]);
  check('unreconciled paid receipts prevent conversion without partial changes',()=>{});
  const preservation=await rpc(service,'credits_preserve_purchased');
  check('paid preservation retains units and receipt without another grant',()=>{assert.equal(preservation.ok,true);assert.equal(preservation.preserved_units,500);});
  const kept=(await q(admin,'SELECT * FROM credit_lots WHERE id=$1',[paidLot.id])).rows[0];
  assert.equal(kept.purchase_id,purchase);assert.equal(kept.credits_remaining,500);assert.equal(kept.credits_granted,500);assert.equal(kept.expires_at,null);assert.equal(kept.source,'purchase');
  assert.deepEqual((await q(admin,'SELECT lifetime_granted,lifetime_spent FROM credit_accounts WHERE wholesaler_id=$1',[paidUser])).rows[0],totals);
  assert.equal((await rpc(service,'credits_preserve_purchased')).replayed,true);
  const preserveRacers=await Promise.all([connect('service_role'),connect('service_role')]);
  await Promise.all(preserveRacers.map(c=>rpc(c,'credits_preserve_purchased')));
  assert.equal((await q(admin,"SELECT count(*)::int n FROM credit_ledger WHERE reference_type='purchased_carryover'")).rows[0].n,1);
  check('preservation retries and concurrent calls cannot duplicate credits',()=>{});
  const outsider=await connect('authenticated');
  await assert.rejects(()=>rpc(outsider,'credits_preserve_purchased'),/permission denied/);
  await assert.rejects(()=>q(outsider,'SELECT * FROM credit_paid_preservation'),/permission denied/);
  check('paid preservation is restricted to trusted operators',()=>{});
  await admin.query('BEGIN');
  await q(admin,'DELETE FROM credit_paid_preservation WHERE lot_id=$1',[paidLot.id]);
  assert.equal((await rpc(admin,'credits_activate_daily')).error,'PAID_PRESERVATION_AUDIT_MISMATCH');
  await admin.query('ROLLBACK');
  check('activation refuses a paid-preservation marker without matching audit',()=>{});
  assert.equal((await rpc(admin,'credits_activate_daily')).ok,true);
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,false)",[paidUser]);
  const paidWallet=await rpc(admin,'credits_wallet');assert.equal(paidWallet.bonus_available,500);assert.equal(paidWallet.daily_available,2000);assert.equal(paidWallet.available,2500);
  const paidGift=await rpc(service,'referral_generate',[paidUser,5000,randomUUID(),'ios']);assert.equal(paidGift.ok,false); // referral gate is still closed

  const key=randomUUID();
  assert.equal((await rpc(service,'referral_generate',[inviter,1000,key,'ios'])).error,'REFERRALS_PAUSED');
  assert.equal((await rpc(service,'referral_activate')).ok,true);
  const paidFunded=await rpc(service,'referral_generate',[paidUser,3500,randomUUID(),'ios']);assert.equal(paidFunded.ok,true);
  assert.equal(await rpc(admin,'credits_recompute',[paidUser]),0);
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-03 06:00:00+00'");
  assert.equal((await rpc(service,'referral_cancel',[paidUser,paidFunded.id])).refunded,500);
  await rpc(service,'credits_ensure_daily_budget',[paidUser]);assert.equal(await rpc(admin,'credits_recompute',[paidUser]),2500);
  assert.equal((await q(admin,'SELECT archived_at,expires_at FROM credit_lots WHERE id=$1',[paidLot.id])).rows[0].archived_at,null);
  check('preserved paid credits fund gifts and survive midnight refunds',()=>{});
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-01 18:29:00+00'");
  const free=await rpc(service,'referral_generate',[inviter,1000,key,'ios']);
  check('base 1000 costs zero',()=>{assert.equal(free.extra_credits,0);assert.equal(free.funding_ledger_id,null);});
  assert.equal((await rpc(service,'referral_generate',[inviter,1500,key,'ios'])).error,'IDEMPOTENCY_CONFLICT');
  assert.equal((await rpc(service,'referral_generate',[inviter,1001,randomUUID(),'ios'])).error,'INVALID_SETTINGS');
  assert.equal((await rpc(service,'referral_generate',[inviter,10000,randomUUID(),'ios'])).error,'INSUFFICIENT_CREDITS');
  const racers=await Promise.all(Array.from({length:8},()=>connect('service_role')));
  const sharedKey=randomUUID();
  const generated=await Promise.all(racers.map(c=>rpc(c,'referral_generate',[inviter,2500,sharedKey,'web'])));
  check('concurrent generation charges once',()=>{assert(generated.every(x=>x.ok));assert.equal(new Set(generated.map(x=>x.id)).size,1);assert.equal(generated.filter(x=>!x.replayed).length,1);});
  const funded=generated[0];
  const balance=await rpc(admin,'credits_recompute',[inviter]);assert.equal(balance,500);
  const users=[randomUUID(),randomUUID()]; await q(admin,'INSERT INTO auth.users VALUES($1),($2)',users);
  const claims=await Promise.allSettled(users.map((u,i)=>q(racers[i],"INSERT INTO retailers(user_id,verification_status,referral_code,business_name) VALUES($1,'pending',$2,'New store') RETURNING id",[u,funded.code.toLowerCase()])));
  check('two concurrent onboarding claims consume one invitation',()=>assert.equal(claims.filter(x=>x.status==='fulfilled').length,1));
  const winner=claims.findIndex(x=>x.status==='fulfilled');const user=users[winner];const retailer=claims[winner].value.rows[0].id;
  assert.equal((await rpc(service,'claim_retailer_referral',[funded.code,user])).replayed,true);
  assert.equal((await rpc(service,'referral_cancel',[inviter,funded.id])).error,'ALREADY_ACCEPTED');
  check('pending retailers receive no credits',()=>{});
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_ledger WHERE reference_type IN ('invitation_gift','referral_bonus')")).rows[0].n,0);
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-10 06:00:00+00'");
  // Accepted promises survive the seven-day deadline; retries stay atomic.
  await Promise.all(racers.map(c=>rpc(c,'referral_admin_review',[retailer,'verified',null,randomUUID()])));
  check('approval after acceptance deadline grants exactly once',()=>{});
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_ledger WHERE reference_id=$1 AND kind='grant'",[funded.id])).rows[0].n,2);
  assert.equal(await rpc(admin,'credits_recompute',[user]),4500);
  assert.equal(await rpc(admin,'credits_recompute',[inviter]),3000);
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_lots WHERE source IN ('invitation_gift','referral_bonus') AND expires_at IS NOT NULL")).rows[0].n,0);
  // Fund across daily and bonus, then cancel after midnight.
  const mixed=await rpc(service,'referral_generate',[inviter,3500,randomUUID(),'ios']);
  assert.equal(mixed.ok,true);
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-11 06:00:00+00'");
  const release=await rpc(service,'referral_cancel',[inviter,mixed.id]);
  check('late cancellation returns bonus only, no expired daily resurrection',()=>assert.equal(release.refunded,500));
  assert.equal((await rpc(service,'referral_cancel',[inviter,mixed.id])).replayed,true);
  await rpc(admin,'credits_ensure_daily_budget',[inviter]); assert.equal(await rpc(admin,'credits_recompute',[inviter]),3000);
  const same=await rpc(service,'referral_generate',[inviter,1500,randomUUID(),'web']);
  assert.equal((await rpc(service,'referral_cancel',[inviter,same.id])).refunded,500);
  assert.equal(await rpc(admin,'credits_recompute',[inviter]),3000);check('same-day cancellation refunds the original lot once',()=>{});
  const rejected=await rpc(service,'referral_generate',[inviter,1500,randomUUID(),'web']);
  const ruser=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1)',[ruser]);
  const rid=(await q(admin,"INSERT INTO retailers(user_id,verification_status,referral_code) VALUES($1,'pending',$2) RETURNING id",[ruser,rejected.code])).rows[0].id;
  await rpc(service,'referral_admin_review',[rid,'rejected','Final rejection',randomUUID()]);
  assert.equal((await q(admin,'SELECT funding_state FROM referral_links WHERE id=$1',[rejected.id])).rows[0].funding_state,'released');
  await assert.rejects(()=>rpc(service,'referral_admin_review',[rid,'verified',null,randomUUID()]));
  check('final rejection releases funds and prevents later accidental rewards',()=>{});
  const fail=await rpc(service,'referral_generate',[inviter,1000,randomUUID(),'web']);
  const fuser=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1)',[fuser]);
  const fid=(await q(admin,"INSERT INTO retailers(user_id,verification_status,referral_code) VALUES($1,'pending',$2) RETURNING id",[fuser,fail.code])).rows[0].id;
  await q(admin,`CREATE FUNCTION fail_reward() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.reference_type='referral_bonus' THEN RAISE EXCEPTION 'Injected failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER fail_reward BEFORE INSERT ON credit_ledger FOR EACH ROW EXECUTE FUNCTION fail_reward();`);
  await assert.rejects(()=>rpc(service,'referral_admin_review',[fid,'verified',null,randomUUID()]));
  assert.equal((await q(admin,'SELECT verification_status FROM retailers WHERE id=$1',[fid])).rows[0].verification_status,'pending');
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_ledger WHERE reference_id=$1 AND kind='grant'",[fail.id])).rows[0].n,0);
  check('settlement failure rolls back verification and both grants',()=>{});
  await q(admin,'DROP TRIGGER fail_reward ON credit_ledger');
  await rpc(service,'referral_admin_review',[fid,'verified',null,randomUUID()]);
  const self=await rpc(service,'referral_generate',[inviter,1000,randomUUID(),'web']);
  await assert.rejects(()=>q(admin,"INSERT INTO retailers(user_id,verification_status,referral_code) VALUES($1,'pending',$2)",[inviter,self.code]),/SELF_REFERRAL/);
  check('self-referrals are rejected',()=>{});
  const expires=await rpc(service,'referral_generate',[inviter,1500,randomUUID(),'web']);
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-19 06:00:00+00'");
  await rpc(service,'referral_expire_due',[100]);
  assert.equal((await q(admin,'SELECT release_reason FROM referral_links WHERE id=$1',[expires.id])).rows[0].release_reason,'expired');
  check('unused expiry releases its reservation',()=>{});
  const client=await connect('authenticated');
  await assert.rejects(()=>rpc(client,'referral_generate',[inviter,1000,randomUUID(),'web']),e=>e.code==='42501');
  await assert.rejects(()=>rpc(client,'referral_settle',[fid,'forged']),e=>e.code==='42501');
  await assert.rejects(()=>q(client,"INSERT INTO referral_links(wholesaler_id,code) VALUES($1,'forged')",[ws]),e=>e.code==='42501');
  check('clients cannot create promises or mint/settle credits',()=>{});
  // Exercise the actual native INSERT/UPDATE guards with ownership RLS, not only service RPCs.
  await q(admin,'ALTER TABLE retailers ENABLE ROW LEVEL SECURITY; GRANT SELECT,INSERT,UPDATE ON retailers TO authenticated; CREATE POLICY own_retailer ON retailers TO authenticated USING(user_id=auth.uid()) WITH CHECK(user_id=auth.uid())');
  const nativeUser=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1)',[nativeUser]);
  const nativeInvite=await rpc(service,'referral_generate',[inviter,1000,randomUUID(),'ios']);
  await q(client,"SELECT set_config('request.jwt.claim.sub',$1,false)",[nativeUser]);
  const nativeRow=(await q(client,"INSERT INTO retailers(user_id,verification_status,referral_code,referred_by) VALUES($1,'verified',$2,$3) RETURNING *",[nativeUser,nativeInvite.code,randomUUID()])).rows[0];
  assert.equal(nativeRow.verification_status,'pending');assert.equal(nativeRow.referred_by,ws);
  const forgedUpdate=(await q(client,"UPDATE retailers SET verification_status='verified',referred_by=$1,referral_code='FORGED' WHERE id=$2 RETURNING *",[randomUUID(),nativeRow.id])).rows[0];
  assert.equal(forgedUpdate.verification_status,'pending');assert.equal(forgedUpdate.referred_by,ws);assert.equal(forgedUpdate.referral_code,nativeInvite.code);
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_ledger WHERE reference_id=$1 AND reference_type IN ('invitation_gift','referral_bonus')",[nativeInvite.id])).rows[0].n,0);
  check('native clients cannot verify themselves or change their pinned inviter',()=>{});
  await q(service,"UPDATE retailers SET verification_status='resubmission_required' WHERE id=$1",[nativeRow.id]);
  assert.equal((await q(service,'SELECT funding_state FROM referral_links WHERE id=$1',[nativeInvite.id])).rows[0].funding_state,'reserved');
  await q(service,"UPDATE retailers SET verification_status='pending',referral_code='FORGED' WHERE id=$1",[nativeRow.id]);
  assert.equal((await q(service,'SELECT referral_code FROM retailers WHERE id=$1',[nativeRow.id])).rows[0].referral_code,nativeInvite.code);
  await rpc(service,'referral_admin_review',[nativeRow.id,'verified',null,randomUUID()]);
  assert.equal((await q(service,'SELECT funding_state FROM referral_links WHERE id=$1',[nativeInvite.id])).rows[0].funding_state,'settled');
  assert.equal((await q(service,"SELECT count(*)::int AS n FROM credit_ledger WHERE reference_id=$1 AND kind='grant'",[nativeInvite.id])).rows[0].n,2);
  check('document resubmission preserves the promised gift until approval',()=>{});
  await q(client,"SELECT set_config('request.jwt.claim.sub',$1,false)",[user]);
  assert.equal((await q(client,'SELECT id FROM retailers WHERE id=$1',[nativeRow.id])).rowCount,0);
  assert.equal((await rpc(service,'referral_cancel',[user,nativeInvite.id])).error,'NOT_FOUND');
  const publicInvite=await rpc(service,'referral_generate',[inviter,1000,randomUUID(),'web']);
  const anonymous=await connect('anon');const projection=await rpc(anonymous,'validate_referral_code',[publicInvite.code.toLowerCase()]);
  assert.equal(projection.valid,true);assert.equal(projection.code,publicInvite.code);
  assert.equal('funding_ledger_id' in projection,false);assert.equal('inviter_user_id' in projection,false);assert.equal('generation_key' in projection,false);
  check('other accounts cannot read or cancel referrals; validation exposes only join details',()=>{});
  const delayed=await rpc(service,'referral_generate',[inviter,1000,randomUUID(),'web']);
  const delayedUser=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1)',[delayedUser]);
  const delayedRetailer=(await q(service,"INSERT INTO retailers(user_id,verification_status,referral_code) VALUES($1,'pending',$2) RETURNING id",[delayedUser,delayed.code])).rows[0].id;
  const locker=await connect('service_role');await q(locker,'BEGIN');await q(locker,'SELECT 1 FROM credit_accounts WHERE wholesaler_id=$1 FOR UPDATE',[inviter]);
  const reviewer=await connect('service_role');const pid=(await q(reviewer,'SELECT pg_backend_pid() AS pid')).rows[0].pid;
  const review=rpc(reviewer,'referral_admin_review',[delayedRetailer,'verified',null,randomUUID()]).then(value=>({value}),error=>({error}));
  let waiting=false;
  for(let attempt=0;attempt<100;attempt++){
    waiting=(await q(admin,"SELECT wait_event_type='Lock' AS waiting FROM pg_stat_activity WHERE pid=$1",[pid])).rows[0]?.waiting;
    if(waiting)break;await new Promise(resolve=>setTimeout(resolve,20));
  }
  assert.equal(waiting,true,'approval must be waiting for the wallet lock');
  await q(admin,"UPDATE wholesalers SET verification_status='pending' WHERE id=$1",[ws]);
  await q(locker,'COMMIT');const refused=await review;assert.match(refused.error?.message||'',/BONUS_ACCOUNT_NOT_ELIGIBLE/);
  assert.equal((await q(service,'SELECT verification_status FROM retailers WHERE id=$1',[delayedRetailer])).rows[0].verification_status,'pending');
  assert.equal((await q(service,"SELECT count(*)::int AS n FROM credit_ledger WHERE reference_id=$1 AND kind='grant'",[delayed.id])).rows[0].n,0);
  await q(admin,"UPDATE wholesalers SET verification_status='verified' WHERE id=$1",[ws]);
  check('inviter deactivation while approval waits rolls back both rewards and verification',()=>{});
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,false)",[user]);
  const wallet=await rpc(admin,'credits_wallet');assert.equal(wallet.bonus_available,2500);assert.equal(wallet.daily_available,2000);
  check('gift survives later daily reset and appears in wallet',()=>assert.equal(wallet.available,4500));
  await q(admin,"INSERT INTO credit_prices(feature_key,credits,label,audience) VALUES('test.bonus',3000,'Bonus spending','all')");
  const spend=await rpc(service,'spend_credits',[user,'test.bonus','bonus-test-spend',null,null,{}]);
  assert.equal(spend.charged,3000);assert.equal(spend.balance,1500);
  assert.equal((await rpc(service,'spend_credits',[user,'test.bonus','bonus-test-spend',null,null,{}])).replayed,true);
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-20 06:00:00+00'");
  const restore=await rpc(service,'refund_debit',[spend.ledger_id,'Failed work']);
  assert.equal(restore.refunded,1000);assert.equal((await rpc(service,'refund_debit',[spend.ledger_id,'Retry'])).replayed,true);
  await rpc(admin,'credits_ensure_daily_budget',[user]);assert.equal(await rpc(admin,'credits_recompute',[user]),4500);
  check('normal spending consumes daily before bonus; late work refund restores bonus once',()=>{});
  // Existing policy-zero invitations retain their terms and receive no new retailer gift.
  const legacyUser=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1)',[legacyUser]);
  await q(admin,"INSERT INTO referral_links(wholesaler_id,code,is_active,expires_at) VALUES($1,'LEGACY-CODE',true,jewel_test.clock()+interval '7 days')",[ws]);
  const legacyID=(await q(admin,"INSERT INTO retailers(user_id,verification_status,referral_code) VALUES($1,'pending','LEGACY-CODE') RETURNING id",[legacyUser])).rows[0].id;
  await rpc(service,'referral_admin_review',[legacyID,'verified',null,randomUUID()]);
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_lots WHERE account_id=$1 AND source='invitation_gift'",[legacyUser])).rows[0].n,0);
  check('legacy invitations are not retroactively given new gifts',()=>{});
  const adminReport=(await q(service,'SELECT status,gift_credits,extra_credits FROM referral_report WHERE id=$1',[funded.id])).rows[0];
  check('admin report matches gift and settlement',()=>assert.deepEqual(adminReport,{status:'rewarded',gift_credits:2500,extra_credits:1500}));
  const conflict=await rpc(service,'referral_generate',[inviter,1000,randomUUID(),'web']);
  const cuser=randomUUID();await q(admin,'INSERT INTO auth.users VALUES($1)',[cuser]);
  const cid=(await q(admin,"INSERT INTO retailers(user_id,verification_status,referral_code) VALUES($1,'pending',$2) RETURNING id",[cuser,conflict.code])).rows[0].id;
  await q(admin,"INSERT INTO credit_ledger(account_id,delta,kind,idempotency_key,balance_after) VALUES($1,0,'adjustment',$2,0)",[inviter,'referral_bonus:'+conflict.id]);
  await assert.rejects(()=>rpc(service,'referral_admin_review',[cid,'verified',null,randomUUID()]),/BONUS_LEDGER_CONFLICT/);
  assert.equal((await q(admin,"SELECT funding_state FROM referral_links WHERE id=$1",[conflict.id])).rows[0].funding_state,'reserved');
  check('a suppressed or conflicting ledger entry cannot count as a paid reward',()=>{});
  console.log(`${checks} referral accounting scenarios passed`);
} finally { await Promise.all(connections.map(c=>c.end().catch(()=>{})));await database.stop().catch(()=>{});await rm(testDir,{recursive:true,force:true}); }
