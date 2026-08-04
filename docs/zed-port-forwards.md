# Zed remote port-forwards for the rask estate

Zed does not forward ports automatically over SSH — they must be declared in your LOCAL
`settings.json` under `ssh_connections[].port_forwards`. Paste `docs/zed-port-forwards.json`
into it (merge with any `ssh_connections` you already have).

Only `local_port` 8080 differs from its remote: binding :80 locally needs admin on most hosts.

```

   localhost:8080  -> remote 80     k3s traefik ingress — THE app UI (catch-all zone routing, as in prod)
   localhost:3024  -> remote 3024   microfrontends composing proxy (scripts/kind-browse.sh, make dev-frontends)
   localhost:8888  -> remote 8888   gateway API — /api/* (the Vite proxy target)
   localhost:6006  -> remote 6006   Storybook for @rask/ui (make storybook)
   localhost:8265  -> remote 8265   Ray dashboard (make ray-up)
   localhost:8804  -> remote 8804   compute service — /api/ray, /api/serve
   localhost:8820  -> remote 8820   controlplane — /api/projects
   localhost:8101  -> remote 8101   media viewer
   localhost:5000  -> remote 5000   dev image registry
   localhost:5173  -> remote 5173   SvelteKit zone dev server
   localhost:5174  -> remote 5174   SvelteKit zone dev server
   localhost:5175  -> remote 5175   SvelteKit zone dev server
   localhost:5176  -> remote 5176   SvelteKit zone dev server
   localhost:5177  -> remote 5177   SvelteKit zone dev server
   localhost:5178  -> remote 5178   SvelteKit zone dev server
   localhost:5179  -> remote 5179   SvelteKit zone dev server
   localhost:5180  -> remote 5180   SvelteKit zone dev server
   localhost:5273  -> remote 5273   SvelteKit zone dev server
   localhost:5274  -> remote 5274   SvelteKit zone dev server
   localhost:5275  -> remote 5275   SvelteKit zone dev server
   localhost:5276  -> remote 5276   SvelteKit zone dev server
   localhost:5277  -> remote 5277   SvelteKit zone dev server
   localhost:5278  -> remote 5278   SvelteKit zone dev server
   localhost:5279  -> remote 5279   SvelteKit zone dev server
   localhost:5280  -> remote 5280   SvelteKit zone dev server
   localhost:9273  -> remote 9273   kind-browse per-zone port-forward
   localhost:9274  -> remote 9274   kind-browse per-zone port-forward
   localhost:9275  -> remote 9275   kind-browse per-zone port-forward
   localhost:9276  -> remote 9276   kind-browse per-zone port-forward
   localhost:9277  -> remote 9277   kind-browse per-zone port-forward
   localhost:9278  -> remote 9278   kind-browse per-zone port-forward
   localhost:9279  -> remote 9279   kind-browse per-zone port-forward
   localhost:9888  -> remote 9888   kind-browse gateway port-forward
```

Equivalent plain ssh, if you are not in Zed:

```bash
ssh -L 8080:127.0.0.1:80 -L 10350:127.0.0.1:10350 -L 3024:127.0.0.1:3024 -L 8888:127.0.0.1:8888 -L 6006:127.0.0.1:6006 -L 8265:127.0.0.1:8265 -L 8804:127.0.0.1:8804 -L 8820:127.0.0.1:8820 -L 8101:127.0.0.1:8101 -L 5000:127.0.0.1:5000 -L 5173:127.0.0.1:5173 -L 5174:127.0.0.1:5174 -L 5175:127.0.0.1:5175 -L 5176:127.0.0.1:5176 -L 5177:127.0.0.1:5177 -L 5178:127.0.0.1:5178 -L 5179:127.0.0.1:5179 -L 5180:127.0.0.1:5180 -L 5273:127.0.0.1:5273 -L 5274:127.0.0.1:5274 -L 5275:127.0.0.1:5275 -L 5276:127.0.0.1:5276 -L 5277:127.0.0.1:5277 -L 5278:127.0.0.1:5278 -L 5279:127.0.0.1:5279 -L 5280:127.0.0.1:5280 -L 9273:127.0.0.1:9273 -L 9274:127.0.0.1:9274 -L 9275:127.0.0.1:9275 -L 9276:127.0.0.1:9276 -L 9277:127.0.0.1:9277 -L 9278:127.0.0.1:9278 -L 9279:127.0.0.1:9279 -L 9888:127.0.0.1:9888 blackwell@10.16.51.53
```
