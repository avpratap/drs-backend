# MASTER FIXLIST — full-backend review (2026-10-24)

Scope: the whole FastAPI backend, reviewed domain by domain by 8 independent reviewers
(money, GST, cutoff/subscriptions, delivery/SCM, orders/cart/catalog, auth+DPDP,
notifications+infra, test suite). Every finding below was verified against the code
before fixing. Each fix has a regression test.

Commits on `claude/brave-cray-021oj1`:
`3301f99`/`183e247` (S1), `c91d619` (money, GST, infra), `5723be2` (cutoff, subscriptions, SCM),
`15a73bf` (orders, cart, catalog), `a4c2698` (auth, DPDP, notifications, infra),
`878abda` (tests), plus the final docs/Docker commit.

Status: **FIXED** = code fixed, with a regression test. **BY-DESIGN** = deliberately not changed, with the reason given.
Test files: `rmg` = tests/test_review_money_gst.py, `rcs` = test_review_cutoff_subs.py,
`rscm` = test_review_scm.py, `ro` = test_review_orders.py, `rai` = test_review_auth_infra.py.

## S — Shipping
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| S1 | High | create_shipment held order lock + txn across the Shiprocket HTTP call | FIXED: draft → courier (no lock) → finalize; persisted courier ids between steps; owned lease; cancel/create race guard | test_shipping.py (24 tests) |

## G — GST / invoicing
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| G1 | High | 2nd RMA refund (or RMA after a full CN) → IntegrityError on uq_credit_note_original → 500, refund lost | FIXED: one cumulative partial CN per invoice; later calls add lines + re-total under the CN lock, capped per product by un-reversed qty AND value; a full CN means nothing left | rmg::test_two_rma_refunds_share_one_consistent_credit_note |
| G2 | High | Invoice PDFs at guessable sequential keys on the PUBLIC CDN | FIXED: private storage (separate R2_PRIVATE_BUCKET, required in prod), random key, short-lived presigned/HMAC-signed URLs minted only after the authz check; dev /files requires the signature for private/ keys. **Ops:** delete legacy public `invoices/*` objects from the CDN bucket | test_invoice_pdf_upload::test_invoice_pdf_is_private_signed_and_cached |
| G3 | Med | partial→full CN upgrade left the cached PDF stale | FIXED: any CN re-total clears pdf_url (re-render) | covered by G1/G6 flows |
| G4 | Med | partial CN components rounded separately (1 paisa off) | FIXED: line total allocated first (remainder to last line), then split with the invoice's inclusive formula | rmg::test_partial_credit_note_never_drifts_a_paisa |
| G5 | Med | free-text state ("RJ", "08", "rajasthan.") flipped CGST/SGST↔IGST | FIXED: app/core/gst_states.py canonical names + GST codes; comparison by code; canonical place of supply stored | rmg::test_state_normalisation, ::test_abbreviated_home_state_is_intra_state |
| G6 | Med | partial refund to source issued no credit note | FIXED: value-only partial CN (qty 0) for partial refunds; remainder upgrades to the exact full reversal | rmg::test_partial_refund_to_source_issues_value_credit_note |
| G7 | Med | PDF missing buyer, separate CGST/SGST/IGST + rates, POS code, CN reference (Rule 53) | FIXED: all rendered | test_invoice_pdf_upload (renders) |
| G8 | Low | order-invoice endpoint returned `lines: []` | FIXED: shared InvoiceOut with lines | rmg::test_two_rma_refunds… (asserts lines) |
| G9 | Low | delivery billing rounded the sum once; invoice per line (paisa drift) | FIXED: billing rounds per line exactly like the invoice | rmg::test_delivery_billing_equals_invoice_total_to_the_paisa |
| G10 | Low | zero-value invoice / CN burned a gap-free serial | FIXED: lines computed before numbering; 409 "nothing supplied"; empty CN not created | rmg::test_fully_returned_delivery_issues_no_invoice |
| G11 | Low | duplicate RMA lines for one product overwrote each other | FIXED: summed per product | rmg::test_two_rma_refunds… (split lines) |
| G12 | Low | `_today()` read twice (fy vs date across Mar-31 midnight) | FIXED: read once per document | code |
| G13 | Low | long INVOICE_PREFIX overflowed invoice_no(32) → 500 | FIXED: validator (1-10 chars, no '/') | rmg::test_invoice_prefix_length_validated |

