# A public lonoboard demo board: simulated data, nothing read from the host.
#   docker build -t lonoboard .
#   docker run -d -p 80:7777 -p 23:2323 lonoboard
# See docs/public-demo.md, and docs/deployment.md for running the real board in a container.
FROM python:3.12-slim

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

RUN useradd --create-home --uid 1000 lonoboard
USER lonoboard
WORKDIR /home/lonoboard

EXPOSE 7777 2323
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7777/health', timeout=4)"
CMD ["lonoboard", "demo", "--listen", "0.0.0.0"]
