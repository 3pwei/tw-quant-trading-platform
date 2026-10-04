FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS runtime

# Debian stable security backports; retain signed APT verification.
# Exact versions fail closed if no longer available; never fall back to an older package.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade \
        libssl3t64=3.5.7-1~deb13u3 \
        openssl=3.5.7-1~deb13u3 \
        openssl-provider-legacy=3.5.7-1~deb13u3 \
        libpcre2-8-0=10.46-1~deb13u3 \
        libsqlite3-0=3.46.1-7+deb13u2 \
        perl-base=5.40.1-6+deb13u1 \
        gzip=1.13-1+deb13u1 \
    && for package in libssl3t64 openssl openssl-provider-legacy; do \
        test "$(dpkg-query -W -f='${Version}' "$package")" = '3.5.7-1~deb13u3' || exit 1; \
    done \
    && test "$(dpkg-query -W -f='${Version}' libpcre2-8-0)" = '10.46-1~deb13u3' \
    && test "$(dpkg-query -W -f='${Version}' libsqlite3-0)" = '3.46.1-7+deb13u2' \
    && test "$(dpkg-query -W -f='${Version}' perl-base)" = '5.40.1-6+deb13u1' \
    && test "$(dpkg-query -W -f='${Version}' gzip)" = '1.13-1+deb13u1' \
    && python -c "import ssl; print(ssl.OPENSSL_VERSION, flush=True); ssl.create_default_context()" \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/matplotlib \
    NUMBA_CACHE_DIR=/tmp/numba

ARG DEPLOY_COMMIT_SHA=unknown
LABEL org.opencontainers.image.revision="${DEPLOY_COMMIT_SHA}" \
      io.tw-quant.core.version="1.2.0" \
      io.tw-quant.core.commit="0e03e03505058a27fd8a4190cf2287b058a9d603" \
      io.tw-quant.core.sha256="63645e42755068c308d66d74ded5133395dfef816360dc8106e0bbc247ee49bd"

WORKDIR /app
RUN python -m pip install --no-cache-dir "uv==0.11.33"
COPY pyproject.toml uv.lock README.md THIRD_PARTY_NOTICES.md ./
COPY docs/dependency-notices.json ./dependency-notices.json
COPY docs/third-party-license-texts.txt ./third-party-license-texts.txt
COPY tw_quant ./tw_quant
RUN uv sync --locked --no-dev --extra server --no-editable \
    && .venv/bin/python -c "from tw_quant.strategy_registry import get_strategy_services; assert get_strategy_services().catalog() == []" \
    && uv cache clean \
    && python -m pip uninstall --yes uv pip wheel setuptools jaraco.context \
    && groupadd --gid 10001 twquant \
    && useradd --uid 10001 --gid twquant --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin twquant \
    && install -d -o twquant -g twquant -m 0750 /data /run/tw-quant-execution \
    && chown -R twquant:twquant /app

ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001

FROM runtime AS market-api
EXPOSE 8000
CMD ["uvicorn", "tw_quant.live.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS execution-worker
CMD ["python", "-m", "tw_quant.execution_service", "run"]
