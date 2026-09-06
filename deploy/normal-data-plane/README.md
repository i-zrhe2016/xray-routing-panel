# Normal data-plane node

For a shared Clash/HTTPS port 443 with legacy proxy aliases, see
[the unified entry deployment procedure](../../docs/unified-entry.md).

This directory contains the minimal Docker Compose definition used to run the
normal Xray data plane on a replacement host. It intentionally does not run
the control plane or the AI node.

When the host firewall is managed by `ai-routing-panel-firewall.timer`, install
`sync-ai-routing-panel-firewall.sh` as the timer's synchronizer as well; it
accounts for the Unix-socket Xray inbounds and the public HAProxy/legacy alias
listeners used by the unified entry.

The deployment directory on a node is expected to have this shape:

```text
/root/xray-routing-panel/
├── docker-compose.node.yml
└── app/xray/
    ├── runtime/config.json
    └── logs/
```

Start or recover the node with:

```bash
docker compose -f docker-compose.node.yml up -d xray-reality
docker compose -f docker-compose.node.yml ps
```

The original node must remain online until the replacement passes the Xray
configuration test, port probe, and client smoke test. Roll back by switching
the DNS/entrypoint to the original node and stopping only the replacement
service after traffic has drained.
