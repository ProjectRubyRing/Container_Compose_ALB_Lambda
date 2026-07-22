# AWS 公式 Lambda ベースイメージ (Lambda Runtime Interface Emulator 同梱)
# ローカルでも http://<host>:8080/2015-03-31/functions/function/invocations で invoke できる
FROM public.ecr.aws/lambda/python:3.12

COPY app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
