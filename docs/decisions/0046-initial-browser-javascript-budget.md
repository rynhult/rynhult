# Initial browser JavaScript budget

Renumbered on 2026-10-24 from 0015 to 0046.

Date: 2026-10-24.

The eager route graph measured 390.18 KiB gzip through the build manifest. Vite's warning about
one large raw chunk did not prevent payload growth or account for several statically imported chunks.

Load substantial pages on demand and enforce **250 KiB gzip** for the complete initial static
JavaScript graph during every production build. Follow manifest imports, deduplicate shared files,
and fail when required manifest entries or files are absent. The current graph measures 170.09 KiB,
leaving room for small changes while rejecting the previous eager build. Keep Vite's warning threshold.

Lazy downloads have a visible loading state and a reload recovery action. Route-specific JavaScript,
CSS and fonts are outside this gate; the limit is not a claim about total first-page bytes, device latency
or all routes' payloads. Measure those separately when investigating page performance.

A compressed graph budget is preferable to a largest-chunk limit, which can be bypassed by splitting
one eager file into several eager files. A warning-only gate or increasing Vite's warning threshold
would leave the original regression unprotected. Broad manual vendor chunk rules would add maintenance
without changing which code the first page needs. Budget changes should be justified by measured output.
