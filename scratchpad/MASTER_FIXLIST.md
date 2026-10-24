# MASTER FIXLIST — full-backend review (2026-10-24)

Status legend: TODO / FIXED (commit) / WONTFIX (reason) / NOT-A-BUG (reason).
Each FIXED item has a regression test (named in the Resolution column).

## S — Shipping
| ID | Sev | Finding | Status |
|---|---|---|---|
| S1 | High | create_shipment held order lock + txn across Shiprocket HTTP call | FIXED (3301f99, 183e247) |

## G — GST / invoicing
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| G1 | High | invoicing.generate_credit_note_for_lines | 2nd RMA refund (or RMA after full CN) → IntegrityError on uq_credit_note_original → 500, refund lost | TODO |
| G2 | High | invoice_pdf / storage | Invoice PDFs at guessable sequential keys on the PUBLIC CDN (GSTIN/PII enumerable); dev /files unauthenticated | TODO |
| G3 | Med | generate_credit_note upgrade | partial→full CN upgrade leaves cached pdf_url stale | TODO |
| G4 | Med | partial CN arithmetic | per-component rounding: parts don't sum to line total (1 paisa) | TODO |
| G5 | Med | place of supply | free-text state ("RJ", "08", "rajasthan.") flips CGST/SGST↔IGST | TODO |
| G6 | Med | payments.refund partial | partial refund-to-source issues no credit note (output GST over-reported) | TODO |
| G7 | Med | invoice_pdf._render | PDF missing buyer name/address, separate CGST/SGST/IGST + rates, POS state code, CN ref to original invoice | TODO |
| G8 | Low | orders.generate_order_invoice | returns ORM row → InvoiceOut.lines == [] | TODO |
| G9 | Low | delivery billing vs invoice | billing rounds the sum once, invoice rounds per line → 1-paisa mismatch | TODO |
| G10 | Low | CN generation | empty (zero-value) CN/invoice consumes a serial | TODO |
| G11 | Low | rma.refund | duplicate RMA lines for the same product overwrite each other (dict) | TODO |
| G12 | Low | CN numbering | _today() read twice → fy and date can straddle Mar-31 midnight | TODO |
| G13 | Low | config INVOICE_PREFIX | long prefix overflows invoice_no String(32) → 500 | TODO |

## M — Money & ledgers
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| M1 | High | payments.refund_payment | retry of a partial refund after 502 (pending) issues a 2nd real gateway refund | TODO |
| M2 | High | payments webhook/verify | payment.failed (per attempt) or one bad signature permanently loses a later successful capture | TODO |
| M3 | High | payments.create_payment | abandoned 'initiated' payment blocks the order from ever being paid | TODO |
| M4 | High | loyalty.redeem_gift_card | redeeming deducts the card but credits nothing (money vanishes) | TODO |
| M5 | High | create_payment/_finalize_payment/cancel | cancelled order can be paid and is resurrected 'confirmed' (coupon reuse past limit) | TODO |
| M6 | Med | refund_payment vs RMA | RMA wallet refund + full refund-to-source pays out > order total | TODO |
| M7 | Med | refunds | no admin path to reconcile 'pending' refunds / repair ledger after post-gateway failure | TODO |
| M8 | Med | top-up refund | refunds a top-up already spent/withdrawn (wallet goes negative) | TODO |
| M9 | Med | idempotency middleware | Idempotency-Key > ~125 chars → DataError 500 | TODO |
| M10 | Med | create_payment | ₹0 (fully-discounted) order unpayable (Razorpay min 100 paise → 500); gateway errors 500 not 502 | TODO |
| M11 | Low | billing withdrawals | withdrawal request check not under wallet lock | TODO |
| M12 | Low | loyalty gift-card balance | any user can read another's issued-to card balance | TODO |
| M13 | Low | loyalty._my_account | concurrent first call → IntegrityError 500 | TODO |
| M14 | Low | redeem_referral | one referee can redeem unlimited different codes (farming) | TODO |
| M15 | Low | wallet_adjust | unquantized amount: txn amount and balance_after can diverge by a paisa | TODO |

