# Jewel India: wishlist sharing and daily credits

Prepared 1 October 2026. Status: implementation proposal; no application code, database, payment provider, or deployment has been changed.

## 1. Proposed outcome

Let a store share a customer wishlist through the Jewel India web app. Each new link has its own recipient allowance and expiry. The user confirmed one viewing session per admitted recipient and anonymous access. Use an explicit “Open wishlist” action that consumes a slot. A link with an allowance of one is the literal single-use case; an allowance of five permits five admissions. Sessions remain usable for a short viewing window so refreshes and image requests do not consume extra slots.

Replace purchasing credits with a daily allowance of exactly 2,000 credits, reset at midnight Asia/Kolkata. Unused credits do not accumulate. Features continue to consume credits at the server's rate card. Remove money checkout, top-up, and payment prompts from current releases and reject new payment creation on the server.

The user confirmed this allowance for both retailer and wholesaler businesses, with staff sharing their business's balance. Recommend eligibility for verified businesses. Active retail staff share their store owner's wallet rather than receiving independent grants. This staff-wallet mapping is an explicit addition, not an assumption about existing behavior.

Confirmed decisions: anonymous access, one viewing session per recipient, and both business roles with shared staff balances. The midnight India reset was the stated planning assumption; other proposed limits and transition rules below still require agreement before the relevant production changes.

## 2. What the local code establishes

This review examined source and migrations, not the deployed database or provider dashboards. Before execution, confirm the actual migration state, active prices, payment traffic, and deployed function versions.

| Area | Existing implementation | Consequence |
| --- | --- | --- |
| Admin web | `Admin-Panel-for-jewel-India-/src/app/routes.tsx` mainly routes admin verification/review and status screens | The share creator belongs in the business app; admin needs monitoring/revocation controls |
| Business web | `Jewel-India-Frontend` is a Next.js app; “Your Taste” operates on store selections | Do not silently reinterpret the store shortlist as a customer's wishlist |
| Customer wishlists | iOS `WishlistAPI.swift` uses `retailer_customers`, `customer_boards`, and `customer_board_items` | Reuse these tables for customer boards; a corresponding web wishlist UI was not found in the inspected source |
| Store access | Wishlist policies use `my_retailer_id()` for store owner and active staff | Reuse membership checks and strengthen them with current verification/suspension checks |
| Credits | `credit_accounts`, `credit_lots`, `credit_ledger`, `credit_prices`; database RPCs handle spend/grant/refund | Extend this accounting rather than replacing it with a client-side daily counter |
| Latest refund behavior | Pipeline migration `006_razorpay_purchases.sql` introduces `refund_debit`, replay safeguards, and refunds per debit | Preserve these safeguards; daily credits require new expiry behavior |
| Welcome/referrals | A 2,000-credit welcome grant and a 1,000-credit referral reward already exist | Their spendable grants must be gated in daily mode to prevent balances above the allowance |
| Money purchases | Razorpay functions in admin and iOS repos; Apple IAP functions, tables, and StoreKit UI in iOS | Hiding the web top-up button alone would leave purchase paths active |
| Onboarding | Fee infrastructure remains; the current iOS `submitOrPay()` directly submits | Preserve fee-free onboarding and disable the residual creation endpoints |
| Retailer plans | Credit-priced monthly/quarterly/yearly plans cost 3,000 / 8,000 / 30,000; `my_plan()` can auto-renew | Daily credits cannot accumulate enough to buy these plans; automatic charging needs an explicit policy |
| Media/cache | Many product assets are public; web PWA uses aggressive navigation caching | Wishlist expiry requires cache exclusions; strict image access requires private media delivery |

Primary source entry points:

