# 0067 — Preview element descriptions cross as untrusted data

Date: 2026-10-25. Status: accepted for the requested picker slice.

The live preview is intentionally on a different origin from Rynhult. A picker must identify rendered elements
without giving the running app dashboard credentials or the ability to type into sessions.

Use a gateway-owned wrapper on the existing preview origin and a same-origin child app frame. A small protocol
checks both origins and frame sources; a one-use pick id and bounded typed payload limit the selection flow.
Only preview responses add `'self'` to `frame-ancestors` for the wrapper; the dashboard's `frame-ancestors 'none'`
remains unchanged. Only the authenticated dashboard can prepare a redacted session message, display it for confirmation, and consume
a short-lived login/session/project/port-bound draft. Page text is always framed as untrusted context.

This preserves the origin boundary and shares existing gateway authorization and session send paths for local and
remote apps. The wrapper and page can influence each other, so descriptions are never authenticated evidence.
Cross-origin navigations are unavailable for inspection. Screenshot crops and source inference remain deferred.

Alternatives: sharing ACC's origin would expose the dashboard; browser extensions need installation; rewriting
streamed app HTML requires compression/encoding handling and changes app responses. The wrapper avoids those costs.
