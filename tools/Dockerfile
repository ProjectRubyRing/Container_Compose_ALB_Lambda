# 検証ツール (albcheck) 実行用コンテナ
#   ホストに Python が無くても、レスポンス確認・画面表示・Excel レポート出力ができる。
#   実物のテキストブラウザ (w3m / lynx) も入れてあるので --renderer w3m で比較できる。
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends w3m lynx \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8

WORKDIR /work
ENTRYPOINT ["python", "/work/tools/albcheck.py"]
CMD ["report"]
