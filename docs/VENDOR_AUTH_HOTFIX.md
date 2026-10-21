# Vendor authentication hotfix (schema 0013)

This branch starts at the deployed schema-0013 release 8a55609a3d99. No database migration, financial behavior, master encryption key or provider secret change is required.

Fix the inbound JWT contract: NumericDate accepts finite JSON numbers (including fractions); the 300-second maximum belongs to developer-to-MoySklad JWTs and must not reject provider-to-developer callbacks. Verify HS256 signatures, required claims, expiration, not-before, future issuance, token purpose and persisted jti replay protection. Fractional issuance is normalized to whole seconds only after validation for the existing lifecycle ordering column.

Official contract: https://dev.moysklad.ru/doc/api/vendor/1.0/#autentifikaciq-vzaimodejstwiq-po-vendor-api

Failures log VENDOR_AUTH_REJECT followed by a fixed reason code only. Never log Authorization, JWT claims, access_token, key material, exception messages or callback bodies. HTTP failures remain generic 401. reason=signature means the received signature did not validate with the loaded key; reason=expired/not_yet_valid requires checking provider/server clocks. reason=replay identifies an already consumed jti. Other codes identify header, format, NumericDate, missing claim, jti or purpose validation.

This corrects a verified contract mismatch; it does not prove the cause of the user's redacted 401. Repeat a real installation after deployment. If it still fails, read fixed diagnostic codes from app container logs. Do not disable signature checks or jti replay protection.

Build this exact branch in a separate git worktree. Confirm the production schema is 0013 before replacing only the image in .env.release and recreating app. If production has advanced to schema 0015, use the forward-port on bonuspro/rebuild instead. Preserve the old image tag and configuration for rollback; never downgrade the financial schema.

## Header interoperability

The live callback on 2026-10-14 still failed with reason=header after matching keys and a container recreation. This establishes a rejection in the old combined alg/typ/crit check, but does not establish which field differed.

Accept absent typ or case-insensitive JWT metadata as allowed by RFC 7519 section 5.1 (https://www.rfc-editor.org/rfc/rfc7519#section-5.1). MoySklad's examples show typ=JWT; absence acceptance is an interoperability tolerance, not a claim that the live provider omitted it. Signature verification remains pinned to HS256. No unsigned token or HS384/HS512 token is accepted. Outbound JWT reflection, expiry, not-before, future issuance and persistent replay checks still apply.

Only after complete JWT validation, an absent typ produces the fixed compatibility marker VENDOR_AUTH_COMPAT typ=absent. Header rejection codes distinguish header_algorithm_hs384, header_algorithm_hs512, header_algorithm_none, header_algorithm_other, header_algorithm_invalid, header_type and header_critical. Arbitrary header contents are never logged. Unknown critical extensions are rejected, including a malformed empty crit array.

Tests cover valid installation without typ, wrong signatures without typ, capitalization, expired/purpose/replayed tokens and algorithm or critical-extension rejection. The actual installation must be repeated to verify the production result; do not claim these synthetic tests prove provider acceptance.
