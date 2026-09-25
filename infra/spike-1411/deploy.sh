#!/usr/bin/env bash
# Stand up (and tear down) the ONE sandbox Lambda the #1411 spike measures.
#
# Everything it creates is named `archimedes-spike-1411*` and tagged
# `Project=archimedes-spike-1411`, so `./deploy.sh destroy` is a complete
# reversal. It creates NO production resources and MUTATES none: the VPC
# subnets and the ECS backend security group are *referenced* (attaching an
# existing SG to a new ENI does not modify that SG), which is what lets the
# function reach Aurora and ElastiCache without editing a single prod rule.
#
# Usage:
#   ./deploy.sh build     # build + push the image from the prod backend image
#   ./deploy.sh create    # role + function
#   ./deploy.sh invoke '<json event>'
#   ./deploy.sh destroy   # remove everything the spike created
set -euo pipefail

ACCOUNT=037613907429
REGION=us-east-1
NAME=archimedes-spike-1411
REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${NAME}"
TAG="${IMAGE_TAG:-probe}"
BASE_IMAGE="${BASE_IMAGE:-${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/archimedes-backend:latest}"
# Read from the live ECS service — see the ADR; these are the same private
# subnets and SG the backend task already runs in.
SUBNETS="${SUBNETS:-subnet-010efb75b1093ba92,subnet-0f412b89a025ca15b}"
SG="${SG:-sg-0f21c2773901a14e7}"
TAGS="Project=${NAME},Issue=1411,Ephemeral=true"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

case "${1:-}" in
build)
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
  docker pull --platform linux/amd64 "$BASE_IMAGE"
  # --provenance/--sbom off: Lambda rejects an OCI index carrying attestation
  # manifests ("The image manifest ... is not supported").
  docker build --platform linux/amd64 --provenance=false --sbom=false \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    -f "$ROOT/infra/spike-1411/Dockerfile.lambda" -t "${REPO}:${TAG}" "$ROOT"
  docker push "${REPO}:${TAG}"
  ;;

create)
  aws iam create-role --role-name "${NAME}-lambda" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --tags Key=Project,Value="${NAME}" Key=Issue,Value=1411 >/dev/null
  aws iam attach-role-policy --role-name "${NAME}-lambda" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
  # Same two grants the ECS task role carries, copied verbatim so the spike
  # measures production's IAM shape, not a more permissive one.
  aws iam put-role-policy --role-name "${NAME}-lambda" --policy-name "${NAME}-app" \
    --policy-document "$(cat "$ROOT/infra/spike-1411/role-policy.json")"
  sleep 10 # IAM propagation

  aws lambda create-function --function-name "$NAME" \
    --package-type Image --code ImageUri="${REPO}:${TAG}" \
    --role "arn:aws:iam::${ACCOUNT}:role/${NAME}-lambda" \
    --timeout 900 --memory-size "${MEMORY:-1769}" \
    --vpc-config "SubnetIds=${SUBNETS},SecurityGroupIds=${SG}" \
    --environment "Variables={$(paste -sd, - <"$ROOT/infra/spike-1411/function-env.txt")}" \
    --tags "$TAGS"
  ;;

invoke)
  aws lambda invoke --function-name "$NAME" --cli-binary-format raw-in-base64-out \
    --payload "${2:-\{\}}" --log-type Tail --query LogResult --output text /tmp/spike-out.json |
    base64 --decode
  echo "--- response ---"
  cat /tmp/spike-out.json
  ;;

destroy)
  aws lambda delete-function --function-name "$NAME" || true
  aws iam delete-role-policy --role-name "${NAME}-lambda" --policy-name "${NAME}-app" || true
  aws iam detach-role-policy --role-name "${NAME}-lambda" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole || true
  aws iam delete-role --role-name "${NAME}-lambda" || true
  aws ecr delete-repository --repository-name "$NAME" --force || true
  aws logs delete-log-group --log-group-name "/aws/lambda/${NAME}" || true
  ;;

*)
  echo "usage: $0 {build|create|invoke <json>|destroy}" >&2
  exit 2
  ;;
esac
