# Duas etapas: o uv e o código-fonte ficam na de construção, a imagem final
# leva só o ambiente virtual com o pacote instalado.
FROM python:3.13.16-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependências antes do código, para que mexer em src/ não reinstale tudo.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13.16-slim-trixie
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LETTERBOXD_DATA_DIR=/data
USER app
EXPOSE 8000
# Um worker só: jobs em andamento e o limitador de taxa vivem na memória do
# processo. Dois workers teriam dois limitadores e jobs invisíveis entre si.
CMD ["uvicorn", "letterboxd.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
