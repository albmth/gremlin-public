# Demo domain

A Cloudflare Worker that forwards the demo domain to the website's Lambda Function URL.
The site sets `PublicOrigin` to the domain, so the Function URL redirects pages there and refuses API calls that skip it.

The Worker proves itself with `PROXY_KEY`, an HMAC of the website's Gremlin token (`proxy_key` in `web/gremlin_web/app.py`), stored only as a Worker secret:

```sh
cd infra/cloudflare/demo-proxy
python3 -c 'import hashlib, hmac, json; t = json.load(open("../../aws/.state/prod-client-gremlin-web.json"))["token"]; print(hmac.new(t.encode(), b"gremlin-web-proxy", hashlib.sha256).hexdigest())' \
  | npx wrangler secret put PROXY_KEY
npx wrangler deploy --var ORIGIN:https://<function-url>.lambda-url.eu-north-1.on.aws
```

Set the route in `wrangler.toml` to your own domain.
Rotating the website's Gremlin token changes the key; put the new one or the domain answers 403.
