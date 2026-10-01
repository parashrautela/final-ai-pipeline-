# Wishlist sharing and daily credits: release handoff

Status updated 2026-10-02: sharing migration 014 and the independent history RPC migration 016 are applied to the linked production Supabase database. Migration 015 and daily-mode activation remain pending; this repair did not change credit balances, payment processing, subscriptions, or refunds. Website publishing is deferred until the user obtains access from its hosting owner. The iOS changes are committed source and pass a simulator Release build; they still need installation/distribution.

## October 2 repair

The iOS screenshots came from missing production dependencies: the wishlist-sharing tables/functions and `credits_history` RPC were absent, and the iOS-configured website returned 404 for `/api/wishlist-shares` and `/share/wishlist`.

- Applied `014_wishlist_sharing.sql` and `016_credit_history_rpc.sql` only. Migration 016 is independent of daily mode, reads the authenticated verified business's ledger, supports active staff and stable pagination, and refuses anonymous/inactive/suspended access. It changes no balances or payment behavior. Migration 015 uses `CREATE OR REPLACE` for history so the later full rollout remains compatible.
- Production checks passed for share creation, guest admission, content, revocation, and a verified business history read inside a transaction rolled back in full. No test link/session remained. PostgREST now exposes both sharing and history. This verifies the database; the website is still the old deployed application.
- iOS activity no longer shows a red error card. A failed request preserves previous entries and offers a neutral refresh action. Recent-link loading is separate from creation, stale history responses cannot overwrite a later refresh, and a previously created link is retained if another creation attempt fails. Upload rate descriptions no longer show legacy rupee copy.
- To finish sharing: obtain access to the existing Vercel project hosting `jewel-india-frontend-yws1.vercel.app`, deploy the reviewed frontend branch `feat/three-door-onboarding`, retain its Supabase server environment variables, and verify authenticated create/list/revoke plus anonymous `/share/wishlist` admission. Do not change the app's site URL to a different host unless the domain and environments are deliberately updated together.
- Automatic approval review rejected applying migration 015 during this narrow repair because it changes financial behavior while purchased credits remain. The safer history-only repair was used. Review purchased-balance handling separately before any full daily-credit cutover.

The behavior below describes the full implemented feature. Daily-program behavior is conditional on the remaining release steps, not active production behavior today.

## Delivered behavior

- Retailer owners and active staff can create customer boards in the web app, save published designs, create links, inspect counts, and revoke links. The existing iOS customer-board menu also offers sharing, copying, counts, and revocation. Staff can reach wishlists from their iOS menu.
- Links admit 1–100 anonymous browsers, with creator-selected validity from 5 minutes to 7 days. Opening the landing page or a messaging preview uses no slot. The recipient explicitly opens the wishlist; admission and viewer count happen in one locked database transaction.
- Each browser receives one session, limited to 30 minutes and the link deadline. A response-loss retry rotates its secret but keeps the same admission and deadline. Reloading during that session works; an ended session cannot obtain another admission using the same browser identity. Clearing browser data or using another browser counts as another viewer: anonymous links cannot prove distinct people.
- Links snapshot product membership. New board additions stay out of existing links. Unpublished/deleted products disappear. Read-only output includes store display name and published design details; customer identity, phone, notes, raw images, and supplier contact fields stay private.
- Revocation, expiry, and store suspension deny further API reads. The open page polls every 10 seconds for revocation, clears content at its deadline, and revalidates after returning to the foreground. A previously downloaded image or screenshot cannot be recalled. Existing catalogue media URLs remain public, as requested.
- Share/session tokens are random 256-bit capabilities stored as hashes. Link secrets use URL fragments; viewing secrets use secure HttpOnly cookies in production. Private responses use no-store/noindex/no-referrer; PWA runtime caching bypasses wishlist pages/APIs. Telemetry filters exclude wishlist requests, links, and content.
- Verified retailer and wholesaler businesses get exactly 2,000 credits for the current India calendar day. Staff share their retailer's wallet. Resets occur at midnight Asia/Kolkata, with no carryover or grants for missed days. Wallet reads and spends lazily establish today's allowance; no midnight scheduler is required for correctness.
- Spends are locked and idempotent. Same-day failed-work refunds restore the original daily lot once. Late refunds reverse the historical debit without adding to today's allowance. Referral/welcome/admin/promotional grants cannot stack onto the daily allowance.
- Retailer plan sales and automatic renewal are paused. Verified retailers have temporary plan/theme access while daily mode is active. Existing subscription and permanent-unlock records remain. Shipping iOS checkout screens are excluded; older callers display allowance information. Backend payment creation fails closed when the program flag is absent/unavailable or purchases are disabled.
- Legacy provider callbacks stay available for reconciliation. Old Apple transactions dated before retirement may settle into preserved legacy records; new transactions are refused and logged for manual handling. Late Razorpay settlements preserve their receipt and open a manual-settlement item without increasing daily credits.
- Admin `/wishlist-controls` lists the latest 100 shares, optionally by store ID, displays program status, and revokes links. Its Edge Function validates `ADMIN_PASSWORD` server-side; it does not trust the existing browser-only admin gate or a bundled VITE password.

## Database and release order

Use the existing migration process and a database backup. The new SQL files are in `ai-pipeline/migrations`, even though some prerequisites live under `wholesaler ios/supabase/migrations`. They must run once against the same Supabase database used by all clients.

