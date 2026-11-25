FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS build
WORKDIR /opt/bud
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

ARG TARGETARCH
RUN .venv/bin/python - "$TARGETARCH" <<'PY'
import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

arch, digest = {
    "amd64": ("x64", "4C5eUD1WkeozFRTsnSWRCzeMHbVs3FOp2QEI5+/yqJSFy00Sc+E7YvC01lSnEEmdWy85AMUCsrqqwNAFJTGa1A=="),
    "arm64": ("arm64", "EEOo0Sk4jDtifLW40CHjMiGlRgOW/SSNXtqUnomppXUqIVGDmlXRzPkQrm/26t0lwZOUdSkxro0yAML80Tw9nA=="),
}[sys.argv[1]]
version = "0.1.5"
name = f"browse-linux-{arch}"
url = f"https://registry.npmjs.org/@opencode/{name}/-/{name}-{version}.tgz"
with urllib.request.urlopen(url, timeout=30) as response:
    data = response.read()
if hashlib.sha512(data).digest() != base64.b64decode(digest):
    raise SystemExit("Browse archive checksum did not match.")
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    archive.extractall("/opt/bud/browse", members=[
        archive.getmember("package/bin/browse"),
        archive.getmember("package/LICENSE"),
    ], filter="data")
Path("/opt/bud/browse/package/bin/browse").chmod(0o755)
PY

FROM python:3.14-slim-trixie
COPY --from=build /opt/bud/.venv /opt/bud/.venv
COPY --from=build /opt/bud/browse/package/bin/browse /usr/local/bin/browse
COPY --from=build /opt/bud/browse/package/LICENSE /usr/local/share/doc/browse/LICENSE
RUN useradd --create-home --uid 10001 bud \
    && install -d -o bud -g bud -m 0700 /home/bud/.bud
ENV BUD_HOME=/home/bud/.bud \
    BUD_HOST=0.0.0.0 \
    OPENCODE_BROWSER_AUTO_UPDATE=off \
    PATH="/opt/bud/.venv/bin:$PATH"
USER bud
WORKDIR /home/bud
EXPOSE 8420
CMD ["bud", "serve"]
