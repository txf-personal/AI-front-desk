FROM node:22-bookworm-slim AS desk
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend /app/backend
RUN pip install --no-cache-dir /app/backend
COPY --from=desk /src/frontend/dist /app/frontend/dist
ENV DESK_DIST=/app/frontend/dist
ENV NORTHSIDE_DB=/data/northside.db
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.serve:create_runtime_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
