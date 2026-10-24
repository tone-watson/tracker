// Adapted from Farm Manager centralFetch.ts; same encrypted session / fixed authority contract.
/** BFF-only transport: one configured authority, no redirects/cookies or token URLs. */
import http from "node:http";
import https from "node:https";
const API_BASE_URL = "https://noiseflood.farm";
const API_HOST = "noiseflood.farm";
export function createCentralFetch({ credential = process.env.TRACKER_BFF_CLIENT_CREDENTIAL ?? "", baseURL = API_BASE_URL, host = API_HOST } = {}) {
    return async (input, init = {}) => {
        const logical = new URL(String(input));
        if (logical.origin !== "https://noiseflood.farm" ||
            !/^\/v1\/auth\/(?:login|logout|me|refresh|service-token|services|capabilities|passkeys\/exchange|app-session\/(?:authorize|exchange))$/.test(logical.pathname) ||
            logical.search ||
            logical.hash ||
            logical.username ||
            logical.password)
            throw new Error("Untrusted BFF authority request");
        if (!/^fbff_[A-Za-z0-9_-]{32,}$/.test(credential))
            throw new Error("BFF client credential is unavailable");
        const base = new URL(baseURL);
        if (base.username ||
            base.password ||
            base.search ||
            base.hash ||
            (base.protocol !== "https:" &&
                !(base.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname))))
            throw new Error("Invalid BFF authority configuration");
        const headers = new Headers(init.headers);
        headers.delete("cookie");
        headers.set("User-Agent", "farm-tracker-web/1");
        headers.delete("x-api-key");
        headers.set("Host", host);
        headers.set("X-Farm-Client-Credential", credential);
        const body = init.body == null ? undefined : String(init.body);
        if (body)
            headers.set("Content-Length", String(Buffer.byteLength(body)));
        const target = new URL(logical.pathname, base);
        return new Promise((resolve, reject) => {
            const request = (target.protocol === "https:" ? https : http).request(target, { method: init.method ?? "GET", headers: Object.fromEntries(headers) }, (response) => {
                const chunks = [];
                let size = 0;
                response.on("data", (chunk) => {
                    size += chunk.length;
                    if (size > 256 * 1024)
                        request.destroy(new Error("Authority response too large"));
                    else
                        chunks.push(chunk);
                });
                response.on("error", reject);
                response.on("aborted", () => reject(new Error("Authority response interrupted")));
                response.on("end", () => {
                    const status = response.statusCode ?? 502;
                    if (status >= 300 && status < 400) {
                        reject(new Error("Authority redirect rejected"));
                        return;
                    }
                    const resultHeaders = new Headers();
                    for (const name of ["content-type", "retry-after"]) {
                        const value = response.headers[name];
                        if (typeof value === "string")
                            resultHeaders.set(name, value);
                    }
                    resolve(new Response([204, 205, 304].includes(status) ? null : Buffer.concat(chunks), {
                        status,
                        headers: resultHeaders
                    }));
                });
            });
            const abort = () => request.destroy(new Error("Authority request cancelled"));
            const timer = setTimeout(() => request.destroy(new Error("Authority deadline exceeded")), 15000);
            if (init.signal?.aborted)
                abort();
            else
                init.signal?.addEventListener("abort", abort, { once: true });
            request.once("close", () => {
                clearTimeout(timer);
                init.signal?.removeEventListener("abort", abort);
            });
            request.on("error", reject);
            request.end(body);
        });
    };
}
