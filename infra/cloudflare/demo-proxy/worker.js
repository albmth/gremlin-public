// Forwards every request to the website's Function URL unchanged, adding the proxy key and the
// visitor's own address, which the site uses for its per-address quotas (web/gremlin_web/app.py).
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    // Never pass on a visitor's own claim to be the proxy.
    headers.delete("x-gremlin-proxy");
    headers.delete("x-gremlin-client-ip");
    headers.set("x-gremlin-proxy", env.PROXY_KEY);
    headers.set("x-gremlin-client-ip", request.headers.get("cf-connecting-ip") || "");
    const hasBody = !["GET", "HEAD"].includes(request.method);
    return fetch(env.ORIGIN + url.pathname + url.search, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      redirect: "manual",
    });
  },
};