## M — Money & ledgers
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| M1 | High | retrying a timed-out partial refund issued a 2nd real gateway refund | FIXED: any initiated/pending refund blocks new refunds (409) until reconciled | rmg::test_pending_refund_blocks_new_refund_and_reconcile_applies_once |
| M2 | High | payment.failed (per attempt) / one bad signature permanently lost a later capture | FIXED: per-attempt failure only notifies; bad signature → 400 without state change; captured/valid verify finalizes from initiated or legacy failed | rmg::test_bad_signature_does_not_fail_payment_and_capture_still_lands |
| M3 | High | abandoned 'initiated' payment blocked the order forever | FIXED: create resumes the open checkout (same gateway order) | test_orders::test_no_double_open_order_payment |
| M4 | High | gift-card redemption debited the card, credited nothing | FIXED: same-txn wallet credit | rmg::test_gift_card_redeem_credits_wallet…, test_loyalty::test_gift_card_redeem |
| M5/O1 | High | cancelled order payable and resurrected 'confirmed' (coupon reuse) | FIXED: create refuses non-open orders; finalize on a cancelled order records the capture (for refund) but never confirms | rmg::test_cancelled_order_cannot_be_paid, ::test_capture_after_cancel_keeps_order_cancelled |
| M6 | Med | RMA refund + full refund-to-source paid out > order total | FIXED: app/services/refunds.py — one headroom across both paths, under the order lock; "fully refunded" counts both | test_zz_audit_fixes::test_full_credit_note_nets_out_prior_partial |
| M7 | Med | no way to reconcile pending refunds / repair a failed ledger step | FIXED: POST /payments/refunds/{id}/reconcile (admin, audited); ledger effect exactly-once via refunds.ledger_applied_at | rmg::test_pending_refund…, ::test_failed_reconcile_frees_the_cap |
| M8 | Med | spent/withdrawn top-up still refundable to the card | FIXED: top-up refund capped at the (locked) wallet balance | rmg::test_topup_refund_capped_at_wallet_balance |
| M9 | Med | Idempotency-Key > ~125 chars → DataError 500 | FIXED: 400 over 100 chars; path stored truncated, hash covers full path | rmg::test_long_idempotency_key_rejected |
| M10/O9 | Med | ₹0 order unpayable (gateway min); gateway errors → 500 | FIXED: no-gateway settlement (CHECK narrowed to gateway='none'); httpx errors → 502 | rmg::test_zero_total_order_settles_without_gateway |
| M11 | Low | withdrawal request check not under wallet lock | FIXED: wallet FOR UPDATE | rai::test_concurrent_withdrawals_cannot_overdraw |
| M12 | Low | any user could read an issued-to gift card's balance | FIXED: 404 for non-recipients | rmg::test_gift_card_redeem_credits_wallet_and_balance_is_private |
| M13 | Low | concurrent first loyalty call → 500 | FIXED: savepoint + re-select | code |
| M14 | Low | one referee could redeem unlimited codes | FIXED: one referral per referee (advisory-locked check) | rmg::test_referee_cannot_farm_multiple_referral_codes |
| M15 | Low | unquantized wallet amount (txn vs balance paisa drift) | FIXED: quantized once | rmg::test_wallet_adjust_quantizes_amount |

