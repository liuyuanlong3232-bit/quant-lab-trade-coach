# Two-stage build: the image contains only built frontend assets and Python runtime.
FROM node:22-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY frontend ./
RUN pnpm build

FROM python:3.12-slim
ARG APP_UID=10001
RUN apt-get update \
    && apt-get install --no-install-recommends -y nginx \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid "${APP_UID}" --create-home --shell /usr/sbin/nologin quantlab \
    && mkdir -p /var/lib/quant-lab /var/log/quant-lab /run/nginx /var/cache/nginx/client_temp /var/cache/nginx/proxy_temp \
    && chown -R quantlab:quantlab /var/lib/quant-lab /var/log/quant-lab /run/nginx /var/cache/nginx
WORKDIR /opt/quant-lab
COPY pyproject.toml ./
COPY quant_lab ./quant_lab
RUN pip install --no-cache-dir --no-compile .
COPY --from=frontend-build /src/frontend/dist ./frontend/dist
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY deploy/entrypoint.sh /usr/local/bin/quant-lab-entrypoint
RUN chmod 0555 /usr/local/bin/quant-lab-entrypoint \
    && chown -R quantlab:quantlab /opt/quant-lab
ENV QUANT_LAB_DATA_DIR=/var/lib/quant-lab
EXPOSE 8080
USER quantlab
ENTRYPOINT ["/usr/local/bin/quant-lab-entrypoint"]
