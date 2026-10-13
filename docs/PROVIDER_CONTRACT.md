# Vendor API contract
Verified against the official documentation on 2026-10-13:
https://dev.moysklad.ru/doc/api/vendor/1.0/

Installation is PUT and deactivation is DELETE under /api/moysklad/vendor/1.0/apps/{appId}/{accountId}.
Incoming JWT uses HS256, expiry and a one-time jti. Outgoing JWT includes appUid as subject.
Context exchange uses POST https://apps-api.moysklad.ru/api/vendor/1.0/context/user with a token in JSON.
Responses identify accountId, userId and role. Requests explicitly accept gzip.
Provider credentials are installation-scoped and must be invalidated on suspension/uninstall.

Implementation covers Install, Resume, Suspend and Uninstall, with separate database credentials for lifecycle.
Other optional lifecycle events are not declared or implemented.
No descriptor is considered release-ready before a real installation test.
A SettingsRequired response is deliberate until actual program configuration and financial integration are complete.
No callback may enable production bonus operations merely by installing a credential.