## C — Cutoff & subscriptions
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| C1/A1 | High | drivers (route scope) could edit/pause/cancel customers' subscriptions; notices went to the actor | FIXED: write guard (subscriber/customer/admin only); lifecycle notices to the account owner | rcs::test_driver_cannot_mutate…, ::test_status_change_notifies_account_owner_not_actor |
| C2 | High | skip/pause/cancel after an earlier run left a stale order (delivered + billed) | FIXED: rerun drops still-generated subscription orders whose plan is empty or whose sub is no longer active; delivered never touched | rcs::test_rerun_drops_order_after_skip_or_pause, ::test_rerun_never_drops_a_delivered_order |
| C3 | Med | items/pause/resume/cancel ignored the cutoff | FIXED: standing changes frozen from CUTOFF_HOUR to midnight IST (admins exempt) | rcs::test_standing_changes_frozen_after_cutoff |
| C4 | Med | duplicate product lines (double qty, 500 on override) | FIXED: 422 + UNIQUE(subscription_id, product_id) (migration 0055 dedupes) | rcs::test_set_items_rejects_duplicates_unknown_and_unpriced |
| C5 | Med | unknown product → FK 500; inactive/unpriced silently accepted | FIXED: 404 / 422 | same |
| C6 | Med | per-sub failures swallowed, run 'frozen' | FIXED: run 'partial' + failed count; worker retries | rcs::test_partial_failure_marks_run_partial, ::test_scheduled_cutoff_retries_on_partial |
| C7 | Med | job timeout (CancelledError) left the run 'generating' | FIXED: BaseException → 'failed'; per-cron timeout 3000s | rcs::test_cancelled_run_is_marked_failed |
| C8 | Med | ~10 round trips per subscription | FIXED: per-chunk batched items/overrides/locked orders (3 queries per chunk); notifications remain per sub (savepointed, dedupe-keyed) | rcs (all) |
| C9 | Low | admin cutoff accepted today/past dates | FIXED: 422 unless force=true | rcs::test_admin_cutoff_refuses_today_and_past |
| C10 | Low | price book loaded full history; ctx built before the id scan | FIXED: SQL validity filter; ctx built from the iterated ids | rcs (all) |
| C11 | Low | concurrent create → uq_subscription_shop 500 | FIXED: savepoint + re-select | rcs::test_concurrent_create_subscription_same_shop |
| C12 | Low | prepaid gate ignored other unbilled open orders | FIXED: reserves other open orders | rcs::test_prepaid_gate_counts_other_open_orders |

## D — Delivery & SCM
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| D1 | High | unswept expired lots double-counted → negative stock; production gate passed on expired stock | FIXED: expired lots written off under the stock lock before FEFO / transfer / production gate | rscm::test_consume_writes_off_expired_lot_first…, ::test_production_gate_ignores_expired_component |
| D2 | Med | lock-order deadlocks (batch→item in receive; unordered loops) | FIXED: stock_item→batch everywhere; product-id order in delivery & GRN | rscm::test_concurrent_receive_and_consume_no_deadlock |
| D3 | Med | transfer dispatch shipped expired lots first | FIXED: expiry write-off + filter | rscm::test_transfer_ships_fresh_lot_not_expired |
| D4 | Med | wastage/adjust changed on-hand not lots; wastage > on-hand accepted | FIXED: downward changes go FEFO through lots, strict (422 beyond on-hand) | rscm::test_wastage_reduces_lots…, ::test_movements_sum_to_on_hand… |
| D5 | Med | stock writes lacked the documented required Idempotency-Key | FIXED: required on adjust / wastage / batch receive | rscm::test_stock_writes_require_idempotency_key |
| D6 | Med | deliveries always deducted the default location | FIXED: locations.driver_user_id (vehicle) → delivering driver's / assigned driver's van, else default | rscm::test_driver_delivery_deducts_vehicle_stock |
| D7 | Med | 2-opt O(n⁴), unbounded (200 stops 28.5s) | FIXED: precomputed matrix, O(1) deltas, 2s budget, 500-stop cap, per-driver filter (200 stops 0.05s) | rscm::test_route_optimisation_fast_and_capped, ::test_two_opt_improves… |
| D8 | Med | production/procurement unknown/missing location → 500 | FIXED: require_location (404/409) | rscm::test_production_unknown_location_404 |
| D9 | Low | RMA while still in transit; no restock movement | FIXED (transit): 409 while a shipment is live. BY-DESIGN (restock): returned perishables are not resaleable, so no restock movement is written | rscm::test_return_refused_while_shipment_in_transit |
| D10 | Low | RMA lists N+1 | FIXED: batched items | test_rma |
| D11 | Low | expiry sweep defaulted to the UTC date | FIXED: IST | code |
| D12 | Low | stock floor CheckViolation → 500 | FIXED: global IntegrityError → 409 handler. BY-DESIGN: deliveries are never blocked on inventory, so negative stock stays allowed down to the floor | code |