- [Wishlist schema](</Users/parashrautela/Documents/jewel india /wholesaler ios/supabase/migrations/20260919_02_customer_wishlists.sql>) and [Wishlist API](</Users/parashrautela/Documents/jewel india /wholesaler ios/JewelIndia/Sources/Networking/WishlistAPI.swift>).
- [Web shortlist](</Users/parashrautela/Documents/jewel india /Jewel-India-Frontend/components/retailer/YourTasteClient.jsx>) and [web credit state](</Users/parashrautela/Documents/jewel india /Jewel-India-Frontend/context/CreditsContext.jsx>).
- [Credit schema](</Users/parashrautela/Documents/jewel india /ai-pipeline/migrations/004a_tables_and_rls.sql>), [latest spend/refund migration](</Users/parashrautela/Documents/jewel india /ai-pipeline/migrations/006_razorpay_purchases.sql>), and [pipeline billing calls](</Users/parashrautela/Documents/jewel india /ai-pipeline/app/main.py>).
- [Retailer plans](</Users/parashrautela/Documents/jewel india /wholesaler ios/supabase/migrations/20260919_03_retailer_plans.sql>), [onboarding payment schema](</Users/parashrautela/Documents/jewel india /wholesaler ios/supabase/migrations/20260921_11_onboarding_payments.sql>), and [Apple purchase schema](</Users/parashrautela/Documents/jewel india /wholesaler ios/supabase/migrations/20260926_01_apple_iap_credits.sql>).

## 3. Wishlist sharing: product contract

### Creator experience

On a customer board, show “Share wishlist.” If the user starts from a product detail, resolve or let them select the customer board first; share that board rather than accidentally exposing an entire catalogue or all of a customer's boards.

The share dialog includes:

- Maximum recipients: integer input, default 1; proposed v1 bounds 1–100.
- Link validity: presets of 1 hour, 24 hours, and 7 days plus custom duration; proposed bounds 5 minutes–7 days. Show the exact expiry in India time.
- A clear rule: “Each recipient gets one viewing session. Opening the wishlist uses a slot.”
- A preview of the public title, store branding, and included designs. Default title is generic; customer identity is private unless explicitly chosen for display.
- “Create link,” then “Copy link.” Use the device share sheet where supported; copying or invoking the share sheet does not automatically send messages.

Creators can inspect links, remaining admissions, expiry, and status, and revoke a link immediately. Keep allowances and expiry immutable in v1. To change them, revoke and generate a fresh link; generating a new independent share does not silently revoke another active share. Never recycle a token after expiry or exhaustion.

Empty boards cannot be shared. Deleted boards or customers invalidate their shares. Suspending a store blocks both new shares and subsequent access to existing ones. Removing the employee who created a share does not transfer ownership: the share remains store-owned and the store owner can revoke it.

### Recipient experience and the meaning of “one-time”

An unauthenticated recipient opens a public route such as `/share/wishlist#<random-token>`. A URL fragment is recommended because it stays out of the initial HTTP request and ordinary URL request logs. The landing page reads it and submits it in the claim POST. Scrub the token from the address bar after successful admission; use the viewing-session cookie for subsequent requests.

The landing page has generic branding, expiry information when safe to disclose, and “Open wishlist.” It does not expose designs or customer information before a successful claim. A GET, HEAD, social link preview, browser prefetch, or crawler visit must not consume a slot. Only the explicit claim POST does. This prevents WhatsApp previews from spending the sole admission. Automated POSTs remain possible, so apply rate limits and add a challenge only if abuse warrants it.

After admission, show a mobile-friendly, read-only product grid and safe product details. No orders, board edits, marketplace search, raw-image download, customer phone, private notes, wholesaler contact data, or AI generation controls are included.

Proposed viewing window: 30 minutes from admission, ending sooner if the link expires. Refreshing the same active session works without consuming another admission. Once that session ends, its admission is spent. Closing a tab cannot reliably terminate server access, so describe this as a timed viewing session rather than promising browser-close enforcement.

When the final slot is consumed, reject new admissions but allow previously admitted sessions to continue until their session deadline. Link expiry, owner revocation, board/customer deletion, or store suspension ends all sessions. Return clear public states for unavailable, expired, full, and session-ended links without revealing another store's records.

