# Lambda container image. Using the AWS base image rather than a zip because
# numpy pushes a zip past the 250 MB unzipped limit once you add anything else.
FROM public.ecr.aws/lambda/python:3.12

# numpy only. scipy is fit-time only (see monitor.py) and would add ~100 MB.
COPY lambda/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fitted model baked in at /opt -- 30 KB, not worth an S3 fetch per cold start.
COPY artifacts/monitor.json /opt/monitor.json

COPY src/tepguard ${LAMBDA_TASK_ROOT}/src/tepguard
COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