## O — Orders / cart / catalog
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| O2 | Med | customer could cancel a COD order with a live shipment | FIXED: 409 while a shipment is live (both cancel paths) | ro::test_cancel_refused_once_order_has_a_live_shipment |
| O3 | Med | admin cancel unguarded; refunded order never cancellable | FIXED: status + shipment guard; fully refunded → cancellable | ro::test_admin_can_cancel_a_fully_refunded_order |
| O4 | Med | merged cart qty uncapped → checkout 500 | FIXED: 422 cap on merged qty; ValidationError safety-net handler | ro::test_cart_merged_qty_is_capped |
| O5 | Med | concurrent cart adds → duplicate lines → 500 | FIXED: UNIQUE(cart_id, product_id) + savepoint merge | ro::test_concurrent_first_adds_make_one_line, test_cart (strengthened) |
| O6 | Med | coupon reservations hoarded by unpaid orders | FIXED: ≤1 outstanding unpaid reservation per account per coupon (a sweep would also cancel legit unpaid COD orders) | ro::test_coupon_cannot_be_hoarded_by_unpaid_orders |
| O7 | Med | cart priced via first shop, checkout via home shop | FIXED: same home-shop → account → RETAIL resolution | ro::test_cart_tier_matches_checkout_home_shop |
| O8 | Med | reviews auto-approved for non-purchasers; retired products rateable | FIXED: unverified → pending moderation; retired → 404 | test_review_mgmt (updated) |
| O10 | Low | duplicate home shops on concurrent first order | FIXED: partial unique index + savepoint | ro::test_concurrent_first_orders_one_home_shop |
| O11 | Low | cart checkout / reorder dropped ship state | FIXED | ro::test_cart_checkout_captures_ship_state |
| O12 | Low | merged order qty bypassed the line cap | FIXED | ro::test_order_merged_qty_cap_and_whole_units |
| O13 | Low | fractional piece/pack; stored vs priced qty | FIXED: whole units for piece/pack; qty quantized before pricing | same |
| O14 | Low | gst_rate unbounded on create; pack_size 0 | FIXED: ge=0, lt=100; pack_size > 0 | ro::test_product_gst_bounds_and_retired_products |
| O15 | Low | retired products wishlistable / variants listed | FIXED: 404 | same |