## C — Cutoff & subscriptions
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| C1 | High | subscriptions write endpoints | delivery driver (scoped to route shops) can edit/pause/cancel customers' subscriptions; notification goes to actor | TODO |
| C2 | High | cutoff rerun | skip/pause/cancel after an earlier run leaves the stale 'generated' order (delivered + billed) | TODO |
| C3 | Med | set_items/pause/resume/cancel | no cutoff enforcement for tomorrow | TODO |
| C4 | Med | set_items | duplicate product lines → permanent double qty + MultipleResultsFound 500 | TODO |
| C5 | Med | set_items | unknown product → FK IntegrityError 500; inactive/unpriced silently accepted | TODO |
| C6 | Med | run_cutoff | per-sub failures swallowed, run marked 'frozen' (no retry/alert) | TODO |
| C7 | Med | worker job timeout | CancelledError leaves ScheduleRun 'generating' forever | TODO |
| C8 | Med | run_cutoff | ~10 round trips per subscription (scale) | TODO |
| C9 | Low | admin cutoff run | accepts today/past dates (rebuilds in-flight orders) | TODO |
| C10 | Low | run_cutoff context | price book loads full history; sub activated between context load and id scan skipped | TODO |
| C11 | Low | create_subscription | concurrent create → uq_subscription_shop 500 | TODO |
| C12 | Low | prepaid gate | ignores other unbilled open orders | TODO |

## D — Delivery & SCM
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| D1 | High | consume_fefo / production | unswept expired lots double-counted; shortfall booked unbatched → negative stock after sweep | TODO |
| D2 | Med | batches lock order | receive_batch locks batch→stock_item, others stock_item→batch; unordered item loops → deadlocks | TODO |
| D3 | Med | transfer dispatch | ships expired lots first (no expiry filter) | TODO |
| D4 | Med | inventory adjust/wastage | changes on-hand but not lots (drift, phantom FEFO units); wastage > on-hand accepted | TODO |
| D5 | Med | stock admin endpoints | batch receive / adjust / wastage lack required Idempotency-Key | TODO |
| D6 | Med | delivery stock location | always deducts default location (vehicle stock never decremented) | TODO |
| D7 | Med | ml routing | pure-Python 2-opt O(n^4), unbounded stops (200 stops 28s) | TODO |
| D8 | Med | production/procurement | unknown/missing location → IntegrityError 500 | TODO |
| D9 | Low | RMA | no restock/write-off movement; undelivered order returnable | TODO |
| D10 | Low | RMA lists | N+1 | TODO |
| D11 | Low | expiry sweep | default as_of uses UTC date | TODO |
| D12 | Low | stock floor | CheckViolation at floor → 500 not 409 | TODO |

## O — Orders / cart / checkout / catalog
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| O1 | High | = M5 | pay-after-cancel resurrects order / coupon reuse | TODO |
| O2 | Med | cancel_order | customer can cancel a COD order that has a live shipment | TODO |
| O3 | Med | admin cancel | no status guard (delivered/shipped cancellable); refunded order can never be cancelled | TODO |
| O4 | Med | cart add/set | merged qty uncapped → checkout ValidationError 500 / Numeric overflow | TODO |
| O5 | Med | cart_items | no UNIQUE(cart_id, product_id): concurrent adds duplicate → MultipleResultsFound 500 | TODO |
| O6 | Med | coupons | reservations on abandoned unpaid orders never released | TODO |
| O7 | Med | cart pricing | cart/quote tier uses first shop, checkout uses home shop → price mismatch | TODO |
| O8 | Med | reviews | not restricted to purchasers, auto-approved; retired products rateable | TODO |
| O9 | Med | = M10 | ₹0 order unpayable | TODO |
| O10 | Low | home shop | no partial unique index on (account_id) WHERE is_home → duplicate home shops | TODO |
| O11 | Low | cart checkout / reorder | ship state/pincode dropped → wrong GST head | TODO |
| O12 | Low | order line merge | merged qty bypasses per-line cap → Numeric overflow 500 | TODO |
| O13 | Low | qty precision | fractional qty for piece/pack; stored qty (3dp) vs priced qty | TODO |
| O14 | Low | product schema | gst_rate unbounded on create (Numeric(4,2) overflow 500, negatives); pack_size > 0 | TODO |
| O15 | Low | wishlist/variants | retired products accepted/listed | TODO |

