#!/usr/bin/env bash
# Deploy TEP-Guard to AWS.
#
# Two-stage on purpose: the Lambda cannot be created until an image exists in
# ECR, and ECR cannot exist until Terraform makes it. So we create ECR first,
# push the image, then apply the rest.
#
#     ./deploy.sh          build, push, apply, smoke test
#     ./deploy.sh destroy  tear everything down
set -euo pipefail

cd "$(dirname "$0")"
REGION="${REGION:-us-east-1}"
PROJECT="${PROJECT:-tep-guard}"
# Tag by git SHA, never :latest.
#
# :latest is a mutable pointer. Push a new image to the same tag and Terraform
# sees image_uri unchanged -- still the literal string "...:latest" -- so it
# does not update the function. Lambda resolved the digest when the function
# was created and keeps running that image forever. Everything reports
# success: docker builds, push succeeds, terraform applies, env vars update.
# The function silently executes old code. Cost us most of a day.
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)}"

step () { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ "${1:-}" = "destroy" ]; then
  step "Destroying all resources"
  terraform -chdir=infra destroy -auto-approve -var="region=$REGION"
  echo
  echo "Verifying nothing survived:"
  aws lambda list-functions --region "$REGION" \
    --query "Functions[?FunctionName=='$PROJECT'].FunctionName" --output text
  aws ecr describe-repositories --region "$REGION" \
    --query "repositories[?repositoryName=='$PROJECT'].repositoryName" --output text 2>/dev/null || true
  aws s3api list-buckets --query "Buckets[?starts_with(Name,'$PROJECT')].Name" --output text
  echo "(empty output above means everything is gone)"
  exit 0
fi

step "Preflight"
command -v terraform >/dev/null || { echo "terraform not installed"; exit 1; }
command -v docker    >/dev/null || { echo "docker not installed"; exit 1; }
docker info >/dev/null 2>&1     || { echo "docker daemon not running"; exit 1; }
aws sts get-caller-identity --region "$REGION" >/dev/null || { echo "aws cli not configured"; exit 1; }
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "account $ACCOUNT, region $REGION"

[ -f artifacts/monitor.json ] || { echo "artifacts/monitor.json missing -- run 'make bench' first"; exit 1; }

step "Stage 1: create ECR repository"
terraform -chdir=infra init -input=false
terraform -chdir=infra apply -auto-approve -input=false \
  -var="region=$REGION" -var="project=$PROJECT" \
  -target=aws_ecr_repository.this -target=aws_ecr_lifecycle_policy.this

ECR=$(terraform -chdir=infra output -raw ecr_repository_url)
echo "repository: $ECR"

step "Stage 2: build and push image"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR"
# buildx defaults to pushing an OCI manifest list, which Lambda rejects with
# "The image manifest is not supported", so it needs --provenance=false. The
# legacy builder produces a Docker v2 manifest already and does not know the
# flag at all. Detect which one is actually present rather than assuming.
if docker buildx version >/dev/null 2>&1; then
  echo "using buildx"
  docker buildx build --provenance=false --load -f lambda/Dockerfile -t "$PROJECT:$TAG" .
else
  echo "using legacy builder"
  docker build -f lambda/Dockerfile -t "$PROJECT:$TAG" .
fi
docker tag "$PROJECT:$TAG" "$ECR:$TAG"
docker push "$ECR:$TAG"

step "Stage 3: apply the rest"
echo "image tag: $TAG"
terraform -chdir=infra apply -auto-approve -input=false \
  -var="region=$REGION" -var="project=$PROJECT" -var="image_tag=$TAG"

step "Verifying Lambda is running the image we just pushed"
FN=$(terraform -chdir=infra output -raw function_name)
PUSHED=$(aws ecr describe-images --repository-name "$PROJECT" --region "$REGION" \
  --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
RUNNING=$(aws lambda get-function --function-name "$FN" --region "$REGION" \
  --query 'Code.ResolvedImageUri' --output text | sed 's/.*@//')
if [ "$PUSHED" != "$RUNNING" ]; then
  echo "  digest mismatch, forcing update"
  echo "    pushed:  $PUSHED"
  echo "    running: $RUNNING"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --image-uri "$ECR:$TAG" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  RUNNING=$(aws lambda get-function --function-name "$FN" --region "$REGION" \
    --query 'Code.ResolvedImageUri' --output text | sed 's/.*@//')
fi
[ "$PUSHED" = "$RUNNING" ] && echo "  ok, running $RUNNING" || { echo "  STILL MISMATCHED"; exit 1; }

step "Smoke test against the deployed function"
python3 scripts/make_payload.py --fault 4 --out /tmp/payload.json
aws lambda invoke --function-name "$FN" --region "$REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload file:///tmp/payload.json /tmp/out.json >/dev/null
python3 -c "
import json
r = json.load(open('/tmp/out.json'))
b = r.get('body', r)
print(f\"  status {r.get('statusCode')}: {b['n_flagged']}/{b['n_samples']} flagged ({b['alarm_rate']:.1%}) in {b['duration_ms']:.0f} ms\")
for c in b['flagged_samples'][0]['top_contributors']:
    print(f\"    {c['variable']}\")
"

step "Done"
terraform -chdir=infra output
echo
echo "Tear down with:  ./deploy.sh destroy"
