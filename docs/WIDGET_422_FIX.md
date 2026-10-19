# Widget document checks and 422 handling

A customer acceptance attempt showed “Скидка не рассчитана точно. Проверьте позиции и НДС.” The old UI used this message for every HTTP 422, including the initial order load. The actual customer document and failing phase were not supplied, so its exact cause remains unverified.

## Confirmed defects corrected

- REMAP customer orders with `vatEnabled: false` can omit `vatIncluded`. Accept this absence only when VAT is explicitly disabled. Preserve explicit boolean values so existing immutable redemption plans remain comparable; reject null, non-boolean, and unsupported exclusive-VAT inputs.
- Viewing a saved order no longer requires eligibility for a new discount. Paid, shipped, unposted, or linked-document orders show the bonus balance and a precise read-only reason. Preparation and initial confirmation keep their financial guards.
- Widget 422 responses carry an allowlisted reason, actual request stage, and opaque correlation code. Logs contain only those values. The UI never displays provider exception text or response bodies.
- An exactly unrepresentable bonus amount is rejected before reservation. The input remains available to choose a different amount. Unknown outcomes and verification failures keep the existing reservation protections.

The arithmetic, two-decimal discount representability, delivery exclusion, saved-document readback, tenant checks, and idempotent debit rules are retained. Exclusive VAT and unsupported document shapes remain explicit limitations.

## Evidence and validation

Official REMAP customer order contract and examples:
[customerOrder documentation](https://github.com/moysklad/api-remap-1.2-doc/blob/master/md/documents/_customerOrder.md).

Regression coverage includes the real HTTP JSON parser, PostgreSQL reservation/debit round trips, disabled-VAT omission, explicit legacy tax flags, fractional quantity with ordinary discount, read-only orders, linked drafts, redacted diagnostics, and Chromium recovery flows.

Automated provider responses and a synthetic browser host verify these contracts. They do not establish that the customer's particular live order works. After deploying, repeat the failing order and a full real earn/spend/return scenario before handover. If it fails, match the displayed BP code to `WIDGET_REJECT` in the app log; no prices, customer identifiers, credentials, or provider body are logged.

The `bonuspro/vendor-auth-fix` patch keeps schema 0013 compatibility. The rebuild branch retains its schema 0015 processing features. This patch does not activate a program, change its cutoff, or run migrations.