Anonymous access counts browser admissions, not proven human identities. A shared device can represent multiple people; clearing cookies or changing devices can represent the same person again and spend another slot. Do not use IP addresses or fingerprinting as identity. The user accepted anonymous access after this distinction was presented. Describe the allowance accurately in product copy. A future distinct-account mode could use sign-in and unique `(share_id, auth_user_id)` admissions; it is outside this version's confirmed scope.

### Content behavior

Recommend a snapshot of the board's product membership at link creation: later additions do not unexpectedly appear in an already-shared list. Render only those captured products that remain published and eligible for customer display. Product descriptions/stock can remain current; this is a membership snapshot, not a promise to freeze every product field or image byte.

If all captured products become unavailable, show an unavailable state and do not consume a new admission. The creator must regenerate a link to include newly added designs. If sharing the store's “Your Taste” shortlist is also desired, treat that as a separate share source with its own authorization check after customer-board sharing is working.

## 4. Wishlist sharing: backend design

### Proposed tables

| Table | Main fields and constraints |
| --- | --- |
| `wishlist_shares` | UUID, retailer ID, board ID, creator auth ID, unique token hash, public title, maximum admissions, admissions count, created time, expiry, revocation time; enforce `0 <= count <= maximum` |
| `wishlist_share_items` | Share ID, product ID, ordering; unique share/product pair; populated from the authorized board in the creation transaction |
| `wishlist_share_sessions` | UUID, share ID, unique session-token hash, browser-claim hash or authenticated viewer ID, admitted time, expiry, ended/revoked time; unique identity per share |
| `wishlist_share_events` | Share ID, actor/session references, event type, timestamp, minimal metadata; no raw bearer tokens or customer contact data |

Use database-generated timestamps and server-generated cryptographically random tokens of at least 32 bytes. Store token/session hashes, return the raw link token only once at creation, and never log it. The creator history does not reconstruct it from its hash; if the link is lost, create a replacement. A later requirement to recopy old links would need encrypted token storage and a deliberate key-management choice.

Indexes: unique token and session hashes; `(retailer_id, created_at)` for management; share/expiry for cleanup; share/viewer uniqueness for retries. Enforce the same store for share and board with a checked relationship or database constraint, not an unchecked retailer ID supplied by the client. Prevent direct client edits to counters, tokens, session deadlines, snapshots, or revocation history.

