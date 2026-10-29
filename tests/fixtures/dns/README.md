# DNS wire fixtures

These are **synthetic response transcripts**, authored from the official API schemas, not captures of live accounts.
Each transcript covers create, update, drift/ownership refusal and delete. No credential or account data was recorded.
The in-memory Cloudflare API additionally models ambiguous writes and a copied ownership marker.

Sources: [Cloudflare DNS records](https://developers.cloudflare.com/api/resources/dns/subresources/records/),
[Azure DNS record sets](https://learn.microsoft.com/en-us/rest/api/dns/record-sets),
[Route 53 API](https://docs.aws.amazon.com/Route53/latest/APIReference/API_Operations.html),
[Google Cloud DNS API](https://docs.cloud.google.com/dns/docs/reference/v1),
[DigitalOcean Domains API](https://docs.digitalocean.com/reference/api/reference/domains/),
and [Hetzner Cloud DNS API](https://docs.hetzner.cloud/reference/cloud#dns).

Wave 2 transcripts also include a second record-list page and a foreign-record refusal. Request bodies are checked
against these transcripts, including exact old Google RRsets and Hetzner action endpoints. Error tests substitute
fixed refusal responses and prove sticky uncertain intents block a retry; no live response body is retained.

Owner qualification must supply real trial-zone results before a provider is marked qualified. Do not replace
these files with raw production recordings. Redact account identifiers, Authorization and response secrets first;
review every recording before committing it.
