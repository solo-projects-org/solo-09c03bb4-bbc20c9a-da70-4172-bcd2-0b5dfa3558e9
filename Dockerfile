FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .

ENV APP_DB=/data/app.db
EXPOSE 8000

# 单 worker 多线程：进程内写锁保证交片按入库顺序串行生效；
# 数据库侧另有 BEGIN IMMEDIATE + 版本校验兜底。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "wsgi:app"]