## A — Auth & RBAC
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| A2 | Med | `--forwarded-allow-ips '*'` → spoofable client IP (OTP/IP limits, session/consent IPs) | FIXED: app/core/clientip.py counts TRUSTED_PROXY_HOPS from the right; uvicorn proxy headers off (Dockerfile/railway.json) | rai::test_client_ip_counts_trusted_hops…; verified in the running Docker image |
| A3 | Med | staff phone stored un-normalized → can never log in | FIXED | rai::test_staff_created_with_prefix_can_log_in |
| A4 | Low | erased account reactivatable; refresh tokens not revoked | FIXED | rai::test_erasure_revokes_cancels_tombstones_and_is_final |
| A5 | Low | suspended users got 200 + tokens | FIXED: 403, no session row | rai::test_suspended_user_gets_403_not_tokens |
| A6 | Low | reuse detection kept access tokens alive | FIXED: token_version bump. BY-DESIGN (logout): logout revokes only that device's refresh family; other devices stay signed in | rai::test_refresh_reuse_invalidates_access_tokens |
| A7 | Low | foreign numbers truncated onto Indian ones | FIXED | rai::test_phone_normalization_prefixes |
| A8 | Low | email unvalidated/unverified (squat → erasure deletes owner's newsletter) | FIXED: format validation; email_verified_at + signed 24h verification flow; erasure only touches a verified email's newsletter row | rai::test_email_must_be_valid_and_verified, ::test_erasure_keeps_newsletter_of_unverified_email |
| A9 | Low | concurrent correct OTP verifies both passed | FIXED: atomic compare-and-delete (Lua) | rai::test_concurrent_otp_verify_only_one_wins |

## P — DPDP / compliance
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| P1 | High | erasure kept the home-shop address; subscriptions kept billing | FIXED: home shops tombstoned/inactive, subscriptions cancelled, driver assignments ended | rai::test_erasure_revokes… |
| P2 | Med | marketing consent not re-checked at send | FIXED: gating re-evaluated in deliver_notification; resend keeps marketing quiet hours | rai::test_marketing_consent_rechecked_at_delivery |
| P3 | Med | marketing push had no consent purpose | FIXED: marketing_push purpose; marketing fails closed without a mapped granted purpose | rai::test_marketing_push_requires_its_own_consent |
| P4 | Med | unconfigured channel stub logged phone+body and reported success in prod | FIXED: masked logs; prod returns failure | rai::test_stub_sender_fails_in_prod_and_masks_pii |
| P5 | Med | export missed many PII tables/columns | FIXED: generic all-columns export of every user/account-keyed table (secrets excluded) | rai::test_export_is_complete |
| P6 | Low | queued notifications still sent after erasure | FIXED: skipped user_erased (+ re-check at send) | rai::test_erasure_revokes… |
| P7 | Low | newsletter logged emails; FCM token in URL; confirm tokens never expired | FIXED: no email in logs; POST /device-tokens/unregister (body); 7-day expiring tokens | rai::test_newsletter_confirm_token_expires |

## N — Notifications
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| N1 | High | emit_event could poison the caller's (money) transaction | FIXED: whole emit in a savepoint | rai::test_emit_failure_does_not_roll_back_caller |
| N2 | Low | stuck 'sending' rows invisible; resend could double-send | FIXED: housekeeping counts/alerts stale rows; resend refused within 5 min of an in-flight attempt | code |
| N3 | Low | concurrent device registration → 500 | FIXED: INSERT … ON CONFLICT upsert | rai::test_concurrent_device_register_same_token |
| N4 | Low | push tokens resolved at emit time | FIXED: re-resolved at send | rai::test_deactivated_push_token_not_used_at_delivery |

## I — Infra / cross-cutting
| ID | Sev | Finding | Status / resolution | Test |
|---|---|---|---|---|
| I1 | High | chunked body bypassed the 8 MB cap (unauth memory exhaustion) | FIXED: pure-ASGI cap on received bytes | rai::test_chunked_body_over_cap_rejected; verified against real uvicorn in Docker |
| I1b | — | IntegrityError / DataError / handler-side ValidationError → 500 | FIXED: safety-net handlers (409 / 422) | rmg (₹0 order CHECK) |
| I2 | Med | CMS PATCH null on NOT NULL → 500 | FIXED: 422 | rai::test_cms_patch_null_is_422 |
| I3 | Med | concurrent churn runs → IntegrityError / deadlock | FIXED: Redis run lock + UPSERT + id order (the admin endpoint still runs inline) | rai::test_concurrent_churn_scoring |
| I4 | Med | SSRF guard missed CGNAT/rebinding; dev bypass matched real hosts | FIXED: is_global, pinned validated IP + SNI, no redirects, exact dev hosts outside prod | rai::test_ssrf_guard_blocks_cgnat_and_narrows_dev_bypass |
| I5 | Med | support lists / forecasting N+1 | FIXED: batched (forecast also uses IST and one worker thread) | test_support, test_forecasting |
| I6 | Med | public sitemap loaded full bodies, unbounded, uncached | FIXED: slugs only, 50k cap, 5-min cache | test_content |
| I7 | Low | /readyz leaked exception text | FIXED | rai::test_readyz_hides_error_details |
| I8 | Low | paid AI fail-open without Redis; retried 4xx | FIXED: 503 when live; retry only transient | rai::test_ai_quota_fails_closed_when_live, ::test_ai_retries_only_transient_errors |
| I9 | Low | idempotency stored token-bearing auth responses | FIXED: /auth/ bypassed | rai::test_idempotency_never_stores_auth_responses |
| I10 | Low | rate-limit INCR/EXPIRE not atomic | FIXED: MULTI pipeline | rai::test_rate_limit_key_has_ttl |
| I11 | Low | prod guards split (worker unguarded); R2 not required | FIXED: all guards in Settings incl. R2 + distinct private bucket | test_config_guard (expanded); verified in Docker (worker refuses insecure prod) |

## T — Test suite
| ID | Finding | Resolution |
|---|---|---|
| T1 | shared customer cart leaked between test_catalog_extra / test_cart | each test uses its own customer |
| T2 | test_cutoff asserted global `orders_generated == 0` | asserts on its own order |
| T3 | forecasting committed_qty == 5 (global) | measures the delta of its own subscription |
| T4 | test_subscriptions mutated the seeded subscription | own shop/subscription |
| T5 | address default-flip depended on no prior address | own customer |
| T6 | products / admin-shops / stock lists depended on the default 50-row page | set difference / paging / new `?product_id=` stock filter |
| T7 | outbox test failed behind a backlog | drains until the target row is processed |
| T8 | test_webhook failed when run alone | creates its account |
| T9 | Redis pool lock bound to an old event loop (flaky concurrent tests) | conftest resets the lock per test |
| T10 | wall-clock dependence (23:00 cutoff, minute windows) | CUTOFF_HOUR pinned in tests; rate-limit clock frozen |
| T11 | cleanup / role restore outside finally; seeded sub paused in place | try/finally; own subscription |
| T12 | weak/tautological asserts (≈25 places: `>= 0`, `in (401,403)`, `pytest.raises(Exception)` + "check", key-only, `all([])`, fixed OTP guess, IDOR without side-effect checks) | exact amounts / deltas, exact codes and exception types + constraint names, positive controls, guaranteed-wrong OTP |
| T13 | `pytest-randomly` is not installed (so `-p no:randomly` is a no-op) | the suite was verified with a shuffle plugin: file order + 7 shuffled orders (seeds 1–5, module-mode 7/99) all green. Adding pytest-randomly as a dev dependency is left as a follow-up for the maintainers |

## Round 2 — independent re-review (8 reviewers: 4 diff reviews of round 1, 4 whole-codebase audits)

Commits: `2ff6f2a` (money), `2578754` (orders/notify/auth/DPDP), `93056d7` (cutoff/delivery/SCM),
`906b09c` (infra/schema/validation). Tests: `r2m` = tests/test_review_round2_money.py,
`r2o` = test_review_round2_orders.py, `r2s` = test_review_round2_scm.py, `r2i` = test_review_round2_infra.py.

### R2-M — money / refunds / GST
| ID | Sev | Finding | Resolution | Test |
|---|---|---|---|---|
| M1 | High | refunding a duplicate capture reversed the whole order + its invoice and ate the order's refund cap | `payments.is_excess` set when a capture didn't settle its order; refunded on its own, excluded from the order cap/net revenue | r2m::test_duplicate_capture_refund_leaves_order_and_invoice_alone |
| M2 | High | reconcile racing the gateway call → effect applied twice / terminal status overwritten | refund_payment re-locks Payment→Refund (populate_existing) after every commit, never overwrites a settled status | r2m::test_refund_settled_during_gateway_call_is_not_applied_twice, ::test_gateway_error_never_overwrites_a_reconciled_status |
| M3 | High | reconcile 'failed' on an in-flight 'initiated' refund → second real refund | 'initiated' reconcilable only after 10 min | r2m::test_young_initiated_refund_cannot_be_reconciled |
| M4 | High | stale wallet balance after commit (identity map) → lost update | every SELECT … FOR UPDATE refreshes the row (session hook) | r2m::test_locked_select_refreshes_identity_map |
| M5 | High | top-up refund cap checked, then lock released before the debit (withdrawal could spend it too) | wallet debit held at 'initiated'; released on 'failed' reconcile | r2m::test_topup_refund_holds_wallet_during_gateway_call |
| M6 | High | retrying a partial refund after a lost 200 paid twice; refunds unaudited | Idempotency-Key required; audit row | r2m::test_refund_requires_idempotency_key_and_is_audited |
| M7 | Med | RMA after a partial refund to source under-reversed GST, order stuck partially_refunded | shortfall reversed by value mode; headroom 0 → refunded + full CN; returns allowed on partially refunded orders | r2m::test_rma_after_partial_refund_…, ::test_rma_that_exhausts_headroom_… |
| M8 | Med | full credit note committed inside the refund transaction | flush-only build_full_credit_note | covered by M2/M7 |
| M9 | Low | CN allocation could emit a negative / over-cap line | paise allocation with per-line caps | r2m::test_credit_note_lines_are_never_negative |
| M10 | Low | order refunded before invoicing could never be invoiced; ₹0 invoice for free orders | invoice + catch-up CN under the order lock; free orders: no invoice | r2m::test_partially_refunded_order_gets_invoice_…, ::test_free_order_gets_no_zero_invoice |
| M11 | Low | "UT" alias mapped to Uttarakhand | removed | — |
| M12 | Med | gift-card / store credit withdrawable as cash | withdrawals capped at net cash paid in (request + approve) | r2m::test_gift_card_credit_cannot_be_withdrawn_as_cash |
| M13 | Med | ~80 admin money/price/stock writes unaudited; audit log hid detail/time | every admin write audited automatically in the same transaction; detail + created_at exposed | r2m::test_admin_writes_are_audited_automatically |
| M14 | Med | create_payment held the order lock across the gateway HTTP call | 'creating' placeholder committed first; unique open payment per order | suite (payments) |
| M15 | Med | analytics revenue ignored refunds | revenue net of processed refunds + RMA credit; credit outstanding consistent | suite |
| M16 | Low | gift-card issue / points adjust not idempotent | Idempotency-Key required | test_loyalty |

### R2-O — orders / notifications / auth / DPDP
| ID | Sev | Finding | Resolution | Test |
|---|---|---|---|---|
| O1 | Med-High | COD orders never became paid → coupon blocked forever, no invoice, reviews unverified | paid on courier delivery; shipped COD orders don't block the coupon | r2o::test_cod_order_paid_on_delivery_and_coupon_reusable |
| O2 | Med | transactional SMS failed 5× per event in prod | skipped as provider_unavailable (not retried) | r2o::test_sms_skipped_when_provider_unavailable_in_prod |
| O3 | Med | unverified email claims locked the owner out; no send throttles; '|' broke tokens; newsletter enumerable | verified-only, case-insensitive uniqueness; per-recipient throttles; rsplit parsing; uniform newsletter answer | r2o::test_email_unique_only_when_verified_and_throttled, ::test_email_with_pipe_…, ::test_newsletter_… |
| O4 | Med | WhatsApp opt-in implied marketing; marketing_push ignored; marketing_opt_in settable without consent; WhatsApp on by default; bell showed gated marketing | marketing_whatsapp purpose; master derived from all marketing consents; toggle ⇄ ledger; default off (+migration); bell filter | r2o::test_whatsapp_optin_does_not_grant_marketing, ::test_whatsapp_toggle_is_recorded_as_consent |
| O5 | Med | address/payment-method default race; default could be unset; mandates outlived their method; no mandate cancel | re-check after lock; unset refused; cascade cancel; cancel endpoint; past valid_to refused | r2o::test_address_default_rules, ::test_mandate_cancel_and_cascade_… |
| O6 | Med | erasure left tomorrow's deliveries and pending withdrawals | deliveries cancelled; erasure waits for withdrawals (409) | r2o::test_erasure_blocked_by_pending_withdrawal |
| O7 | High | POD photos / uploads public, with GPS EXIF, no quota | private media refs, EXIF stripped, daily quota, owner-only attach, signed view URLs | r2o::test_upload_is_private_ref_with_exif_stripped |
| O8 | Low | rating/wishlist rollback 500; referral/profile create races; recompute lost votes; cart add vs checkout; override/item upsert races; stale CN PDF cached; drain read stale consent | savepoints, product lock, cart lock, item/sub locks, conditional PDF cache write, populate_existing at send | suite + r2o |
| O9 | Low | self/hidden review votes; whole units only at checkout; "+91 0…" phones; stale draft blocked cancel; avatar javascript:; account type by first endpoint; export newsletter/GPS | fixed each; admin account-type endpoint | r2o::test_review_votes_rules, ::test_whole_units_enforced_in_cart, ::test_phone_…, ::test_admin_can_set_account_type |

### R2-C — cutoff / delivery / SCM
| ID | Sev | Finding | Resolution | Test |
|---|---|---|---|---|
| C1 | High | cash collected at the door never settled the bill (COD charged twice); amount unbounded | posted against the bill (wallet credit / credit payment), capped at amount due; method enum | r2s::test_cash_collected_pays_the_delivery |
| C2 | Med | drivers could deliver/fail future-dated stops | refused for non-admins | r2s::test_driver_cannot_close_future_stop |
| C3 | Med | inactive shops / suspended owners still got deliveries + bills | cutoff filters shop + owner status; inactive shops get no new standing orders | r2s::test_cutoff_skips_inactive_shop |
| C4 | Med | cutoff retry after midnight built the wrong day | date from arq enqueue_time | r2s::test_scheduled_cutoff_date_from_enqueue_time |
| C5 | Med | set_items rejected qty-0 lines and tomorrow-dated prices | priced as of first delivery date, qty-0 skipped; future overrides bounded per item | r2s::test_set_items_allows_zero_qty_… |
| C6 | Low | sweep used the run-start snapshot (NOT IN with N params); IN lists over 32k params | decided at sweep time (NOT EXISTS); ANY(array) binds | suite |
| C7 | Low | freeze lasted until midnight; re-subscribe skipped it | lifts when tonight's run has frozen tomorrow; applied on re-subscribe | r2s::test_freeze_lifts_after_tonights_run |
| C8 | Low | substitute driver drew another van; vans not re-bindable; forecast UTC dates; future as_of wrote off stock; expiry < mfg; route counted closed stops; dead code | fixed each (PATCH /admin/locations/{id}) | r2s::test_substitute_driver_…, ::test_expire_as_of_future_rejected_… |

### R2-I — infra / schema / validation
| ID | Sev | Finding | Resolution | Test |
|---|---|---|---|---|
| I1 | Med | churn lock never blocked (`got is False`) | redis_ok flag + atomic release | r2i::test_churn_lock_blocks_concurrent_run |
| I2 | Low-Med | chunked oversize → 400 on most routes, logged as 500 | send-side 413 replacement; not counted as 500 | r2i::test_chunked_body_413_without_idempotency_key |
| I3 | Low | global handlers silent + echoed input | log + no input | suite |
| I4 | Low | https scheme lost behind proxy; TRUSTED_PROXY_HOPS=0 trap; /metrics public; / disclosed ENV; seed had no prod guard | ProxySchemeMiddleware, prod warning, env/docs, METRICS_TOKEN, seed refuses prod | r2i::test_proxy_scheme_and_root_and_metrics, ::test_seed_refuses_prod |
| I5 | High | (ref_type, ref_id) scans on every delivery/checkout/refund; FK indexes missing | migration 0056 indexes | r2i::test_integrity_backstops_exist |
| I6 | High | autogenerate would drop DB-only backstops | include_object guard; models mirror new uniques | — |
| I7 | Med | 0055 could delete the ACTIVE subscription line; downgrades failed on real data (0048/0049/0055) | ranked dedupe; safe downgrades (verified up/down/up) | migration round trip |
| I8 | Med | explicit null → 409/500 on PATCH | reject_nulls on every patch model | r2i::test_patch_nulls_and_bounds_are_422 |
| I9 | Med | Numeric(12,2) overflow; gst 99.995; pack_size Infinity; unbounded ints/lists; free-text state/pincode | bounds + order cap + state/pincode validation; products.pack_size Numeric(10,3) | r2i::test_unknown_state_…, ::test_order_value_overflow_is_422 |
| I10 | Med | code-only rules without DB backstops | conditional partial uniques (one charge/delivery, redemption/order, referral/referee) + NOT VALID CHECKs | r2i::test_integrity_backstops_exist |
| I11 | Low | unbounded purges / GPS table; home sections aborted each other; shipment N+1; webhook non-string fields; stale sitemap; CSV dropped newest; webhook Host port; HTTP inside txn | batched purges + retention + ping throttle; savepoints; batched events; coercion; invalidation; newest-first + header; Host:port; commit first | r2i::test_csv_export_…, ::test_shipping_webhook_…, ::test_sitemap_… |

### Deliberately NOT changed (needs a product/ops decision — not code defects)
- Re-authentication before self-erasure: tokens live 15 min; a step-up OTP flow is a product feature.
- Mandatory POD for drivers: policy choice (POD URLs are now validated and private).
- Payment-method gateway tokens supplied by the client: needs the gateway's tokenisation API; responses never expose them.
- Referral reward before the referee's first purchase: business rule.
- Idempotent get-or-create on some GETs (accounts/wallet/cart): harmless, kept for client compatibility.
- 0045 historic data migration: already applied everywhere; left untouched.

## Verification
- `uv run pytest -p no:randomly -q`: **448 passed** (400 → 448 with round-2 regression tests). `uv run ruff check .`: clean.
- Shuffled runs (sequential, never concurrent): seeds 11, 22, 33 all pass; round 1 also passed seeds 1–5, 7, 99.
- Migrations: fresh upgrade to 0056, downgrade to 0054, upgrade again — clean.
- Docker (local-verify image; CI builds the real Dockerfile): migrated a fresh DB to 0056, `/readyz` ok, a forwarded-https trailing-slash redirect now points at https. Round 1 also verified: XFF spoof ignored, chunked 413, non-root, prod guards.
