# 自作 Lambda 用イメージ (差し替え検証)
# builtin と同じ AWS 公式 Lambda ベースイメージを使うため、
# ここで動いたコードはそのまま AWS の Lambda にデプロイできる。
FROM public.ecr.aws/lambda/python:3.12

# 追加ライブラリが必要な場合は requirements.txt に書く (空でも可)
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY app.py ${LAMBDA_TASK_ROOT}/

# ハンドラを変えたい場合はここを "<module>.<function>" に書き換える
CMD ["app.handler"]
