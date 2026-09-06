#!/usr/bin/env python3
"""Keep the existing subscription app, moving only its TLS listener to loopback."""

import argparse
import importlib.util


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="/root/verge_sub/server.py")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("verge_subscription_backend", args.server)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_https_server = module.SubscriptionHTTPSServer

    class LoopbackHTTPSServer(original_https_server):
        def __init__(self, server_address, handler, tls_context):
            super().__init__(("127.0.0.1", 18443), handler, tls_context)

    module.SubscriptionHTTPSServer = LoopbackHTTPSServer
    # HTTP 80/8080, handlers, certificates and subscription URLs stay intact.
    module.main()


if __name__ == "__main__":
    main()
