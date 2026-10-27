FROM docker.io/library/docker:28.5.2-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d
RUN apk add --no-cache python3 iptables
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/gaoji
COPY src/local_runtime ./src/local_runtime
COPY src/sandbox_runtime ./src/sandbox_runtime
COPY src/storage_admission.py src/llm_options.py src/project_content.py ./src/
COPY images.lock.json manifest.json ./
COPY sandbox ./sandbox
RUN python3 -c "import src.local_runtime.desktop"
ENTRYPOINT ["python3", "-m", "src.local_runtime.desktop"]
CMD ["serve"]
