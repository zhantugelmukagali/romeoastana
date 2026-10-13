# Customer-order widget — stage 2

The widget is at /widget, embedded using marketplace/descriptor-widget.xml (v2).
The previous registration descriptor remains available. This candidate adds own attribute metadata
and customer-order create/edit extension points; changed permissions require a provider installation
update/reinstallation and real acceptance. It does not enable Loyalty/POS API.

## Implemented path
1. Open resets all cached document/session state. A new unsaved order asks the employee to save first.
2. A one-use user-context token is exchanged server-side. Tokens stay in memory.
3. The backend reads the saved order, all positions and linked documents from the fixed REMAP host.
4. It pins the rule version, maps the customer's UUID to the wallet, and returns goods-only spend availability.
5. Prepare re-reads the order, rejects a changed source, atomically reserves funds with an immutable discount plan,
   and returns UpdateRequest data including **every** existing line and a unique operation marker.
6. The host edits the form under the employee's own permissions. UpdateResponse is NOT proof of persistence.
7. Save triggers verification. The backend re-reads the provider and compares exact lines, prices, quantities,
   ordinary and bonus discounts, VAT flags, packs, customer, organization, currency, total and marker.
8. Only a complete match commits the reserved points. Concurrent verifications produce one ledger transaction.

Delivery and other services are excluded; product, variant and bundle positions participate.
Delivery must actually be recorded as a service in MoySklad. We do not infer eligibility from a name.

## Money and unsupported cases
Base = saved goods total after ordinary discounts and before the new bonus discount.
Only KZT, positive saved quantities, up to 1000 positions, non-negative discounts up to 100%,
and VAT included (or disabled) are supported by this implementation.
Amounts are parsed with Decimal and checked against the saved provider sum.
The additional discount is allocated using largest remainders. A plan is emitted only if each target line
amount is representable exactly with a 0.01% discount step. It never silently substitutes a different
number of points. Non-representable amounts, unknown assortments, exclusive VAT, unpaid-but-linked
documents and incompatible totals fail closed. These need real-provider UX/rounding acceptance
and broader support before general release.

## Setup and release gate
POST /api/admin/widget/setup provisions the dedicated string marker field using useOwnAttributeMetadata
and binds its UUID to this tenant. Its name is “БонусПро: операция списания”.
This is an administrator action; no production metadata was changed by the development tests.
A timeout during metadata provisioning is resolved by reading the named metadata on the next attempt;
generic document writes are not exposed by the HTTP client.

Installation **Activated** is required to prepare/commit a redemption. Lifecycle installation still returns
SettingsRequired. Do not force Activated in production: stages 3–4 (automatic accrual/returns), reconciliation
and actual installation acceptance must pass first. Only isolated CI fixtures activate a synthetic account.

Document endpoints currently require the MoySklad **admin** role. The user-context protocol's coarse worker
role is not treated as permission to use the application's broader token on arbitrary customer orders.
A deliberate employee authorization policy and real non-admin acceptance are still required for rollout.

## Uncertain results
Prepared operations enter verifying before data leaves the server. An interrupted response, rejected form
update, page close, amount mismatch or failed provider request cannot release that reservation automatically.
A retry returns the same operation without redispatching stale form data.
There is no general cancel/release button in this stage. Background reconciliation and an audited recovery
workflow must be supplied in the following stages; this is a release blocker, not a hidden success path.

Change disables new application until the employee saves; refresh alone does not clear dirty state.
On a cached Open the old document state is discarded, and late results are ignored using an epoch.
Host events are accepted only from the actual parent and exact MoySklad origin.

## Validation and deployment
Tests exercise real PostgreSQL transactions and Chromium with synthetic provider/host transports.
They do not prove the actual MoySklad permissions, schema acceptance, tax rounding, marker merge semantics,
document editing or competing BonusPlus behavior. These remain explicit acceptance items.
Schema 0007 adds immutable RLS-protected attempts and marker bindings. Application readiness requires 0007.
The VPS is still on its previous image/schema: do not run migrations separately from a coordinated image upgrade.

Official contracts reviewed:
- https://dev.moysklad.ru/doc/api/vendor/1.0/#vidzhety
- https://dev.moysklad.ru/doc/api/remap/1.2/documents/#dokumenty-zakaz-pokupatelq
- https://github.com/moysklad/js-widget-sdk
