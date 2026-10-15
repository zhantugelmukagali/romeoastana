# Compact cabinet and safe widget interaction

The cabinet uses one local page header below the MoySklad host title, an optional collapsed sidebar, and a keyboard-focusable inner scroll region. The host title belongs to MoySklad and is not changed by app CSS. The customer shortcut is /; it never intercepts typing in fields or modal dialogs.

The widget disables spending for empty/invalid amounts, configuration restrictions, outstanding review, unsaved changes, or missing current evidence. Failed refreshes invalidate the previous quote. Amount previews use integer minor units with exact decimal rendering. Late unauthorized responses from an old order cannot clear the current order session.

A host UpdateResponse is only an acknowledgement. The saved document must still match the planned financial changes and marker before success is shown. Unknown prepare results keep spending blocked; refresh finds the existing server operation. No blind repeated mutation or automatic reserve cancellation is added.

Playwright covers constrained iframe scrolling, compact layout, navigation, amount validation, unavailable and blocked states, keyboard submission, stale refresh and old-order response races, and existing save/readback and security scenarios. Docker checks compare static assets through real Uvicorn/Caddy transports. These checks use isolated fixtures; acceptance of a real MoySklad sale and return remains required before broad rollout.

This change requires no schema migration or new secrets. Deploy the schema-0013-compatible branch through deploy/update_cabinet.py; main rebuild remains schema 0015.
