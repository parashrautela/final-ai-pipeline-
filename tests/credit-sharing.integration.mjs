// Disposable real PostgreSQL, including multiple independent connections.
// Runtime dependencies: embedded-postgres and pg (installed outside the application).
import assert from 'node:assert/strict';
import { randomUUID, randomBytes } from 'node:crypto';
import { mkdtemp, readFile } from 'node:fs/promises';
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

// Run BEFORE the daily-credit migration to prove the live repair is independent.
async function checkIndependentHistory(admin) {
  await q(admin,'BEGIN');
  const owner = randomUUID(), staff = randomUUID(), stranger = randomUUID(), wholesaler = randomUUID();
  await q(admin,'INSERT INTO auth.users VALUES($1),($2),($3),($4)',[owner,staff,stranger,wholesaler]);
  const store = (await q(admin,"INSERT INTO retailers(user_id,verification_status) VALUES($1,'verified') RETURNING id",[owner])).rows[0].id;
  await q(admin,"INSERT INTO retailers(user_id,verification_status) VALUES($1,'verified')",[stranger]);
  await q(admin,"INSERT INTO wholesalers(user_id,verification_status) VALUES($1,'verified')",[wholesaler]);
  await q(admin,"INSERT INTO employees(auth_user_id,retailer_id,status) VALUES($1,$2,'active')",[staff,store]);
  for (const id of [owner,stranger,wholesaler]) await rpc(admin,'credits_ensure_account',[id]);
  const ids = [randomUUID(),randomUUID()].sort().reverse();
  for (const id of ids) await q(admin,"INSERT INTO credit_ledger(id,account_id,delta,kind,balance_after,created_at) VALUES($1,$2,-10,'debit',100,'2026-10-01 12:00:00+00')",[id,owner]);
  await q(admin,"INSERT INTO credit_ledger(account_id,delta,kind,balance_after) VALUES($1,-20,'debit',80),($2,-30,'debit',70)",[stranger,wholesaler]);
  const before = (await q(admin,"SELECT jsonb_agg(to_jsonb(a) ORDER BY wholesaler_id) AS state FROM credit_accounts a")).rows[0].state;
  await q(admin,'SET LOCAL ROLE authenticated');
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,true)",[owner]);
  const ownerPage = await rpc(admin,'credits_history',[1,0,'debit']);
  check('history works with the existing wallet before daily mode',()=>{assert.equal(ownerPage.ok,true);assert.equal(ownerPage.count,2);assert.equal(ownerPage.data[0].id,ids[0]);});
  const next = await rpc(admin,'credits_history',[1,0,'debit',ownerPage.data[0].created_at,ids[0]]);
  check('history cursor keeps equal-timestamp entries without duplicates',()=>assert.equal(next.data[0].id,ids[1]));
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,true)",[staff]);
  const staffPage = await rpc(admin,'credits_history',[100,0,'debit']);
  check('active staff history is limited to the business ledger',()=>assert.deepEqual(staffPage.data.map(r=>r.id),ids));
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,true)",[wholesaler]);
  const wholePage = await rpc(admin,'credits_history',[10,0,'debit']);
  check('wholesaler history is isolated from retailer wallets',()=>{assert.equal(wholePage.data.length,1);assert.equal(wholePage.data[0].delta,-30);});
  await q(admin,'RESET ROLE');
  await q(admin,"UPDATE employees SET status='inactive' WHERE auth_user_id=$1",[staff]);
  await q(admin,'SET LOCAL ROLE authenticated');
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,true)",[staff]);
  check('inactive staff cannot read business history',()=>{});
  assert.equal((await rpc(admin,'credits_history')).error,'NOT_VERIFIED');
  await q(admin,'RESET ROLE');
  await q(admin,"UPDATE retailers SET verification_status='banned' WHERE id=$1",[store]);
  await q(admin,'SET LOCAL ROLE authenticated');
  await q(admin,"SELECT set_config('request.jwt.claim.sub',$1,true)",[owner]);
  assert.equal((await rpc(admin,'credits_history')).error,'NOT_VERIFIED');
  check('suspended owners cannot read history',()=>{});
  await q(admin,'RESET ROLE');
  const after = (await q(admin,"SELECT jsonb_agg(to_jsonb(a) ORDER BY wholesaler_id) AS state FROM credit_accounts a")).rows[0].state;
  check('history reads do not alter credit balances',()=>assert.deepEqual(after,before));
  await q(admin,'SAVEPOINT anonymous_check');
  await q(admin,'SET LOCAL ROLE anon');
  await assert.rejects(()=>rpc(admin,'credits_history'),error=>error.code==='42501');
  await q(admin,'ROLLBACK TO SAVEPOINT anonymous_check');
  check('anonymous callers cannot read any credit history',()=>{});
  await q(admin,'ROLLBACK');
}

