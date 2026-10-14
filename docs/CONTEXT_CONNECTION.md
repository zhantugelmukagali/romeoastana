# Browser context connection diagnostics

Schema-compatible browser-only change; no migration or provider credential changes.

The deployed page's exact timeout text originates in bridge.js before /api/session. The actual saved descriptor includes user-context and useContextKey=false. This narrows the investigation to host-window delivery, frame source/origin, descriptor installation/cache or host protocol support; it does not establish a backend Vendor exchange failure.

Send UserContextRequest on the next task after the iframe's window load (or next task when already loaded). This avoids sending during module evaluation before the parent's iframe onload transport registration. It is a robustness fix; retry also timed out in production, so it is not asserted to be the sole cause.

Recognize InvalidMessageError with correlationId or an exact invalidMessage.messageId when correlationId is null, as documented. Expose only allowlisted numeric error codes; never display arbitrary provider errors, invalidMessage content, tokens or claims.

A timeout displays a fixed support code BP-CTX-P?H?O?R?S?E?.
P: any message from the immediate parent was seen during this attempt.
H: such a message also had the exact trusted https://online.moysklad.ru origin.
O: a trusted Open event was seen (possibly before this attempt).
R: a trusted UserContextResponse event was seen, even if correlation did not match.
S: a trusted parent sent a string rather than a protocol object.
E: a trusted InvalidMessageError event was seen, even if correlation did not match.
Each flag is 0 or 1; no message fields or identifiers are stored or printed.
Messages with another source or origin remain rejected; no wildcard targetOrigin is used.

Official protocol: https://dev.moysklad.ru/doc/api/vendor/1.0/#protokol-konteksta-pol-zowatelq
Official SDK: https://github.com/moysklad/js-widget-sdk/blob/master/src/core.js

Regression tests use a host that does not register its transport until iframe load, null-correlation errors, unrelated parent messages and untrusted self messages. Production acceptance remains pending until real context exchange succeeds. The current draft main window points at /admin (returns). Program setup and the customer-order widget require the separately supplied complete descriptor; do not enable sales until actual financial acceptance.
