FROM python:3.10-slim

WORKDIR /app

# 依存インストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリコピー
COPY app.py .
COPY launchpad.html .
COPY image-1781108826407.png .
COPY image-1781108859882.png .

# データ永続化ディレクトリ
RUN mkdir -p /data

ENV PORT=8000

EXPOSE 8000

CMD ["python", "app.py"]
