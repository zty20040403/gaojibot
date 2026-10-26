FROM docker.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS whisper-build
ARG DOWNLOAD_SOURCE=official
RUN case "$DOWNLOAD_SOURCE" in \
      official) mirror=https://deb.debian.org ;; \
      tuna) mirror=https://mirrors.tuna.tsinghua.edu.cn ;; *) exit 2 ;; esac \
    && sed -i "s|http://deb.debian.org|$mirror|g" /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Retries "2";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80gaoji-download
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl cmake g++ make \
    && rm -rf /var/lib/apt/lists/*
COPY vendor/whisper.cpp-v1.9.4.tar.gz /tmp/whisper.tar.gz
RUN echo '57e280cee375ab02425b806ad5146b99f6eb9357e3c2b31357c8a6af2e2e44ae  /tmp/whisper.tar.gz' | sha256sum --check \
    && tar -xzf /tmp/whisper.tar.gz -C /tmp \
    && cmake -S /tmp/whisper.cpp-1.9.4 -B /tmp/build -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_BUILD_IS_DEV=OFF \
      -DGGML_NATIVE=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF \
      -DGGML_SSE42=OFF -DGGML_BMI2=OFF -DGGML_OPENMP=OFF -DGGML_BLAS=OFF \
    && cmake --build /tmp/build --target whisper-cli --parallel 2 \
    && strip /tmp/build/bin/whisper-cli

FROM docker.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG DOWNLOAD_SOURCE=official
RUN case "$DOWNLOAD_SOURCE" in \
      official) mirror=https://deb.debian.org ;; \
      tuna) mirror=https://mirrors.tuna.tsinghua.edu.cn ;; *) exit 2 ;; esac \
    && sed -i "s|http://deb.debian.org|$mirror|g" /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Retries "2";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80gaoji-download

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg fontconfig fonts-noto-cjk fonts-noto-color-emoji \
    poppler-utils libsndfile1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.8.22
COPY --from=whisper-build /tmp/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper-build /tmp/whisper.cpp-1.9.4/LICENSE /usr/local/share/licenses/whisper.cpp/LICENSE
WORKDIR /opt/gaoji
COPY pyproject.toml uv.lock README.md ./
RUN apt-get update && apt-get install -y --no-install-recommends g++ make libffi-dev libatomic1 \
    && UV_HTTP_TIMEOUT=180 UV_HTTP_RETRIES=3 UV_CONCURRENT_DOWNLOADS=4 \
    uv sync --frozen --no-dev --no-install-project --no-editable \
    && uv cache clean \
    && apt-get purge -y --auto-remove g++ make libffi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
COPY src ./src
COPY migrations ./migrations
COPY skills ./skills
COPY alembic.ini LICENSE THIRD_PARTY_NOTICES.md manifest.json ./
# Uploaded sources are private on the host; container code must be readable by
# the unprivileged runtime account without making that account their owner.
RUN chmod -R a+rX src migrations skills \
    && chmod a+r pyproject.toml uv.lock README.md alembic.ini LICENSE THIRD_PARTY_NOTICES.md manifest.json \
    && groupadd --gid 10001 gaoji && useradd --uid 10001 --gid 10001 --no-create-home gaoji
ENV PATH=/opt/gaoji/.venv/bin:$PATH HOME=/data/state
USER 10001:10001
ENTRYPOINT ["python", "-m", "src.local_runtime.entrypoint"]
CMD ["serve"]
