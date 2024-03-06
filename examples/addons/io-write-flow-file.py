"""
Generate a mitmproxy dump file.

This script demonstrates how to generate a mitmproxy dump file,
as it would also be generated by passing `-w` to mitmproxy.
In contrast to `-w`, this gives you full control over which
flows should be saved and also allows you to rotate files or log
to multiple files in parallel.
"""

import random
import sys
from typing import BinaryIO

from mitmproxy import http
from mitmproxy import io


class Writer:
    def __init__(self, path: str) -> None:
        self.f: BinaryIO = open(path, "wb")
        self.w = io.FlowWriter(self.f)

    def response(self, flow: http.HTTPFlow) -> None:
        if random.choice([True, False]):
            self.w.add(flow)

    def done(self):
        self.f.close()


addons = [Writer(sys.argv[1])]