Keep the underlying wishlist tables private. Owners/staff may list management metadata under RLS; create/revoke go through audited authorized routines. Public recipients never receive a generic Supabase query against customer or product tables. The public gateway validates the capability and returns an explicit allowlist of fields. A service-role client bypasses RLS, so the gateway must enforce store, board, token, session, and product eligibility itself; never expose that key in the browser. [Supabase RLS documentation](https://supabase.com/docs/guides/database/postgres/row-level-security).

### Atomic claim and retries

The server establishes a random browser-claim cookie before claiming; sign-in mode uses the account ID instead. Claim creation is one database transaction:

1. Hash the supplied bearer token and lock the matching share row.
2. Recheck current time after the lock, expiry, revocation, store eligibility, source existence, and whether shareable products remain.
3. Check for an existing admission for this browser/account. If active, reissue/rotate its session cookie under the same admission; if ended, reject in one-session mode. This makes retries safe even if the response containing the first session cookie was lost.
4. If no existing admission, enforce `count < maximum`, increment once, and insert the session together.
5. Commit; set a secure, HttpOnly, same-site viewing cookie scoped to the public share routes. Do not use the business user's auth cookie as the share capability.

Database row locking provides the concurrency primitive; the implementation must put both counter update and session creation behind it. A frontend counter cannot enforce the last available slot. [PostgreSQL locking documentation](https://www.postgresql.org/docs/current/explicit-locking.html).

Every content and protected-media request checks the session and its parent share. Check elapsed deadlines on reads; cleanup jobs only remove old records and never define whether a link is valid. Use the same share-first lock order for claim/revoke mutations and bounded transaction retries for serialization/deadlock failures.

### API boundaries

Proposed Next.js business-app endpoints:

- `POST /api/wishlist-shares`: authenticated creation from `board_id`, recipient allowance, and duration; server resolves retailer and authorized board. Protect against CSRF/cross-origin mutation.
- `GET /api/wishlist-shares?board_id=...`: authorized management metadata only.
- `POST /api/wishlist-shares/{id}/revoke`: store owner, authorized active staff, or audited admin; idempotent.
- `POST /api/shared-wishlist/claim`: bearer link token plus browser/account identity; rate-limited, atomic admission.
- `GET /api/shared-wishlist`: session-authorized safe content; explicit share selection if a browser has multiple active sessions.
- `GET /api/shared-wishlist/media/{item}`: optional private-media gateway; verify captured membership and current access before serving bytes.

An iOS share-creation client can reuse the creation service with its Supabase bearer token. Ensure that creation supports the appropriate authentication transport with the same authorization checks. Keep the recipient route opening in the web app; verify existing universal-link rules do not intercept it unexpectedly.

Cache policy: explicit private/no-store responses, no search indexing, no referrer leakage, no sensitive data in Open Graph tags or telemetry, and PWA service-worker exclusions for share routes, content APIs, and protected media. Also disable accidental framework/CDN caching and clear rendered private content on expiry or restore from browser back/forward cache. Framework caching behavior does not replace an explicit share-response policy. [Next.js Route Handlers](https://nextjs.org/docs/app/getting-started/route-handlers).

### Media access decision

Basic scope: enforce limited access to the wishlist page/API and acknowledge that already-public product images remain accessible by URL. The current `ProtectedImage` presentation and right-click restrictions cannot revoke that access.

Stronger scope: copy approved display images into a private share bucket and serve them through a session-authorized, no-store proxy; do not expose their original public URLs. Existing public originals remain public unless the whole catalogue storage policy is changed. Short-lived signed URLs are another option, but their validity can outlast a revocation, so the proxy better matches immediate server-side revocation. Already delivered images and screenshots cannot be recalled. [Supabase bucket access models](https://supabase.com/docs/guides/storage/buckets/fundamentals).

Recommendation: ship page/API controls first if that matches the intended promise; include private copies/proxy in v1 if confidentiality of the shared image URLs is a requirement. Record this explicitly in acceptance criteria.

## 5. Daily credits: accounting contract

| Rule | Recommended behavior |
| --- | --- |
| Daily amount | 2,000 per eligible business wallet |
| Reset boundary | 00:00 Asia/Kolkata, independent of device timezone; equivalent to 18:30 UTC on the preceding UTC date |
| Balance reset | Replace the daily allowance; never add 2,000 to leftover credits |
| Inactivity | Returning after several days provides only today's allowance; no backfilled accumulation |
| New verification | Initialize today's allowance once when the business becomes eligible |
| Ineligible business | No grant or spending when pending, rejected, banned, suspended, or otherwise ineligible |
| Staff | Resolve owner wallet on the server and audit the acting employee; no extra daily allowance per employee |
| Feature prices | Keep server rate-card costs; remove rupee-conversion copy |
| Sharing | Free to create/view in v1; bounded independently with link/rate limits |
| Empty wallet | Stop credit-consuming work; display the next reset time instead of checkout |
| Failed work | Reverse the exact debit once, with the day-aware behavior below |

Source migrations price ordinary fusion at 200 credits, so 2,000 would permit ten of those actions daily. Uploads with one to four studio images cost 200–800 in the inspected migrations. Confirm live values before publishing these examples. Add rate limits and per-wallet job concurrency limits as well: a credit allowance controls consumption but does not alone prevent bursts or many newly-created accounts. Verified-business eligibility is the existing foundation; avoid adding device-based grant rules.

### Retain the lot/ledger model

Add daily-mode configuration, timezone and allowance, plus an effective cutover timestamp/version. Add `daily` as a lot source and a `budget_date` field, with a unique `(account_id, budget_date)` daily-lot constraint. Add account reset/date metadata if useful for cheap lookups; the unique lot and ledger keys are the authoritative duplicate protection. Use one immutable daily grant key such as `daily:<wallet-id>:<India-date>`.

Introduce an internal `credits_ensure_daily_budget(wallet_id)` routine. All wallet mutations acquire the same account-row transaction lock first, even when there are no spendable lots. Lock affected lots only after that lock. Replace the existing lot-only locking protocol in spend, grants, refunds, expiration, and settlement paths; mixed protocols would leave reset/grant races and potential lock-order deadlocks. Apply nested calls consistently and keep helper permissions restricted.

After acquiring the lock, sample server time once and calculate the India calendar date and next local midnight. A request that waited across midnight must use the date at admission after the lock, rather than stale request time. A short transaction's chosen budget period stays consistent for its grant and debit.

In the same transaction:

1. Check business eligibility and effective credit mode.
2. Settle previous daily leftovers as expired, with an append-only ledger entry once.
3. Ensure exactly one daily lot of 2,000 for the current date, expiring at the next India midnight; create only today's lot after an absence.
4. Recompute current spendable balance from applicable live lots; do not trust an expired cached value.
5. Return `mode`, `daily_allowance`, `budget_date`, `available`, `resets_at`, and `server_now`, alongside compatible existing fields.

Call this routine inside `credits_wallet()` and every credit-consuming transaction, including pipeline spends, theme unlocks, and any retained plan routines. `credits_wallet()` becomes an idempotent write-capable RPC and must not be marked STABLE. Do not give clients execution rights on an unrestricted grant helper. Preserve existing caller-scoped wallet access.

For staff, resolve the wallet owner from the authenticated actor's active store membership on the server. Never trust a supplied owner ID. Keep acting-user identity separate from wallet ownership in job metadata and audit records, and explicitly preserve product/generation authorization checks. The current pipeline uses authenticated user IDs for billing and ownership checks; simply substituting the owner ID everywhere would risk losing correct job authorization. Review each employee-enabled operation and charge the store only for operations staff are already permitted to perform.

Correctness uses lazy initialization at wallet read/spend. An optional midnight batch can preinitialize eligible wallets and improve reporting, but an outage of that scheduler must not delay a user's reset. The inspected plan migration notes that no scheduler existed at that point; verify current platform capabilities instead of assuming cron is configured.

### Refunds across midnight

Preserve migration 006's exact-debit refund and replay logic. Add the original budget date and allocation to every daily debit.

- Same-day failure: restore only the originally consumed daily credits, once, to that day's lot while it is still valid. Balance must remain between 0 and 2,000.
- Failure after midnight: record a completed reversal of the old debit and the failure reason without increasing today's spendable allowance. Use zero wallet delta and explicit refunded/expired-unit metadata, or equivalent historical accounting; do not write a positive current-wallet delta for inaccessible credits. The old consumption statistics can be corrected separately.
- Retry after failure: the old debit must be marked refunded even if its reversal added zero spendable credits, so existing retry logic can bill a new attempt against today's allowance rather than treating it as a completed live charge.
- Job admitted before midnight and completed afterward: completion alone never bills it again. A genuinely new retry is a new debit.

The current refund function grants an extra 30 days when the original expiry has passed. Disable that branch for daily lots; otherwise old failures mint additional spendable credits on top of today's 2,000. Keep legacy purchase refunds in their separate settlement path.

Ledger history should explain “Daily allowance,” “Unused daily credits expired,” feature usage, and failure reversals. Lifetime totals are reporting fields, not a substitute for today's budget. Avoid allowing refunds, referral triggers, promotions, or admin grants to silently create a second active allowance.

### Frontend updates

Web: update `CreditsContext`, credit query adapters, balance badges, Treasure Chest, and insufficient-credit dialogs. Show “1,420 / 2,000 available today” and “Resets at 12:00 AM IST.” Refresh at the server-provided reset deadline, when the tab regains visibility, and after charged/refunded work. A countdown is informational; the server owns access and spending. On fetch failure show a stale/unavailable state rather than presenting zero or yesterday's balance as current.

iOS: update `CreditWallet`, `CreditStore`, wallet cards and insufficient-credit sheets. Refresh on app foreground/reset boundary and remove top-up sheets and purchase startup tasks from the shipped flow. Staff UI must call the owner-scoped wallet service rather than reading another account table directly.

Admin: expose credit mode/allowance, business eligibility, last initialized day, today's usage and failures. Provide audited support visibility. Avoid unrestricted “add credits” controls during the strict daily mode. Changing global allowance or mode should be versioned and should normally take effect at the next boundary, not overwrite a partly spent daily budget mid-day.

## 6. Removing payment flows completely from the current offer

New purchases must be disabled on the backend as well as removed from UI. Keep payment history and a narrowly scoped settlement path for transactions initiated before cutover. Retiring purchase creation does not mean dropping accounting tables or ignoring money already received.

Implementation inventory:

- Web: remove pack cards, top-up dialogs, invoice-clearance/contact-to-recharge copy, buy links, and any rupee valuation of credits. Keep feature costs expressed only in credits.
- iOS: remove StoreKit product fetching, purchase buttons, checkout sheets, Razorpay checkout entry points, custom-amount quotes, and purchase prompts in the current release. Reconciliation of previously initiated transactions can remain outside the customer purchase flow where needed.
- Supabase: reject new `credits-topup` create/options and `onboarding_*` payment-creation actions with a stable `PAYMENTS_DISABLED` response understood by older clients. Gate database purchase/credit grant routines too, so an alternate caller cannot bypass the endpoint flag.
- Apple: prevent new in-app credit sales in the release/configuration. Restrict verification and notification processing to validated legacy settlement/refund duties; do not confuse disabling new creation with refusing a legitimate unfinished transaction. Confirm provider configuration and outstanding traffic before final retirement.
- Razorpay: stop new payment-link creation, inventory existing links, cancel outstanding unpaid links where supported, and reconcile in-flight paid events by cutoff/payment record. Deduplicate late callbacks and route ambiguous events to recorded manual settlement; do not silently acknowledge and discard a paid transaction.
- Onboarding: keep applications fee-free and preserve ordinary verification; remove residual fee entry points and make the public fee/config endpoint report disabled behavior consistently.
- Duplicated functions: reconcile the admin and iOS repo copies into one chosen deployment source. Editing one copy while deploying the other would preserve the old payment behavior.
- Operational configuration: remove obsolete secrets only after legacy reconciliation no longer needs them. Do not delete purchases, invoices, transaction evidence, entitlements, or prior migration files.

### Existing balances and subscriptions: required cutover decisions

An account with 40,000 purchased credits cannot both retain that money-backed spendable balance and have a total balance that resets to exactly 2,000. The implementation must not choose to erase it silently.

Recommendation: keep the new daily balance separate in eligibility/reporting, preserve legacy purchase records and units in a non-daily balance/archive, and prepare an account-by-account settlement/migration report. The business must choose treatment of any real paid balances before cutover: retain separate legacy spending, compensate/refund, or another explicit agreement. Retaining separately spendable legacy credits changes the total-credit promise and should be communicated as such. Block production conversion for affected accounts until this is settled; unaffected code and staging work can proceed.

Existing welcome/referral/promo balances also need an audited conversion rule. Propose stopping their additional spendable grants in daily mode while retaining referral relationships and attribution. Refresh the “earn 1,000 credits” copy. Never delete old ledger rows; if previous units are removed from active balance, append a conversion/adjustment record and retain their source.

Recommend suspending new credit-priced plan subscriptions and auto-renewal during this free-credit period. Make `my_plan()` read without automatic debits in daily mode, preserve previously acquired themes/entitlements, and offer verified retailers temporary access to the relevant plan-gated features through a mode-aware entitlement rule. Keep this temporary access distinct from a permanent entitlement so monetization can change later. Fixed 500-credit one-time theme unlocks can remain daily-credit actions if that is the desired product behavior; otherwise include them in the temporary feature access. Decide this explicitly before changing entitlement guards.

Do not fix unaffordable plans by making their renewal charge zero: that would create effectively paid subscription records without a considered product policy. Orders and jewellery purchase negotiations are a separate domain; removing credit/onboarding checkout does not imply deleting order workflows.

## 7. Work packages and implementation order

| Package | Deliverable | Main code areas | Dependency |
| --- | --- | --- | --- |
| A. Resolve contract and deployment baseline | Accepted defaults, live migration/function inventory, paid-balance report, source of truth for functions | All repos; read-only provider/database checks during execution | First |
| B. Share data and service | Tables, RLS, atomic creation/claim/revoke, minimal public projection | New canonical Supabase migrations; Next.js API routes | A's sharing choices |
| C. Web customer wishlists and recipient page | Customer/board list needed to reach existing boards; share dialog, history, timed recipient grid | `Jewel-India-Frontend/app`, new wishlist components/adapters | B |
| D. Daily accounting | Daily source/date, common account locks, lazy reset, refund changes, compatible wallet contract | Canonical migrations; pipeline repository/main integration | A's credit/cutover choices |
| E. Payment and plan retirement | Server gates, provider cutover procedure, UI removal, plan/entitlement behavior | Edge functions, web Treasure Chest, iOS StoreKit/onboarding/plans | D staged and validated |
| F. Admin and mobile alignment | Admin share revocation/usage; iOS daily wallet and optional share creator | Admin web and `wholesaler ios`; native admin monitoring only if required | B/D contracts |
| G. Validation and controlled release | Race/boundary tests, end-to-end review, migration rehearsal, monitored cutover | Staging and release configuration | All relevant packages |

The full web wishlist management screen is a prerequisite discovered during this review. Keep it focused on reading existing customer boards, reaching their contents, and sharing them; include creating/editing customers/boards and product-to-board assignment if web users need to manage them without iOS. Specify that scope in package A. A first release can share existing iOS-created boards from a minimal web board screen, followed by full web management.

Provisional effort for an experienced engineer: 10–17 engineering days including staging and mobile alignment. Rough allocation: 1–2 baseline/design, 2–3 share backend, 2–4 web wishlist/share UI, 2–3 daily accounting, 2–3 payment/plan/mobile updates, and 1–2 integrated validation; some activities overlap. Allow extra time for full web wishlist CRUD, private media, distinct-account onboarding, or legacy financial settlement. This is a planning range, not a delivery commitment or store-review estimate. After package A, split into reviewable changes with acceptance criteria rather than one broad change.

## 8. Validation that matters

### Sharing

- Two simultaneous admissions for the final slot: exactly one new admission succeeds; no counter/session mismatch.
- Retry after the claim commits but its response is lost: same admission, no second slot.
- Reload/image/pagination requests: no admission increments.
- Expiry crossed while waiting on a lock: claim uses current time and is refused.
- Last slot consumed: existing admitted sessions work; newcomers are refused.
- Revocation, source deletion, or store suspension: further content/media requests fail.
- Another store's board ID, share ID, or product ID: no create/manage/read access.
- Inactive employee: cannot create/revoke through inherited store rights.
- Link previews, HEAD requests, Next.js prefetch, and search crawlers: consume zero admissions.
- PWA offline cache, browser back/forward restore, CDN, and image optimizer: no stale authorized content served after access ends.
- Captured board membership, product unpublishing, empty board, and expired session: correct UI states and field allowlists.
- Actual WhatsApp copy/paste and mobile Safari/Chrome opening: fragment preserved and generic preview, with no accidental app-link interception.

### Daily credits

- Balance 1,700 at 23:59:59 IST becomes 2,000 for the next day; never 3,700.
- Zero balance and inactive user returning days later: only today's 2,000.
- Wallet read/reset/spend/refund from multiple devices and a staff account racing at midnight: one grant and correct final balance.
- Account initially has no lots: account-row lock still serializes grant/spend.
- Same request key: one debit and one job; preserve feature/reference/account conflict checks from migration 006.
- Same-day refund: reverses the exact debit once; late refund: audit reversal with no extra current allowance.
- A refunded old attempt retried today: new billable attempt, no duplicate refund/job.
- Different device timezones and a manipulated clock: unchanged server result.
- Scheduler absent or failed: read/spend initializes today's budget correctly.
- Banned/pending user, disabled employee, malformed wallet-owner ID: rejected by server eligibility/membership.
- New purchase, old client checkout, welcome/referral/promo trigger, plan auto-renew, and arbitrary grant call: cannot inflate the daily balance.
- Web legacy upload request without image count: verify there is no unintended free upload path; retain free-feature decisions explicitly.
- Database unavailable: consuming operations fail closed; UI shows unavailable/stale status rather than free work.

Use real database integration tests for race/locking/reset invariants in addition to appropriate existing Python/edge-function tests. Mock-only tests cannot establish transaction correctness. Run focused web builds and iOS builds for touched clients, and verify representative owner/staff/recipient flows on mobile layouts.

## 9. Rollout, monitoring, and rollback

1. Inventory deployed schema/functions and provider activity. Choose the canonical migration/function location; do not modify old applied migrations. Snapshot accounting and plan state for the cutover report.
2. Deploy additive schema and dormant feature flags. Rehearse migrations and rollback in staging with production-shaped anonymous data and legacy lots.
3. Deploy backend contracts, client compatibility, and payment-disabled handling. Confirm every credit-consuming route is authenticated/metered in the actual runtime; `CREDITS_ENABLED` is false by default in source and must not stay false for daily mode.
4. Release updated clients and disable provider purchase availability/new link creation. Reconcile the cutoff window before daily conversion. Old builds must receive a safe disabled-checkout response and must not bypass daily enforcement.
5. Enable sharing for a small verified cohort. Then enable daily credits at a documented India midnight after required paid-balance decisions are complete. Avoid a partially applied mixed regime across clients and database routines.
6. Monitor daily grant count/uniqueness, balances outside 0–2,000 in daily wallets, reset failures, lock waits/deadlocks, refunds, grant rejections, job usage/cost, admission counts, invalid-token bursts, cache behavior, and late payment events. Logs exclude raw share/session tokens and customer contact fields.

Kill switches: stop new share creation/claims; revoke affected shares; stop new metered job admission if accounting is inconsistent. Disabling daily mode is not a reason to re-enable payments. Rollback preserves all daily and legacy ledgers; never restore a stale balance snapshot over transactions performed after cutover. A full accounting rollback needs a reconciled conversion, not only a code deployment.

## 10. Confirmed rules and remaining decisions

The user confirmed: one viewing session per recipient; anonymous browser admissions; both retailer and wholesaler businesses; staff share their business's allowance. These do not need to be asked again.

Remaining product/transition decisions before execution:

1. Accept the proposed midnight India reset, no carryover, share-limit bounds, 30-minute session duration, and free sharing, or adjust those defaults.
2. Customer boards only in v1, and minimal web browsing/sharing or full web wishlist management? Store shortlist sharing can follow separately.
3. Page/API expiry or private share-image delivery as well?
4. Treatment of real paid balances and pending payments; no production conversion without this answer if such balances exist.
5. Suspend plan sales/renewal and temporarily unlock plan-gated functionality, or specify a different free-credit-era model; confirm theme unlock behavior.

The concrete implementation can start after these contracts are accepted. Until then, this document is the reviewable planning deliverable.