try {
  await database.initialise(); await database.start();
  const admin = await connect();
  await admin.query(`
    CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE sql STABLE AS
      $$ SELECT nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
    CREATE TABLE public.wholesalers(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),user_id UUID REFERENCES auth.users,
      verification_status TEXT,full_name TEXT,business_name TEXT,phone TEXT,email TEXT,business_logo_url TEXT);
    CREATE TABLE public.retailers(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),user_id UUID REFERENCES auth.users,
      verification_status TEXT,full_name TEXT,business_name TEXT,selected_theme TEXT,referred_by UUID,referral_code TEXT,
      rejection_reason TEXT,notification_message TEXT,notified BOOLEAN);
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
  if(process.env.JEWEL_REFERRAL_MIGRATION==='1') await admin.query(`CREATE TABLE referral_links(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),wholesaler_id UUID REFERENCES wholesalers(id),code TEXT UNIQUE,uses_count INT DEFAULT 0,max_uses INT DEFAULT 1,is_active BOOLEAN DEFAULT true,created_at TIMESTAMPTZ DEFAULT now());`);
  const migrations = [
    'ai-pipeline/migrations/004a_tables_and_rls.sql', 'ai-pipeline/migrations/004b_functions.sql',
    'ai-pipeline/migrations/004c_trigger_seed_grants.sql', 'ai-pipeline/migrations/006_razorpay_purchases.sql',
    'ai-pipeline/migrations/007_rupee_denominated_credits.sql', 'ai-pipeline/migrations/008_welcome_grant_2000.sql',
    'wholesaler ios/supabase/migrations/20260919_01_retailer_wallet_entitlements.sql',
    'wholesaler ios/supabase/migrations/20260919_02_customer_wishlists.sql',
    'wholesaler ios/supabase/migrations/20260919_03_retailer_plans.sql',
    'wholesaler ios/supabase/migrations/20260926_01_apple_iap_credits.sql',
    'ai-pipeline/migrations/014_wishlist_sharing.sql', 'ai-pipeline/migrations/016_credit_history_rpc.sql',
    'ai-pipeline/migrations/015_daily_credit_program.sql',
    ...(process.env.JEWEL_REFERRAL_MIGRATION==='1' ? ['ai-pipeline/migrations/012_retailer_referrals.sql','ai-pipeline/migrations/017_invitation_gifts.sql','ai-pipeline/migrations/018_preserve_purchased_credits.sql'] : []),
  ];
  for (const file of migrations) {
    const sql = await readFile(path.join(workspace, file), 'utf8');
    // Boundary tests use one shared controlled clock. All other SQL/locking is unchanged.
    await admin.query(sql.replaceAll('clock_timestamp()', 'jewel_test.clock()'));
    console.log(`APPLIED ${path.basename(file)}`);
    if (file.endsWith('016_credit_history_rpc.sql')) await checkIndependentHistory(admin);
  }
  await admin.query('GRANT ALL ON ALL TABLES IN SCHEMA public,auth TO service_role');
  const owner = randomUUID(), employee = randomUUID(), stranger = randomUUID(), wholesaler = randomUUID();
  await q(admin, 'INSERT INTO auth.users VALUES($1),($2),($3),($4)', [owner, employee, stranger, wholesaler]);
  const store = (await q(admin, "INSERT INTO retailers(user_id,verification_status,business_name) VALUES($1,'verified','Test store') RETURNING id", [owner])).rows[0].id;
  const otherStore = (await q(admin, "INSERT INTO retailers(user_id,verification_status,business_name) VALUES($1,'verified','Other store') RETURNING id", [stranger])).rows[0].id;
  await q(admin, "INSERT INTO employees(auth_user_id,retailer_id,status) VALUES($1,$2,'active')", [employee,store]);
  await q(admin, "INSERT INTO wholesalers(user_id,verification_status) VALUES($1,'verified')", [wholesaler]);
  const customer = (await q(admin, 'INSERT INTO retailer_customers(retailer_id,name,phone,note) VALUES($1,$2,$3,$4) RETURNING id', [store,'Private customer','private phone','private notes'])).rows[0].id;
  const board = (await q(admin, 'SELECT id FROM customer_boards WHERE customer_id=$1', [customer])).rows[0].id;
  const product = (await q(admin, "INSERT INTO products(title,jewellery_type,net_weight,is_published,processed_image_url,raw_image_url,wholesaler_email) VALUES('Necklace','necklace',20,true,'https://example.com/display.jpg','https://example.com/private-original.jpg','private@example.com') RETURNING id")).rows[0].id;
  await q(admin, 'INSERT INTO customer_board_items(board_id,product_id,retailer_id) VALUES($1,$2,$3)', [board,product,store]);
  const service = await connect('service_role');
  const token = hash();
  const share = await rpc(service,'wishlist_share_create',[employee,board,token,1,60]);
  check('active staff can share their store board', () => assert.equal(share.ok,true));
  check('another store cannot share this board', () => {});
  assert.equal((await rpc(service,'wishlist_share_create',[stranger,board,hash(),1,60])).error,'NOT_FOUND');
  const racers = await Promise.all(Array.from({length: 12}, () => connect('service_role')));
  const browsers = racers.map(() => hash()), sessions = racers.map(() => hash());
  const claims = await Promise.all(racers.map((client,i) => rpc(client,'wishlist_share_claim',[token,browsers[i],sessions[i]])));
  const winner = claims.findIndex(r => r.ok);
  check('12 simultaneous claims consume exactly one slot', () => { assert.equal(claims.filter(r => r.ok).length,1); assert.equal(claims.filter(r => r.error === 'FULL').length,11); });
  const content = await rpc(service,'wishlist_share_content',[sessions[winner]]);
  check('an admitted session works after the link becomes full', () => assert.equal(content.products.length,1));
  check('public projection excludes customer contacts and raw media', () => {
    const json = JSON.stringify(content); assert(!json.includes('private')); assert(!json.includes('customer_id')); assert(!json.includes('wholesaler_email'));
  });
  const replacement = hash();
  const retry = await rpc(service,'wishlist_share_claim',[token,browsers[winner],replacement]);
  check('response-loss retry reuses the admission and deadline', () => { assert.equal(retry.ok,true); assert.equal(retry.expires_at,claims[winner].expires_at); });
  assert.equal((await q(admin,'SELECT views_used FROM wishlist_shares WHERE id=$1',[share.id])).rows[0].views_used,1);
  assert.equal((await rpc(service,'wishlist_share_revoke',[stranger,share.id])).error,'NOT_FOUND');
  await rpc(service,'wishlist_share_revoke',[owner,share.id]);
  check('revocation blocks the admitted session', () => {});
  assert.equal((await rpc(service,'wishlist_share_content',[replacement])).error,'UNAVAILABLE');
  const sessionToken = hash(), browser = hash(), linkToken = hash();
  const expiringShare = await rpc(service,'wishlist_share_create',[owner,board,linkToken,2,60]);
  await rpc(service,'wishlist_share_claim',[linkToken,browser,sessionToken]);
  const laterProduct = (await q(admin,"INSERT INTO products(title,is_published) VALUES('Later design',true) RETURNING id")).rows[0].id;
  await q(admin,'INSERT INTO customer_board_items VALUES($1,$2,$3,now())',[board,laterProduct,store]);
  check('share membership excludes later board additions', () => {});
  assert.equal((await rpc(service,'wishlist_share_content',[sessionToken])).products.length,1);
  await q(admin,"UPDATE wishlist_share_sessions SET expires_at = '2026-10-01 18:28:00+00' WHERE share_id=$1",[expiringShare.id]);
  assert.equal((await rpc(service,'wishlist_share_claim',[linkToken,browser,hash()])).error,'SESSION_ENDED');
  check('a finished session cannot claim another admission', () => {});

  assert.equal((await rpc(service,'wishlist_share_create',[owner,board,hash(),0,60])).error,'INVALID_SETTINGS');
  assert.equal((await rpc(service,'wishlist_share_create',[owner,board,hash(),1,10081])).error,'INVALID_SETTINGS');
  check('viewer and expiry bounds are enforced by the database', () => {});
  const adminToken = hash(),adminSession = hash();
  const adminShare = await rpc(service,'wishlist_share_create',[owner,board,adminToken,3,5]);
  await rpc(service,'wishlist_share_claim',[adminToken,hash(),adminSession]);
  await rpc(service,'wishlist_share_admin_revoke',[adminShare.id]);
  assert.equal((await rpc(service,'wishlist_share_content',[adminSession])).error,'UNAVAILABLE');
  check('admin revocation blocks an already admitted session', () => {});
  const suspendedToken = hash(),suspendedSession = hash();
  await rpc(service,'wishlist_share_create',[owner,board,suspendedToken,2,60]);
  await rpc(service,'wishlist_share_claim',[suspendedToken,hash(),suspendedSession]);
  await q(admin,"UPDATE retailers SET verification_status='banned' WHERE id=$1",[store]);
  assert.equal((await rpc(service,'wishlist_share_content',[suspendedSession])).error,'UNAVAILABLE');
  assert.equal((await rpc(service,'wishlist_share_claim',[suspendedToken,hash(),hash()])).error,'UNAVAILABLE');
  await q(admin,"UPDATE retailers SET verification_status='verified' WHERE id=$1",[store]);
  check('store suspension denies new and admitted viewers', () => {});
  await q(admin,"UPDATE wishlist_shares SET expires_at='2026-10-01 18:28:00+00' WHERE token_hash=$1",[suspendedToken]);
  assert.equal((await rpc(service,'wishlist_share_claim',[suspendedToken,hash(),hash()])).error,'EXPIRED');
  assert.equal((await rpc(service,'wishlist_share_content',[suspendedSession])).error,'EXPIRED');
  check('link expiry blocks both admissions and existing sessions', () => {});

  // A real paid balance must stop conversion. A provider refund settles it first.
  const purchased = await rpc(service,'record_apple_credit_purchase',[wholesaler,'legacy-test','com.jewelindia.credits.starter',{productId:'com.jewelindia.credits.starter',purchaseDate:1}]);
  assert.equal(purchased.ok,true);
  assert.equal((await rpc(service,'credits_activate_daily')).error,'LEGACY_PAID_BALANCES_REQUIRE_DECISION');
  check('activation refuses outstanding purchased credits', () => {});
  assert.equal((await rpc(service,'record_apple_credit_refund',['legacy-test',{}])).ok,true);
  assert.equal((await rpc(service,'credits_activate_daily')).ok,true);
  const lateApple = await rpc(service,'record_apple_credit_purchase',[wholesaler,'after-cutoff','com.jewelindia.credits.starter',{purchaseDate:Date.parse('2026-10-03T00:00:00Z')}]);
  assert.equal(lateApple.error,'PAYMENTS_DISABLED');
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_purchase_issues WHERE razorpay_payment_id='apple:after-cutoff'")).rows[0].n,1);
  check('new Apple payments are refused and recorded for settlement', () => {});
  const delayedApple = await rpc(service,'record_apple_credit_purchase',[wholesaler,'old-delayed','com.jewelindia.credits.starter',{purchaseDate:1}]);
  assert.equal(delayedApple.ok,true);
  assert.equal((await rpc(service,'credits_ensure_daily_budget',[wholesaler])).balance,2000);
  await rpc(service,'record_apple_credit_refund',['old-delayed',{}]);
  assert.equal((await rpc(service,'credits_ensure_daily_budget',[wholesaler])).balance,2000);
  check('late purchased-credit settlement and refund do not alter the daily allowance', () => {});
  const lateRazorpay = await rpc(service,'record_razorpay_purchase',[wholesaler,'late-razorpay',5000,'starter',500,90,'old-link',null,null,{}]);
  assert.equal(lateRazorpay.ok,true);
  assert.equal((await rpc(service,'credits_ensure_daily_budget',[wholesaler])).balance,2000);
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_purchase_issues WHERE razorpay_payment_id='late-razorpay' AND resolved_at IS NULL")).rows[0].n,1);
  check('late Razorpay settlement preserves the receipt and opens manual handling', () => {});
  const authed = await connect('authenticated');
  await q(authed,"SELECT set_config('request.jwt.claim.sub',$1,false)",[employee]);
  const wallet = await rpc(authed,'credits_wallet');
  check('staff receive the owner wallet with one daily allowance', () => { assert.equal(wallet.available,2000); assert.equal(wallet.shared_business_wallet,true); assert.equal(Date.parse(wallet.resets_at),Date.parse('2026-10-01T18:30:00+00:00')); });
  const spends = await Promise.all(racers.map((client,i) => rpc(client,'spend_credits',[i % 2 ? employee : owner,'chamak.generate',`race-${i}`,'chamak_generation',`job-${i}`,{}])));
  check('concurrent owner/staff spends stop at the shared allowance', () => { assert.equal(spends.filter(r => r.ok).length,10); assert.equal(spends.filter(r => r.error === 'INSUFFICIENT_CREDITS').length,2); });
  const charged = spends.find(r => r.ok);
  const refunds = await Promise.all(racers.slice(0,5).map(client => rpc(client,'refund_debit',[charged.ledger_id,'Failed test job'])));
  check('concurrent refund callbacks restore one exact debit', () => assert.equal(refunds.reduce((sum,r) => sum + r.refunded,0),200));
  assert.equal((await rpc(authed,'credits_wallet')).available,200);
  const liveDebit = spends.find(r => r.ok && r.ledger_id !== charged.ledger_id);
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-01 18:30:01+00'");
  const midnightWallets = await Promise.all(racers.map(client => rpc(client,'credits_ensure_daily_budget',[owner])));
  check('midnight reset creates one allowance, not leftover plus 2,000', () => assert(midnightWallets.every(r => r.balance === 2000)));
  assert.equal((await q(admin,"SELECT count(*)::int AS n FROM credit_lots WHERE account_id=$1 AND source='daily' AND budget_date='2026-10-02'",[owner])).rows[0].n,1);
  const late = await rpc(service,'refund_debit',[liveDebit.ledger_id,'Late failure']);
  check('late failure reverses the debit without inflating today', () => { assert.equal(late.refunded,0); assert.equal(late.reversed_units,200); assert.equal(late.balance,2000); });
  const originalIndex = spends.findIndex(r => r.ledger_id === liveDebit.ledger_id);
  const replay = await rpc(service,'spend_credits',[originalIndex % 2 ? employee : owner,'chamak.generate',`race-${originalIndex}`,'chamak_generation',`job-${originalIndex}`,{}]);
  check('late reversal still marks the original attempt refunded', () => { assert.equal(replay.refunded,true); assert.equal(replay.replayed,true); });
  assert.equal((await rpc(service,'grant_credits',[owner,1000,'referral','extra-referral'])).granted,0);
  assert.equal((await rpc(authed,'credits_wallet')).available,2000);
  check('referral rewards cannot stack onto the daily allowance', () => {});
  assert.equal((await rpc(authed,'subscribe_plan',['monthly'])).error,'PLANS_PAUSED');
  const plan = await rpc(authed,'my_plan');
  check('verified retailer plan access is temporary and never renews', () => { assert.equal(plan.active,true); assert.equal(plan.temporary_access,true); assert.equal(plan.renewed_now,false); });
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-10 18:30:01+00'");
  assert.equal((await rpc(authed,'credits_wallet')).available,2000);
  check('inactivity gives only today, without missed-day grants', () => {});
  const history = await rpc(authed,'credits_history',[50,0,null]);
  check('staff can read their business ledger safely', () => assert(history.data.length > 0));
  // Force a spend to wait at midnight, then move the shared clock before unlocking.
  const locker = await connect(),blocked = await connect('service_role');
  const blockedPID = (await q(blocked,'SELECT pg_backend_pid() AS pid')).rows[0].pid;
  async function waitForAccountLock() {
    for (let attempt=0;attempt<100;attempt++) {
      if ((await q(admin,"SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted",[blockedPID])).rowCount) return;
      await new Promise(resolve => setTimeout(resolve,10));
    }
    throw new Error('Expected the request to wait for the account lock');
  }
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-11 18:29:59+00'");
  await q(locker,'BEGIN');
  await q(locker,'SELECT 1 FROM credit_accounts WHERE wholesaler_id=$1 FOR UPDATE',[owner]);
  const waiting = rpc(blocked,'spend_credits',[employee,'chamak.generate','midnight-wait','chamak_generation','midnight-wait',{}]);
  await waitForAccountLock();
  await q(admin,"UPDATE jewel_test.time SET value='2026-10-11 18:30:01+00'");
  await q(locker,'COMMIT');
  assert.equal((await waiting).balance,1800);
  assert.equal((await q(admin,"SELECT budget_date::text AS day FROM credit_lots WHERE id=(SELECT (metadata->'allocations'->0->>'lot_id')::uuid FROM credit_ledger WHERE idempotency_key='midnight-wait')")).rows[0].day,'2026-10-12');
  check('a request waiting across midnight charges the new day', () => {});
  await q(locker,'BEGIN');
  await q(locker,'SELECT 1 FROM credit_accounts WHERE wholesaler_id=$1 FOR UPDATE',[owner]);
  const deactivating = rpc(blocked,'spend_credits',[employee,'chamak.generate','staff-wait','chamak_generation','staff-wait',{}]);
  await waitForAccountLock();
  await q(admin,"UPDATE employees SET status='inactive' WHERE auth_user_id=$1",[employee]);
  await q(locker,'COMMIT');
  assert.equal((await deactivating).error,'NOT_VERIFIED');
  assert.equal((await rpc(service,'credits_ensure_daily_budget',[owner])).balance,1800);
  check('staff deactivated while waiting cannot debit the store', () => {});
  await q(admin,"UPDATE employees SET status='inactive' WHERE auth_user_id=$1",[employee]);
  assert.equal((await rpc(authed,'credits_wallet')).error,'NOT_VERIFIED');
  assert.equal((await rpc(service,'spend_credits',[employee,'chamak.generate','inactive','chamak_generation','inactive',{}])).error,'NOT_VERIFIED');
  check('deactivated staff lose wallet and spending access', () => {});
  const guest = await connect('anon');
  await assert.rejects(rpc(guest,'wishlist_share_claim',[hash(),hash(),hash()]), /permission denied/);
  await assert.rejects(rpc(authed,'grant_credits',[owner,1000,'admin','tampered']), /permission denied/);
  await assert.rejects(rpc(service,'spend_credits_legacy',[owner,'chamak.generate','legacy-backdoor']), /permission denied/);
  check('anonymous/direct-client and legacy-function bypasses are denied', () => {});
  console.log(`\n${checks} integration checks passed using real PostgreSQL.`);
} finally {
  await Promise.allSettled(connections.map(client => client.end()));
  await database.stop().catch(() => {});
}