Prerequisites:

- Existing credit schema/functions and hardening through `006_razorpay_purchases.sql`, rupee prices, and welcome migration `008`.
- Existing retailer-wallet/entitlements `20260919_01`, customer-wishlist `20260919_02`, retailer-plans `20260919_03`, and Apple credits `20260926_01`.
- Existing production product schema, published status, and generated/showcase-image columns. Apply the other already-required product/referral/order migrations through the project's normal migration history; do not re-run old seeds over a live installation.

Release steps:

1. Apply `014_wishlist_sharing.sql` if it is not already installed. For history-only repair without a credit cutover, apply `016_credit_history_rpc.sql`. Apply `015_daily_credit_program.sql` only as part of the separately reviewed full credit-program release; it can replace the independently installed history RPC. The latter starts with `payments_enabled=false` and `daily_enabled=false`. It stops new payments/plan charges without silently converting purchased balances. Note the migration timestamp is the Apple retirement cutoff.
2. Deploy `credits-topup` with its existing JWT verification from the updated source in `wholesaler ios/supabase/functions` (the admin repository copy is also updated). Keep the existing Razorpay webhook and Apple confirmation/notification functions for settlement. Deploy new `admin-wishlist-shares` from the admin repository, following existing admin-function JWT configuration, with the server-side `ADMIN_PASSWORD` secret.
3. Cancel outstanding Razorpay purchase links and retire credit-pack sales in App Store Connect. Those provider-console changes have not been performed here. Review pending/unsettled receipts and `credit_purchase_issues`, including callbacks that arrive after retirement. Confirm paid purchases/refunds reconcile before activating daily mode.
4. Run this trusted service-role/database-owner activation call:

   ```sql
   SELECT public.credits_activate_daily();
   ```

   Continue only if `ok` is true. If it returns `LEGACY_PAID_BALANCES_REQUIRE_DECISION`, it reports the number of affected accounts and performs no conversion. Refund/settle purchased balances according to an explicit business decision before retrying. Do not force the flag or zero the lots to bypass this check. Once active, old promotional units are preserved separately on first wallet use; purchased units are never silently discarded.
5. Deploy the Next app and Python pipeline with `CREDITS_ENABLED=true` (the new default). Remove any legacy environment override that disables credit enforcement. Deploy admin web and distribute the new iOS build. Daily-mode wallet clients require migration 015; history reads work with either migration 016 or 015. Wishlist routes require 014. Existing clients cannot override the server's daily allowance or reopen payment creation.
6. Smoke test with one verified wholesaler, retailer, and active employee: same retailer/staff balance, plan access without a debit, guest admission, final-slot contention, revoke, expiry, exhausted daily balance, and the next India day. Confirm `credits_program_status()` reports daily enabled/purchases disabled.

Turning daily mode off is not an automatic rollback: retain database/ledger records and explicitly decide how daily, legacy, and new payment balances would be handled before restoring any paid offering.

## Validation completed

- Real PostgreSQL integration suite: 39 scenarios, including eight history-only checks before the daily migration, plus concurrent admissions, response-loss replay, private projection, ownership, suspension, expiry, admin revocation, purchased-balance activation guard, delayed payment settlement, shared staff spends, refunds, midnight reset and lock waits, staff deactivation during a wait, and revoked legacy-function permissions.
- Existing Python credit/upload/set-creation regression suites: 77 tests pass. The onboarding-fee regression now asserts zero even when a legacy paid environment value is present.
- Existing top-up and Razorpay webhook unit suites: 88 tests pass, including disabled/unavailable payment flags preventing provider calls.
- Wishlist contract/privacy tests: 4 tests pass.
- Chrome recipient tests use API fixtures against the built production page: no-sign-in landing, explicit admission, reload, revocation, timer expiry, mobile layout, private headers, service-worker cache exclusion, and no recipient telemetry transmissions. Database authorization/concurrency is tested separately with real PostgreSQL. This does not claim a live Supabase end-to-end deployment test.
- Next production build, admin Vite build, targeted ESLint, and iOS simulator Debug and Release builds pass.

## Reproduce checks

Test runtimes were installed outside application dependencies in temporary directories. For a fresh machine, install `embedded-postgres` + `pg` for the database suite, `playwright` for the browser suite, and `pytest` for the existing Python environment.

```sh
# ai-pipeline
JEWEL_TEST_RUNTIME=/path/to/test-runtime/node_modules node tests/credit-sharing.integration.mjs
python -m pytest test_credits.py test_product_upload_credits.py test_set_creation_pieces.py -q

# Jewel-India-Frontend (with a local production preview on port 3100)
node --test tests/wishlist-contract.test.mjs
JEWEL_BROWSER_RUNTIME=/path/to/browser-runtime/node_modules node tests/wishlist-browser.mjs
npm run build -- --webpack

# Admin-Panel-for-jewel-India-
node --experimental-strip-types --test supabase/functions/credits-topup/*.test.ts supabase/functions/razorpay-webhook/*.test.ts
npm run build

# wholesaler ios
xcodebuild -project JewelIndia.xcodeproj -scheme JewelIndia -sdk iphonesimulator -configuration Release CODE_SIGNING_ALLOWED=NO build
```
