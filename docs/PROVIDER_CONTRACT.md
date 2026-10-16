# Vendor API contract

Reviewed 2026-10-16 against the official [Vendor API source](https://github.com/moysklad/api-vendor-1.0-doc/tree/e3b41a2eda96b1abd074f4f64712d06f927ec9f5/source/includes).
The scoped audit and acceptance limits are in [MOYSKLAD_CONTRACT_AUDIT.md](MOYSKLAD_CONTRACT_AUDIT.md).

## Authentication and context

Installation is PUT and deactivation is DELETE under
/api/moysklad/vendor/1.0/apps/{appId}/{accountId}.
Incoming JWT uses HS256, required iat/exp/jti and a consumed one-time jti;
outgoing JWT uses appUid as subject and a fresh jti for each attempt.
Wrong purpose, algorithm, signature, critical headers, expired token and replay fail closed.
Headers used by the real host are covered by compatibility tests.

Context exchange uses POST https://apps-api.moysklad.ru/api/vendor/1.0/context/user
with its short-lived token in JSON, never in a URL or persistent browser storage.
Responses identify accountId, userId and role.
Both context exchange and activation explicitly send Accept-Encoding: gzip.
Activation uses PUT https://apps-api.moysklad.ru/api/vendor/1.0/apps/{appId}/{accountId}/status,
body {"status":"Activated"}; only HTTP 200 confirms the remote acknowledgement.

## Lifecycle

- Install supplies a REMAP credential and returns SettingsRequired until an administrator
  configures the marker, rules and immutable cutover and completes remote activation.
- Suspend revokes the local credential and sessions, preserving configuration and journal.
- Resume supplies a new credential. Activated is restored only from Suspended when a completed
  administrator approval still matches the marker and current rule version.
  The original accrual cutoff is preserved. Otherwise it returns SettingsRequired.
- Uninstall revokes credentials and sessions. A later Install does not automatically
  activate the preserved program.
- TariffChanged and Autoprolongation do not carry access tokens. They acknowledge the
  existing active/setup state without replacing a credential, session generation or configuration.
  They cannot activate an inactive installation.
- PermissionsChanged is declared with additionalEvents/updatePermissions. Its signed event
  carries access permissions without a replacement token. Both documented path variants
  /api/moysklad/vendor/1.0/apps/{appId}/{accountId}/event and
  /api/vendor/1.0/apps/{appId}/{accountId}/event are accepted.
  The event does not install or activate anything. REMAP enforces current permissions on each
  provider request; this application keeps no positive permissions cache.

Lifecycle storage uses its own least-privileged database login. A callback never
creates a financial journal transaction. New installs and changed configurations
require explicit administrator setup; a matching prior approval may survive Resume.

Saving the repository descriptor does not update the descriptor in the developer
cabinet or grants of the installed version. No release is considered accepted
before installation and financial tests in a real MoySklad account.