## A — Auth & RBAC
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| A1 | High | = C1 | driver can mutate subscriptions | TODO |
| A2 | Med | Dockerfile/railway.json | --forwarded-allow-ips '*' → spoofable client IP bypasses OTP/IP rate limits | TODO |
| A3 | Med | management create staff | phone not normalized → staff can never log in | TODO |
| A4 | Low | erase/reactivate | erased user can be reactivated; sessions not revoked by erasure | TODO |
| A5 | Low | otp verify | suspended users get 200 + tokens | TODO |
| A6 | Low | logout/reuse detection | token_version not bumped (stolen access token lives 15 min) | TODO |
| A7 | Low | phone normalize | foreign 11-15 digit numbers mapped to last 10 digits | TODO |
| A8 | Low | account email | email unvalidated / unverified (squat → erasure deletes owner's newsletter) | TODO |
| A9 | Low | otp verify | concurrent correct verifies both pass (non-atomic get+delete) | TODO |

## P — DPDP / compliance
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| P1 | High | dpdp.erase_user | home-shop address/geo kept; subscriptions keep billing erased user | TODO |
| P2 | Med | deliver_notification / resend | marketing consent not re-checked at send (deferred/quiet-hours/retry/resend) | TODO |
| P3 | Med | push marketing | no consent purpose for push; marketing_opt_in settable without consent ledger | TODO |
| P4 | Med | senders in prod | unconfigured channel stub logs phone+body and reports success | TODO |
| P5 | Med | dpdp.export | export misses many PII columns/tables | TODO |
| P6 | Low | erase | queued notifications still sent (to= blank) | TODO |
| P7 | Low | logs/URLs | newsletter logs email; FCM token in DELETE path; newsletter confirm tokens never expire | TODO |

## N — Notifications
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| N1 | High | emit_event | profile get-or-create outside savepoint → IntegrityError poisons caller txn (money write lost, 500) | TODO |
| N2 | Low | stuck 'sending' rows | no surfacing; resend of in-flight row can double-send | TODO |
| N3 | Low | device register | concurrent first register → UNIQUE 500 | TODO |
| N4 | Low | push tokens | resolved at emit time, deactivated token still pushed | TODO |

## I — Infra / cross-cutting
| ID | Sev | Where | Finding | Status |
|---|---|---|---|---|
| I1 | High | main.limit_body_size / idempotency / uploads | chunked body bypasses 8MB cap → unauth memory exhaustion; upload reads whole file before size check | TODO |
| I2 | Med | content PATCH | explicit null on NOT NULL fields → IntegrityError 500 | TODO |
| I3 | Med | churn scoring | concurrent admin run + nightly → IntegrityError / deadlock; inline in request | TODO |
| I4 | Med | outbound webhook SSRF guard | CGNAT 100.64/10 allowed (not is_global); dev bypass prefix matches real hosts | TODO |
| I5 | Med | support lists / forecasting | N+1 | TODO |
| I6 | Med | /sitemap.xml | loads full article bodies unbounded, uncached | TODO |
| I7 | Low | /readyz | leaks raw exception text | TODO |
| I8 | Low | AI quota | fails open when Redis down (unbounded paid spend); retries non-transient 4xx | TODO |
| I9 | Low | idempotency middleware | stores token-bearing auth responses for 48h | TODO |
| I10 | Low | rate limiter | incr/expire not atomic | TODO |
| I11 | Low | prod guards | split between main.py and config; worker never imports main; R2 not required in prod | TODO |

## T — Tests
(filled in from the test-suite review)
